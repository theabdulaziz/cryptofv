import io
import json
import threading
import urllib.error
import urllib.request
from datetime import date
from decimal import Decimal as D

import pytest

from cryptofv.feeds import ExchangeFeed, SimulatedFeed
from cryptofv.live import LiveSession
from cryptofv.models import MarketQuote
from cryptofv.server import make_server

TODAY = date(2026, 3, 10)
Q4 = date(2025, 12, 31)

# Payload shapes follow each exchange's public REST documentation.
COINBASE = {"open": "99000", "high": "101500", "low": "98000", "last": "100000.00", "volume": "1000", "volume_30day": "40000"}
KRAKEN = {"error": [], "result": {"XXBTZUSD": {"c": ["100050.00", "0.01"], "v": ["120", "400"], "l": ["98000", "97900"]}}}
BITSTAMP = {"last": "99990", "volume": "150.5", "low": "98100", "high": "101400"}


class FakeWeb:
    def __init__(self, missing=()):
        self.calls, self.missing = [], set(missing)

    def __call__(self, url):
        self.calls.append(url)
        if any(m in url for m in self.missing):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO())
        if "coinbase" in url:
            return COINBASE
        if "kraken" in url:
            return KRAKEN
        return BITSTAMP


# ----------------------------------------------------------------------- feeds
def test_exchange_feed_parses_each_exchange_and_converts_volume_to_usd():
    web = FakeWeb()
    quotes, warnings = ExchangeFeed(get=web).quotes(["BTC"], TODAY)
    by = {q.market: q for q in quotes}
    assert warnings == []
    assert by["Coinbase"].price == D("100000.00") and by["Coinbase"].volume == D("100000000.00")
    assert by["Kraken"].price == D("100050.00") and by["Kraken"].volume == D("100050.00") * 400  # 24h, not today
    assert by["Bitstamp"].price == D("99990") and by["Bitstamp"].volume == D("99990") * D("150.5")
    assert all(q.date == TODAY and q.accessible for q in quotes)
    assert any("pair=XBTUSD" in u for u in web.calls)  # Kraken's symbol for bitcoin
    assert any("/products/BTC-USD/stats" in u for u in web.calls)
    assert any("/ticker/btcusd/" in u for u in web.calls)


def test_exchange_feed_reports_unlisted_pairs_and_keeps_the_rest():
    quotes, warnings = ExchangeFeed(get=FakeWeb(missing=["kraken"])).quotes(["BTC"], TODAY)
    assert {q.market for q in quotes} == {"Coinbase", "Bitstamp"}
    assert warnings == ["Kraken has no BTC-USD market (HTTP 404)"]


def test_exchange_feed_marks_inaccessible_and_rejects_unsafe_symbols():
    web = FakeWeb()
    quotes, warnings = ExchangeFeed(["coinbase", "kraken"], inaccessible=["kraken"], get=web).quotes(["BTC", "../X"], TODAY)
    assert {q.market: q.accessible for q in quotes} == {"Coinbase": True, "Kraken": False}
    assert warnings == ["../X: not a valid ticker symbol, skipped"]
    assert not any(".." in u for u in web.calls)


def test_exchange_feed_caches_within_ttl():
    web, now = FakeWeb(), [0.0]
    feed = ExchangeFeed(["coinbase"], ttl=10, get=web, clock=lambda: now[0])
    feed.quotes(["BTC"], TODAY)
    feed.quotes(["BTC"], TODAY)
    assert len(web.calls) == 1
    now[0] = 11
    feed.quotes(["BTC"], TODAY)
    assert len(web.calls) == 2


def test_unknown_exchange_is_rejected():
    with pytest.raises(ValueError, match="Unknown exchange"):
        ExchangeFeed(["mtgox"])


def base_quotes():
    return [MarketQuote(Q4, "BTC", "A", D("100000"), D("900")),
            MarketQuote(Q4, "BTC", "B", D("100010"), D("100")),
            MarketQuote(Q4, "BTC", "X", D("99990"), D("9999"), accessible=False)]


