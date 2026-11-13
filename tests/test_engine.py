from collections import defaultdict
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from cryptofv import (
    FairValueEngine,
    InsufficientUnitsError,
    MarketQuote,
    NoAccessibleMarketError,
    PriceLow,
    Transaction,
    TxType,
    analytics,
    determine_fair_value,
)
from cryptofv.cli import main
from cryptofv.html_report import to_html
from cryptofv.io import load_price_lows, load_quotes, load_transactions
from cryptofv.report import to_markdown

Q1, Q2 = date(2025, 3, 31), date(2025, 6, 30)
EXAMPLES = Path(__file__).parent.parent / "examples"


def quote(on, price, market="M1", volume="1000", asset="BTC", accessible=True, fee_bps="0"):
    return MarketQuote(on, asset, market, D(price), D(volume), accessible, D(fee_bps))


def buy(tx_id, on, units, amount, fee="0", asset="BTC"):
    return Transaction(tx_id, on, asset, TxType.BUY, D(units), D(amount), D(fee))


def sell(tx_id, on, units, amount, fee="0", asset="BTC"):
    return Transaction(tx_id, on, asset, TxType.SELL, D(units), D(amount), D(fee))


def balances(result):
    bal = defaultdict(lambda: D(0))
    for line in result.journal:
        bal[line.account] += line.debit - line.credit
    return bal


# ------------------------------------------------------------ principal market
def test_principal_market_is_highest_volume_not_highest_price():
    quotes = [quote(Q1, "100", "BIG", "9000"), quote(Q1, "105", "SMALL", "10")]
    det = determine_fair_value(quotes, "BTC", Q1)
    assert (det.market, det.price, det.basis, det.level) == ("BIG", D("100"), "principal", "Level 1")


def test_inaccessible_market_is_excluded_even_with_greatest_volume():
    quotes = [quote(Q1, "100", "LOCAL", "50"), quote(Q1, "99", "OFFSHORE", "99999", accessible=False)]
    det = determine_fair_value(quotes, "BTC", Q1)
    assert det.market == "LOCAL"
    assert "OFFSHORE" in det.rationale


def test_most_advantageous_market_uses_net_price_to_choose_but_gross_price_to_measure():
    # B has the higher quoted price, but A wins after transaction costs.
    quotes = [quote(Q1, "100.00", "A", "0", fee_bps="10"), quote(Q1, "100.50", "B", "0", fee_bps="100")]
    det = determine_fair_value(quotes, "BTC", Q1)
    assert det.basis == "most advantageous"
    assert det.market == "A"
    assert det.price == D("100.00")  # not reduced by the fee


def test_no_accessible_market_raises():
    with pytest.raises(NoAccessibleMarketError):
        determine_fair_value([quote(Q1, "100", accessible=False)], "BTC", Q1)


# ---------------------------------------------------------------------- engine
def test_remeasurement_gain_then_loss_flows_through_rollforward():
    txs = [buy("T1", date(2025, 1, 10), "2", "200")]
    quotes = [quote(Q1, "150"), quote(Q2, "120")]
    res = FairValueEngine().run(txs, quotes, [Q1, Q2])

    r1, r2 = res.rollforward
    assert (r1.opening, r1.additions, r1.gains, r1.losses, r1.closing) == (0, 200, 100, 0, 300)
    assert (r2.opening, r2.additions, r2.gains, r2.losses, r2.closing) == (300, 0, 0, 60, 240)
    assert res.holdings[-1].cost_basis == 200  # cost basis is unaffected by remeasurement


def test_fifo_partial_lot_sale():
    txs = [
        buy("T1", date(2025, 1, 5), "1", "100"),
        buy("T2", date(2025, 1, 6), "1", "200"),
        sell("T3", date(2025, 2, 1), "1.5", "450"),
    ]
    res = FairValueEngine("fifo").run(txs, [quote(Q1, "300")], [Q1])
    row, holding = res.rollforward[0], res.holdings[0]
    # Relieved: all of T1 (100) and half of T2 (100) -> cost out 200.
    assert row.realized_gains == 250
    assert holding.units == D("0.5")
    assert holding.cost_basis == 100
    assert holding.fair_value == 150
    assert [(l.lot_id, l.units) for l in res.lots] == [("T2", D("0.5"))]