def test_simulated_feed_is_seeded_moves_and_keeps_market_structure():
    def run(seed):
        now = [0.0]
        feed = SimulatedFeed(base_quotes(), seed=seed, clock=lambda: now[0])
        first, _ = feed.quotes(["BTC"], TODAY)
        same, _ = feed.quotes(["BTC"], TODAY)  # same tick
        now[0] = 5
        later, _ = feed.quotes(["BTC"], TODAY)
        return first, same, later

    first, same, later = run(1)
    assert first == same and first != later
    assert run(1) == (first, same, later) and run(2)[0] != first
    assert {q.market: q.accessible for q in first} == {"A": True, "B": True, "X": False}
    assert all(abs(q.price / D("100000") - 1) < D("0.05") for q in later)
    assert feed_warns(["DOGE"]) == ["DOGE: no base quote to simulate from"]


def feed_warns(assets):
    return SimulatedFeed(base_quotes()).quotes(assets, TODAY)[1]


# --------------------------------------------------------------------- session
class StubFeed:
    label, simulated = "Stub", True

    def __init__(self, prices):
        self.prices = prices

    def quotes(self, assets, on):
        out = [MarketQuote(on, a, "M1", D(p), D("1000")) for a, p in self.prices.items() if a in set(assets)]
        return out, []


@pytest.fixture
def session(tmp_path):
    tx = tmp_path / "tx.csv"
    tx.write_text("tx_id,date,asset,type,units,amount,fee\nT001,2025-11-01,BTC,buy,2,160000,10\n")
    hist = tmp_path / "quotes.csv"
    hist.write_text("date,asset,market,price,volume\n2025-12-31,BTC,M1,90000,1000\n2025-12-31,ETH,M1,3000,1000\n")
    return LiveSession(tx, StubFeed({"BTC": "100000"}), hist, today=lambda: TODAY)


def test_state_marks_holdings_to_the_live_price_as_a_provisional_period(session):
    s = session.state()
    assert s["today"] == "2026-03-10" and s["period_start"] == "2025-12-31"
    assert s["tiles"] == {"fair_value": "200000.00", "cost_basis": "160000.00", "unrealized": "40000.00",
                          "period_gain_loss": "20000.00", "legacy_difference": "40000.00"}
    assert s["controls_passed"] and len(s["comparison"]) == 2
    assert s["rollforward"][0]["opening"] == "180000.00"
    assert s["markets"][0]["quotes"][0]["selected"] is True
    assert "<svg" in s["charts"]["bases"] and ">Now<" in s["charts"]["bases"]
    json.dumps(s)  # fully serialisable


def test_add_transaction_validates_against_the_ledger_then_persists(session):
    with pytest.raises(ValueError, match="only 2 held"):
        session.add_transaction({"type": "sell", "date": "2026-02-01", "asset": "btc", "units": "5", "amount": "1"})
    with pytest.raises(ValueError, match="future"):
        session.add_transaction({"type": "buy", "date": "2026-03-11", "asset": "BTC", "units": "1", "amount": "1"})
    with pytest.raises(ValueError, match="units is required"):
        session.add_transaction({"type": "buy", "date": "2026-02-01", "asset": "BTC", "amount": "1"})
    with pytest.raises(ValueError, match="No accessible market quote for DOGE"):
        session.add_transaction({"type": "buy", "date": "2026-02-01", "asset": "DOGE", "units": "1", "amount": "1"})
    assert len(session.state()["transactions"]) == 1  # nothing was saved

    tx = session.add_transaction({"type": "sell", "date": "2026-02-01", "asset": "btc", "units": "0.5", "amount": "52,000", "fee": "5"})
    assert tx.tx_id == "T002"
    s = session.state()
    assert [t["tx_id"] for t in s["transactions"]] == ["T001", "T002"]
    assert s["tiles"]["fair_value"] == "150000.00"
    assert "T002" in session.tx_path.read_text()


def test_delete_refuses_to_break_later_transactions(session):
    session.add_transaction({"type": "sell", "date": "2026-02-01", "asset": "BTC", "units": "1", "amount": "95000"})
    with pytest.raises(ValueError, match="Cannot delete T001"):
        session.delete_transaction("T001")
    with pytest.raises(KeyError):
        session.delete_transaction("nope")
    session.delete_transaction("T002")
    assert len(session.state()["transactions"]) == 1


def test_falls_back_to_last_close_when_feed_has_no_price(session):
    session.feed = StubFeed({})
    s = session.state()
    assert s["warnings"] == ["No live price for BTC; using the 2025-12-31 close."]
    assert s["tiles"]["fair_value"] == "180000.00"
    assert "close 2025-12-31" in s["holdings"][0]["market"]