@pytest.mark.parametrize("method, realized", [("fifo", 200), ("lifo", 100), ("hifo", 50)])
def test_cost_method_changes_realized_gain_but_not_closing_fair_value(method, realized):
    txs = [
        buy("T1", date(2025, 1, 5), "1", "100"),
        buy("T2", date(2025, 1, 6), "1", "250"),
        buy("T3", date(2025, 1, 7), "1", "200"),
        sell("T4", date(2025, 2, 1), "1", "300"),
    ]
    res = FairValueEngine(method).run(txs, [quote(Q1, "300")], [Q1])
    assert res.rollforward[0].realized_gains == realized
    assert res.rollforward[0].closing == 600


def test_fee_policy_expense_vs_capitalize():
    txs = [buy("T1", date(2025, 1, 5), "1", "100", fee="2")]
    expensed = FairValueEngine().run(txs, [quote(Q1, "100")], [Q1])
    capitalized = FairValueEngine(capitalize_fees=True).run(txs, [quote(Q1, "100")], [Q1])
    assert expensed.holdings[0].cost_basis == 100
    assert capitalized.holdings[0].cost_basis == 102
    assert capitalized.rollforward[0].losses == 2  # written back down to fair value


def test_full_exit_leaves_zero_closing_and_no_holding():
    txs = [buy("T1", date(2025, 1, 5), "1", "100"), sell("T2", date(2025, 2, 1), "1", "90")]
    res = FairValueEngine().run(txs, [], [Q1])  # no quote needed: nothing held
    assert res.holdings == []
    row = res.rollforward[0]
    assert (row.closing, row.losses, row.realized_losses) == (0, 10, 10)
    assert res.controls_passed


def test_reward_is_income_at_fair_value_and_becomes_cost_basis():
    txs = [Transaction("R1", date(2025, 1, 5), "ETH", TxType.REWARD, D("2"), D("500"))]
    res = FairValueEngine().run(txs, [quote(Q1, "300", asset="ETH")], [Q1])
    bal = balances(res)
    assert bal["Crypto rewards income"] == -500
    assert bal["Cash"] == 0
    assert res.holdings[0].cost_basis == 500
    assert res.rollforward[0].additions == 500
    assert res.rollforward[0].gains == 100


def test_swap_recognises_gain_on_asset_given_and_no_cash_moves():
    txs = [
        buy("T1", date(2025, 1, 5), "10", "1000", asset="SOL"),
        Transaction("S1", date(2025, 2, 1), "SOL", TxType.SWAP, D("4"), D("600"), D("0"), "ETH", D("0.2")),
    ]
    quotes = [quote(Q1, "150", asset="SOL"), quote(Q1, "3000", asset="ETH")]
    res = FairValueEngine().run(txs, quotes, [Q1])
    rows = {r.asset: r for r in res.rollforward}
    # SOL: 4 units relieved at cost 400 for 600 of value -> 200 gain; 6 left at 150 = 900 (+300).
    assert rows["SOL"].dispositions == 600 and rows["SOL"].realized_gains == 200
    assert rows["SOL"].gains == 500 and rows["SOL"].closing == 900
    # ETH enters at the exchange's fair value, then is remeasured.
    assert rows["ETH"].additions == 600 and rows["ETH"].closing == 600
    assert balances(res)["Cash"] == -1000  # only the original purchase
    assert res.controls_passed


def test_swap_requires_a_destination():
    bad = Transaction("S1", date(2025, 2, 1), "SOL", TxType.SWAP, D("1"), D("10"))
    with pytest.raises(ValueError, match="to_asset"):
        FairValueEngine().run([buy("T1", date(2025, 1, 5), "1", "10", asset="SOL"), bad], [], [Q1])


def test_overselling_raises():
    txs = [buy("T1", date(2025, 1, 5), "1", "100"), sell("T2", date(2025, 2, 1), "2", "300")]
    with pytest.raises(InsufficientUnitsError):
        FairValueEngine().run(txs, [quote(Q1, "100")], [Q1])


def test_transaction_after_last_period_end_raises():
    with pytest.raises(ValueError, match="after the last period end"):
        FairValueEngine().run([buy("T1", date(2025, 4, 1), "1", "100")], [quote(Q1, "100")], [Q1])


def test_duplicate_tx_id_raises():
    txs = [buy("T1", date(2025, 1, 5), "1", "100"), buy("T1", date(2025, 1, 6), "1", "100")]
    with pytest.raises(ValueError, match="Duplicate"):
        FairValueEngine().run(txs, [quote(Q1, "100")], [Q1])


# ---------------------------------------------------------------- legacy model
def test_legacy_impairment_is_never_reversed():
    txs = [buy("T1", date(2025, 1, 10), "1", "100")]
    quotes = [quote(Q1, "70"), quote(Q2, "130")]
    res = FairValueEngine().run(txs, quotes, [Q1, Q2])
    h1, h2 = res.holdings
    assert (h1.fair_value, h1.legacy_carrying) == (70, 70)
    assert (h2.fair_value, h2.legacy_carrying) == (130, 70)  # recovery not recognised
    c1, c2 = analytics.compare_models(res)
    assert (c1.fv_gain_loss, c1.legacy_gain_loss) == (-30, -30)
    assert (c2.fv_gain_loss, c2.legacy_gain_loss) == (60, 0)
    assert c2.cumulative_difference == 60


def test_intra_period_low_impairs_legacy_even_when_price_recovers_by_period_end():
    txs = [buy("T1", date(2025, 1, 10), "1", "100")]
    lows = [PriceLow(date(2025, 2, 15), "BTC", D("60"))]
    res = FairValueEngine().run(txs, [quote(Q1, "110")], [Q1], lows)
    assert res.holdings[0].fair_value == 110
    assert res.holdings[0].legacy_carrying == 60
    assert res.rollforward[0].legacy_impairment == 40


def test_low_before_acquisition_does_not_impair_the_lot():
    txs = [buy("T1", date(2025, 2, 20), "1", "100")]
    lows = [PriceLow(date(2025, 2, 15), "BTC", D("60"))]
    res = FairValueEngine().run(txs, [quote(Q1, "110")], [Q1], lows)
    assert res.holdings[0].legacy_carrying == 100


def test_legacy_sale_gain_is_measured_from_impaired_carrying_amount():
    txs = [buy("T1", date(2025, 1, 10), "1", "100"), sell("T2", date(2025, 3, 1), "1", "90")]
    lows = [PriceLow(date(2025, 2, 15), "BTC", D("60"))]
    res = FairValueEngine().run(txs, [], [Q1], lows)
    row = res.rollforward[0]
    assert row.legacy_impairment == 40 and row.legacy_disposal_gain == 30
    # Both models end with the same cumulative P&L once the position is closed.
    c = analytics.compare_models(res)[0]
    assert c.fv_gain_loss == c.legacy_gain_loss == -10


def test_transition_adjustment_needs_a_period_end_the_day_before():
    res = FairValueEngine().run([buy("T1", date(2025, 1, 10), "1", "100")], [quote(Q1, "70"), quote(Q2, "130")], [Q1, Q2])
    assert analytics.transition_adjustment(res, date(2025, 7, 1)).cumulative_difference == 60
    with pytest.raises(ValueError, match="needs a period end"):
        analytics.transition_adjustment(res, date(2025, 5, 1))


# ------------------------------------------------------------------- analytics
def test_market_spread_ignores_inaccessible_markets():
    quotes = [quote(Q1, "100", "A", "900"), quote(Q1, "101", "B", "100"),
              quote(Q1, "50", "X", "99999", accessible=False)]
    res = FairValueEngine().run([buy("T1", date(2025, 1, 5), "10", "900")], quotes, [Q1])
    (s,) = analytics.market_spreads(res)
    assert (s.selected_market, s.lowest, s.highest, s.spread) == ("A", 1000, 1010, 10)
    assert s.spread_bps == D("100.0")
    assert len(res.alternatives) == 3  # the inaccessible one is still shown


def test_price_shocks_are_symmetric_around_fair_value():
    res = FairValueEngine().run([buy("T1", date(2025, 1, 5), "1", "100")], [quote(Q1, "200")], [Q1])
    shocks = {s.shock_pct: s for s in analytics.price_shocks(res)}
    assert shocks[-25].pre_tax_effect == -50 and shocks[25].pre_tax_effect == 50
    assert shocks[-50].fair_value == 100