def test_bad_ledger_is_reported_not_raised(session):
    session.tx_path.write_text("tx_id,date,asset,type,units,amount\nT1,2025-11-01,BTC,sell,1,5\n")
    s = session.state()
    assert "only 0 held" in s["error"] and len(s["transactions"]) == 1


def test_cost_method_can_be_switched(session):
    session.set_cost_method("hifo")
    assert session.state()["cost_method"] == "hifo"
    with pytest.raises(ValueError):
        session.set_cost_method("random")


# ---------------------------------------------------------------------- server
@pytest.fixture
def base_url(session):
    server = make_server(session, port=0, refresh=3)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def call(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={**({"Content-Type": "application/json"} if data else {}), **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_server_serves_app_state_and_exports(base_url):
    status, headers, body = call(base_url + "/")
    assert status == 200 and b"cryptofv" in body and "default-src 'self'" in headers["Content-Security-Policy"]
    status, _, body = call(base_url + "/api/state")
    assert status == 200 and json.loads(body)["refresh"] == 3
    status, headers, body = call(base_url + "/api/export/journal_entries.csv")
    assert status == 200 and body.startswith(b"entry_id,date,account") and "attachment" in headers["Content-Disposition"]
    assert call(base_url + "/report.html")[0] == 200
    assert call(base_url + "/api/export/passwd.csv")[0] == 404


def test_server_write_endpoints(base_url):
    good = {"type": "buy", "date": "2026-01-05", "asset": "BTC", "units": "1", "amount": "95000"}
    status, _, body = call(base_url + "/api/transactions", "POST", good)
    assert status == 201 and json.loads(body) == {"tx_id": "T002"}
    status, _, body = call(base_url + "/api/transactions", "POST", {**good, "type": "sell", "units": "99"})
    assert status == 400 and "only 3 held" in json.loads(body)["error"]
    assert call(base_url + "/api/settings", "POST", {"cost_method": "lifo"})[0] == 200
    assert call(base_url + "/api/settings", "POST", {"cost_method": "x"})[0] == 400
    assert call(base_url + "/api/transactions/T002", "DELETE")[0] == 200
    assert call(base_url + "/api/transactions/T002", "DELETE")[0] == 404


def test_server_refuses_cross_origin_writes_and_foreign_hosts(base_url):
    good = {"type": "buy", "date": "2026-01-05", "asset": "BTC", "units": "1", "amount": "95000"}
    assert call(base_url + "/api/transactions", "POST", good, {"Origin": "http://evil.example"})[0] == 403
    assert call(base_url + "/api/transactions/T001", "DELETE", headers={"Origin": "http://evil.example"})[0] == 403
    assert call(base_url + "/api/state", headers={"Host": "evil.example"})[0] == 403
    assert len(json.loads(call(base_url + "/api/state")[2])["transactions"]) == 1


# ---------------------------------------------------------------- period close
from cryptofv.close import fingerprint
from cryptofv.feeds import ExchangeFeed as _EF


class RealStub(StubFeed):
    label, simulated = "Stub Exchange", False


@pytest.fixture
def real(tmp_path):
    tx = tmp_path / "tx.csv"
    tx.write_text("tx_id,date,asset,type,units,amount,fee\nT001,2025-11-01,BTC,buy,2,160000,10\n")
    hist = tmp_path / "quotes.csv"
    hist.write_text("date,asset,market,price,volume\n2025-12-31,BTC,M1,90000,1000\n")
    return LiveSession(tx, RealStub({"BTC": "100000"}), hist, today=lambda: TODAY)


def test_close_freezes_quotes_writes_memo_and_locks_the_period(real):
    record = real.close_period("  Aziz  ", "Quarter-end cut-off")
    assert (record.fair_value, record.period_gain_loss, record.prepared_by) == (D("200000.00"), D("20000.00"), "Aziz")

    assert "2026-03-10,BTC,M1,100000" in real.quotes_path.read_text()          # evidence frozen on disk
    memo = (real.closes_dir / "fair-value-memo-2026-03-10.html").read_text()
    assert "Fair value measurement memo" in memo and record.fingerprint in memo
    assert "Quarter-end cut-off" in memo and "SIMULATED" not in memo
    assert json.loads((real.closes_dir / "closes.json").read_text())[0]["fingerprint"] == record.fingerprint

    s = real.state()
    assert s["closed_today"] and s["locked_through"] == "2026-03-10"
    assert s["closes"][0]["intact"] is True
    assert s["close_blockers"] == ["The period ended 2026-03-10 is already closed."]
    with pytest.raises(ValueError, match="already closed"):
        real.close_period()
    with pytest.raises(ValueError, match="is closed"):
        real.add_transaction({"type": "buy", "date": "2026-03-01", "asset": "BTC", "units": "1", "amount": "1"})
    with pytest.raises(ValueError, match="is closed"):
        real.delete_transaction("T001")


def test_closed_position_ignores_later_price_moves_and_survives_restart(real):
    real.close_period()
    real.feed = RealStub({"BTC": "150000"})
    assert real.state()["tiles"]["fair_value"] == "200000.00"        # still the closed figure today

    reopened = LiveSession(real.tx_path, RealStub({"BTC": "150000"}), real.quotes_path, today=lambda: date(2026, 3, 11))
    s = reopened.state()
    assert s["period_start"] == "2026-03-10" and s["tiles"]["fair_value"] == "300000.00"
    assert s["tiles"]["period_gain_loss"] == "100000.00"             # measured from the close
    assert s["closes"][0]["intact"] and not s["closed_today"]
    assert "Fair value measurement memo" in reopened.memo("2026-03-10")
    assert reopened.memo("../../etc/passwd") is None


def test_tampering_with_a_closed_period_is_detected(real):
    real.close_period()
    text = real.quotes_path.read_text().replace("2026-03-10,BTC,M1,100000", "2026-03-10,BTC,M1,120000")
    real.quotes_path.write_text(text)
    reopened = LiveSession(real.tx_path, RealStub({"BTC": "150000"}), real.quotes_path, today=lambda: date(2026, 3, 11))
    s = reopened.state()
    assert s["closes"][0]["intact"] is False
    assert "changed after close" in s["closes"][0]["detail"] and "fair value is now 240,000.00" in s["closes"][0]["detail"]
    assert s["warnings"][0].startswith("Closed period altered after close: 2026-03-10")


def test_backdated_transaction_edited_into_the_file_is_detected(real):
    real.close_period()
    with real.tx_path.open("a") as f:
        f.write("T999,2026-01-15,BTC,buy,1,90000,0\n")
    reopened = LiveSession(real.tx_path, RealStub({"BTC": "150000"}), real.quotes_path, today=lambda: date(2026, 3, 11))
    assert reopened.state()["closes"][0]["intact"] is False


def test_close_is_blocked_without_a_current_price(real):
    real.feed = RealStub({})
    with pytest.raises(ValueError, match="No live price for BTC"):
        real.close_period()
    assert not real.closes_dir.exists()


def test_simulated_close_writes_nothing_to_disk(session):
    before = session.quotes_path.read_text()
    record = session.close_period("Aziz")
    assert record.simulated and session.quotes_path.read_text() == before and not session.closes_dir.exists()
    assert "SIMULATED PRICES" in session.memo("2026-03-10")
    assert session.state()["closes"][0]["intact"]


def test_fingerprint_ignores_number_formatting_and_later_transactions():
    from cryptofv.models import Transaction, TxType
    q1 = [MarketQuote(TODAY, "BTC", "M", D("100.0"), D("5"))]
    q2 = [MarketQuote(TODAY, "BTC", "M", D("100"), D("5.00"))]
    t = [Transaction("A", date(2026, 1, 1), "BTC", TxType.BUY, D("1"), D("10"))]
    later = t + [Transaction("B", date(2026, 4, 1), "BTC", TxType.BUY, D("1"), D("10"))]
    assert fingerprint(q1, t, TODAY) == fingerprint(q2, later, TODAY)
    assert fingerprint(q1, t, TODAY) != fingerprint([MarketQuote(TODAY, "BTC", "M", D("101"), D("5"))], t, TODAY)


def test_server_close_endpoint_and_memo(base_url):
    status, _, body = call(base_url + "/api/close", "POST", {"prepared_by": "Aziz"})
    assert status == 201 and json.loads(body)["period_end"] == "2026-03-10"
    status, _, body = call(base_url + "/closes/2026-03-10.html")
    assert status == 200 and b"Fair value measurement memo" in body
    assert call(base_url + "/closes/1999-01-01.html")[0] == 404
    assert call(base_url + "/api/close", "POST", {})[0] == 400
    assert call(base_url + "/api/close", "POST", {}, {"Origin": "http://evil.example"})[0] == 403