# ------------------------------------------------------- end-to-end on examples
@pytest.fixture(scope="module", params=["fifo", "lifo", "hifo"])
def example(request):
    txs = load_transactions(EXAMPLES / "transactions.csv")
    quotes = load_quotes(EXAMPLES / "quotes.csv")
    lows = load_price_lows(EXAMPLES / "price_lows.csv")
    ends = sorted({q.date for q in quotes})
    return txs, FairValueEngine(request.param).run(txs, quotes, ends, lows)


def test_all_controls_pass_on_examples(example):
    _, res = example
    assert len(res.controls) == 7
    assert [c.name for c in res.controls if not c.passed] == []


def test_general_ledger_agrees_to_disclosures(example):
    txs, res = example
    bal = balances(res)
    final = res.period_ends[-1]
    assert bal["Crypto assets, at fair value"] == sum(h.fair_value for h in res.holdings if h.period_end == final)
    assert sum(bal.values()) == 0

    # Cash proof, derived from the raw transactions rather than the journal.
    cash = D(0)
    for t in txs:
        if t.type is TxType.BUY:
            cash -= t.amount + t.fee
        elif t.type is TxType.SELL:
            cash += t.amount - t.fee
        else:
            cash -= t.fee
    assert bal["Cash"] == cash


def test_cumulative_model_difference_is_the_sum_of_period_differences(example):
    _, res = example
    comp = analytics.compare_models(res)
    assert sum(c.period_difference for c in comp) == comp[-1].cumulative_difference
    assert all(c.legacy_carrying <= c.cost_basis for c in comp)


def test_trial_balance_balances(example):
    _, res = example
    tb = analytics.trial_balance(res.journal)
    assert sum(t.debit for t in tb) == sum(t.credit for t in tb)


def test_principal_market_shifts_between_periods():
    quotes = load_quotes(EXAMPLES / "quotes.csv")
    assert determine_fair_value(quotes, "SOL", Q1).market == "ALPHA-US"
    assert determine_fair_value(quotes, "SOL", Q2).market == "BETA-US"


# --------------------------------------------------------------------- reports
def test_reports_render_and_escape_untrusted_names():
    txs = [buy("T1", date(2025, 1, 5), "1", "100", asset="<script>X")]
    res = FairValueEngine().run(txs, [quote(Q1, "120", market='M"<b>', asset="<script>X")], [Q1])
    html = to_html(res, adoption_date=date(2025, 4, 1))
    assert "<script>X" not in html and "&lt;script&gt;X" in html
    assert "<b>" not in html
    assert "Transition adjustment" in html
    assert "Transition adjustment" in to_markdown(res, date(2025, 4, 1))


def test_html_report_handles_no_periods():
    assert "No reporting periods" in to_html(FairValueEngine().run([], [], []))


def test_cli_writes_outputs(tmp_path):
    code = main([
        "run", "--transactions", str(EXAMPLES / "transactions.csv"),
        "--quotes", str(EXAMPLES / "quotes.csv"),
        "--price-lows", str(EXAMPLES / "price_lows.csv"),
        "--adoption-date", "2025-01-01", "--out", str(tmp_path),
    ])
    assert code == 0
    for name in ("journal_entries.csv", "holdings.csv", "lots.csv", "rollforward.csv",
                 "model_comparison.csv", "trial_balance.csv", "report.md", "dashboard.html"):
        assert (tmp_path / name).stat().st_size > 0


@pytest.mark.parametrize("content, message", [
    ("tx_id,date,asset,type,units\nT1,2025-01-05,BTC,buy,1\n", "missing column"),
    ("tx_id,date,asset,type,units,amount\nT1,2025-01-05,BTC,gift,1,100\n", "unknown type"),
    ("tx_id,date,asset,type,units,amount\nT1,05/01/2025,BTC,buy,1,100\n", "not a YYYY-MM-DD date"),
    ("tx_id,date,asset,type,units,amount\nT1,2025-01-05,BTC,buy,one,100\n", "not a number"),
])
def test_cli_reports_bad_input_with_file_and_line(tmp_path, capsys, content, message):
    bad = tmp_path / "tx.csv"
    bad.write_text(content)
    code = main(["run", "--transactions", str(bad), "--quotes", str(EXAMPLES / "quotes.csv"), "--out", str(tmp_path)])
    assert code == 1
    assert message in capsys.readouterr().err
