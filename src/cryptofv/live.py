"""Live session: the books marked to market right now.

"Now" is treated as a provisional reporting date. Historical period ends come
from the quotes file as usual; today's quotes come from a price feed. The same
engine, controls and analytics then run unchanged, so the live view is a flash
close rather than a separate calculation.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from . import analytics, html_report
from .close import CloseRecord, build_memo, fingerprint
from .engine import COST_METHODS, FairValueEngine, Result
from .feeds import PriceFeed
from .io import load_price_lows, load_quotes, load_transactions, save_quotes, save_transactions
from .models import ZERO, MarketQuote, Transaction, TxType, money
from .principal_market import NoAccessibleMarketError


def _plain(obj):
    """Dataclasses, Decimals and dates to JSON-safe values. Decimals stay exact as strings."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _plain(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, TxType):
        return obj.value
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


@dataclass
class _Run:
    result: Result
    quotes: list[MarketQuote]      # today's quotes as used: live, or frozen if closed today
    warnings: list[str]
    today: date
    stale: list[str] = field(default_factory=list)  # assets priced from an old close
    closed_today: bool = False


class LiveSession:
    def __init__(
        self,
        transactions_path: str | Path,
        feed: PriceFeed,
        quotes_path: str | Path | None = None,
        price_lows_path: str | Path | None = None,
        cost_method: str = "fifo",
        capitalize_fees: bool = False,
        today: Callable[[], date] = date.today,
    ):
        self.tx_path = Path(transactions_path)
        self.feed = feed
        self.cost_method = cost_method
        self.capitalize_fees = capitalize_fees
        self._today = today
        self.quotes_path = Path(quotes_path) if quotes_path else self.tx_path.with_name("period_quotes.csv")
        self._history = load_quotes(self.quotes_path) if self.quotes_path.exists() else []
        self._lows = load_price_lows(price_lows_path) if price_lows_path else []
        self._lock = threading.RLock()
        # Closed periods. Simulated sessions keep them in memory only, so demo
        # prices can never be written into real records.
        self.closes_dir = self.tx_path.with_name("closes")
        self._closes: list[CloseRecord] = []
        self._memos: dict[str, str] = {}
        log = self.closes_dir / "closes.json"
        if not feed.simulated and log.exists():
            for row in json.loads(log.read_text(encoding="utf-8")):
                self._closes.append(CloseRecord(
                    date.fromisoformat(row["period_end"]), row["closed_at"], Decimal(row["fair_value"]),
                    Decimal(row["period_gain_loss"]), row["prepared_by"], row["fingerprint"], row["feed"],
                    bool(row["simulated"])))

    # ---------------------------------------------------------------- helpers
    def _transactions(self) -> list[Transaction]:
        return load_transactions(self.tx_path) if self.tx_path.exists() else []

    def _run(self, txs: list[Transaction]) -> _Run:
        today = self._today()
        history = [q for q in self._history if q.date < today]
        frozen = [q for q in self._history if q.date == today]
        assets = {t.asset for t in txs} | {t.to_asset for t in txs if t.to_asset}
        if frozen:  # the period was closed today: the books show the closed position
            live, warnings = frozen, []
        else:
            live, warnings = self.feed.quotes(assets, today) if assets else ([], [])
        ends = sorted({q.date for q in history} | {today})
        engine = FairValueEngine(self.cost_method, self.capitalize_fees)
        stale: list[str] = []
        try:
            result = engine.run(txs, history + live, ends, self._lows)
        except NoAccessibleMarketError:
            # A held asset has no live price: fall back to its last recorded close, and say so.
            priced = {q.asset for q in live if q.accessible}
            for asset in sorted(assets - priced):
                past = [q for q in history if q.asset == asset]
                if past:
                    last = max(q.date for q in past)
                    live = live + [replace(q, date=today, market=f"{q.market} (close {last.isoformat()})")
                                   for q in past if q.date == last]
                    warnings.append(f"No live price for {asset}; using the {last.isoformat()} close.")
                    stale.append(asset)
            result = engine.run(txs, history + live, ends, self._lows)
        return _Run(result, live, warnings, today, stale, bool(frozen))

    @property
    def locked_through(self) -> date | None:
        return max((c.period_end for c in self._closes), default=None)

    def _integrity(self, txs: list[Transaction], result: Result) -> list[dict]:
        """Recompute each closed period's fingerprint and fair value from what is on file now."""
        out = []
        for c in sorted(self._closes, key=lambda c: c.period_end, reverse=True):
            now_fp = fingerprint(self._history, txs, c.period_end)
            now_fv = sum((h.fair_value for h in result.holdings if h.period_end == c.period_end), ZERO)
            problems = []
            if now_fp != c.fingerprint:
                problems.append("quotes or transactions for this period were changed after close")
            if now_fv != c.fair_value:
                problems.append(f"fair value is now {now_fv:,.2f}, was {c.fair_value:,.2f} at close")
            out.append({**asdict(c), "intact": not problems,
                        "detail": "; ".join(problems) or "Matches the evidence recorded at close"})
        return out

    # ------------------------------------------------------------------ state
    def state(self) -> dict:
        with self._lock:
            base = {
                "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "feed": self.feed.label,
                "simulated": self.feed.simulated,
                "cost_method": self.cost_method,
                "cost_methods": list(COST_METHODS),
                "capitalize_fees": self.capitalize_fees,
            }
            try:
                txs = self._transactions()
                base["transactions"] = _plain(sorted(txs, key=lambda t: (t.date, t.tx_id)))
                run = self._run(txs)
            except (ValueError, OSError) as exc:
                return {**base, "transactions": base.get("transactions", []), "error": str(exc)}
            closes = self._integrity(txs, run.result)
            altered = [c["period_end"].isoformat() for c in closes if not c["intact"]]
            if altered:
                run.warnings.insert(0, "Closed period altered after close: " + ", ".join(altered)
                                    + ". See Closed periods on the Controls tab.")
            return {**base, **self._view(run.result, run.quotes, run.warnings, run.today), **_plain({
                "closes": closes,
                "closed_today": run.closed_today,
                "locked_through": self.locked_through,
                "close_blockers": self._blockers(txs, run),
            })}

    def _view(self, result: Result, live: list[MarketQuote], warnings: list[str], today: date) -> dict:
        ends = result.period_ends
        comp = analytics.compare_models(result)
        latest = comp[-1] if comp else None
        prior = ends[-2] if len(ends) > 1 else None
        holdings = [h for h in result.holdings if h.period_end == today]
        dets = {d.asset: d for d in result.determinations if d.date == today}
        spreads = {s.asset: s for s in analytics.market_spreads(result) if s.period_end == today}
        volume = {(q.asset, q.market): q.volume for q in live}

        markets = []
        for h in holdings:
            alts = [a for a in result.alternatives if a.period_end == today and a.asset == h.asset]
            s = spreads.get(h.asset)
            markets.append({
                "asset": h.asset, "units": h.units, "fair_value": h.fair_value, "price": h.price,
                "selected_market": h.market, "basis": h.basis, "level": h.level,
                "rationale": dets[h.asset].rationale,
                "spread": s.spread if s else ZERO, "spread_bps": s.spread_bps if s else ZERO,
                "quotes": [{**asdict(a), "volume": volume.get((a.asset, a.market), ZERO)} for a in alts],
            })

        labels = [e.strftime("%b %Y") for e in ends[:-1]] + ["Now"] if ends else []
        charts = {}
        if comp:
            bases = [("Fair value (ASC 350-60)", "--series-1", [c.fair_value for c in comp]),
                     ("Cost basis", "--series-2", [c.cost_basis for c in comp]),
                     ("Legacy cost less impairment", "--series-3", [c.legacy_carrying for c in comp])]
            pl = [("Fair value model", "--series-1", [c.fv_gain_loss for c in comp]),
                  ("Legacy model", "--series-3", [c.legacy_gain_loss for c in comp])]
            charts = {
                "bases": html_report._legend([(n, c) for n, c, _ in bases], line=True)
                + html_report._line_chart(labels, bases, "Carrying amount by reporting date"),
                "pnl": html_report._legend([(n, c) for n, c, _ in pl])
                + html_report._grouped_chart(labels, pl, "Gain or loss by period under each model"),
            }
            series = html_report.asset_series(result)
            charts["assets"] = (html_report._legend([(n, c) for n, c, _ in series])
                                + html_report._stacked_chart(labels, series, "Fair value by asset"))

        return _plain({
            "today": today,
            "period_start": prior,
            "warnings": warnings,
            "tiles": {
                "fair_value": latest.fair_value if latest else ZERO,
                "cost_basis": latest.cost_basis if latest else ZERO,
                "unrealized": (latest.fair_value - latest.cost_basis) if latest else ZERO,
                "period_gain_loss": latest.fv_gain_loss if latest else ZERO,
                "legacy_difference": latest.cumulative_difference if latest else ZERO,
            },
            "markets": markets,
            "holdings": holdings,
            "lots": [l for l in result.lots if l.period_end == today],
            "rollforward": [r for r in result.rollforward if r.period_end == today],
            "comparison": comp,
            "shocks": analytics.price_shocks(result),
            "controls": result.controls,
            "controls_passed": result.controls_passed,
            "journal": result.journal,
            "trial_balance": analytics.trial_balance(result.journal),
            "charts": charts,
        })

    # -------------------------------------------------------------- mutations
    def set_cost_method(self, method: str) -> None:
        if method not in COST_METHODS:
            raise ValueError(f"cost method must be one of: {', '.join(COST_METHODS)}")
        with self._lock:
            self.cost_method = method

    def _parse(self, data: dict, existing: list[Transaction]) -> Transaction:
        def dec(key: str, required: bool = True) -> Decimal:
            raw = str(data.get(key) or "").strip().replace(",", "")
            if not raw:
                if required:
                    raise ValueError(f"{key} is required")
                return ZERO
            try:
                value = Decimal(raw)
            except InvalidOperation:
                raise ValueError(f"{key} must be a number") from None
            if not value.is_finite():
                raise ValueError(f"{key} must be a number")
            return value

        try:
            kind = TxType(str(data.get("type", "")).strip().lower())
        except ValueError:
            raise ValueError("type must be buy, sell, swap or reward") from None
        try:
            on = date.fromisoformat(str(data.get("date", "")).strip())
        except ValueError:
            raise ValueError("date must be YYYY-MM-DD") from None
        if on > self._today():
            raise ValueError("date cannot be in the future")
        locked = self.locked_through
        if locked and on <= locked:
            raise ValueError(f"The period ended {locked.isoformat()} is closed. "
                             f"Date the entry after it, in the current period.")
        asset = str(data.get("asset", "")).strip().upper()
        if not asset.isalnum() or len(asset) > 10:
            raise ValueError("asset must be a ticker symbol such as BTC")
        to_asset = str(data.get("to_asset", "")).strip().upper() if kind is TxType.SWAP else ""
        if to_asset and (not to_asset.isalnum() or len(to_asset) > 10):
            raise ValueError("to_asset must be a ticker symbol such as ETH")

        taken = {t.tx_id for t in existing}
        tx_id = str(data.get("tx_id") or "").strip()
        if not tx_id:
            n = len(existing) + 1
            while f"T{n:03d}" in taken:
                n += 1
            tx_id = f"T{n:03d}"
        elif tx_id in taken:
            raise ValueError(f"tx_id {tx_id} already exists")
        elif len(tx_id) > 40 or any(c in tx_id for c in ',"\n\r'):
            raise ValueError("tx_id must be short and free of commas and quotes")

        return Transaction(tx_id, on, asset, kind, dec("units"), money(dec("amount")), money(dec("fee", False)),
                           to_asset, dec("to_units", kind is TxType.SWAP) if kind is TxType.SWAP else ZERO)

    def add_transaction(self, data: dict) -> Transaction:
        """Validate by running the full engine on the candidate ledger, then persist."""
        with self._lock:
            txs = self._transactions()
            tx = self._parse(data, txs)
            self._run(txs + [tx])  # raises ValueError if the ledger would be invalid
            save_transactions(self.tx_path, txs + [tx])
            return tx

    def delete_transaction(self, tx_id: str) -> None:
        with self._lock:
            txs = self._transactions()
            remaining = [t for t in txs if t.tx_id != tx_id]
            if len(remaining) == len(txs):
                raise KeyError(tx_id)
            locked = self.locked_through
            target = next(t for t in txs if t.tx_id == tx_id)
            if locked and target.date <= locked:
                raise ValueError(f"Cannot delete {tx_id}: the period ended {locked.isoformat()} is closed.")
            try:
                self._run(remaining)
            except ValueError as exc:
                raise ValueError(f"Cannot delete {tx_id}: later transactions depend on it ({exc})") from None
            save_transactions(self.tx_path, remaining)

    # ------------------------------------------------------------------ close
    def _blockers(self, txs: list[Transaction], run: _Run) -> list[str]:
        blockers = []
        if run.closed_today:
            blockers.append(f"The period ended {run.today.isoformat()} is already closed.")
        if not txs:
            blockers.append("There are no transactions to report on.")
        if run.stale:
            blockers.append("No live price for " + ", ".join(run.stale) + ". A close needs a current price for every holding.")
        if not run.result.controls_passed:
            blockers.append("One or more reconciliation controls failed.")
        return blockers

    def close_period(self, prepared_by: str = "", note: str = "") -> CloseRecord:
        """Freeze today's price snapshot as a closed reporting period and write its memo."""
        with self._lock:
            txs = self._transactions()
            run = self._run(txs)
            blockers = self._blockers(txs, run)
            if blockers:
                raise ValueError(blockers[0])

            frozen = [q for q in run.quotes if q.date == run.today]
            comp = analytics.compare_models(run.result)[-1]
            record = CloseRecord(
                period_end=run.today,
                closed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                fair_value=comp.fair_value,
                period_gain_loss=comp.fv_gain_loss,
                prepared_by=" ".join(prepared_by.split())[:80],
                fingerprint=fingerprint(frozen, txs, run.today),
                feed=self.feed.label,
                simulated=self.feed.simulated,
            )
            memo = build_memo(run.result, frozen, record, self.cost_method, self.capitalize_fees,
                              " ".join(note.split())[:500])
            key = record.period_end.isoformat()

            if not self.feed.simulated:  # write evidence first, then the log that points to it
                save_quotes(self.quotes_path, self._history + frozen)
                self.closes_dir.mkdir(parents=True, exist_ok=True)
                (self.closes_dir / f"fair-value-memo-{key}.html").write_text(memo, encoding="utf-8")
                log = self.closes_dir / "closes.json"
                tmp = log.with_suffix(".tmp")
                tmp.write_text(json.dumps(_plain(self._closes + [record]), indent=2), encoding="utf-8")
                tmp.replace(log)
            self._history = self._history + frozen
            self._closes.append(record)
            self._memos[key] = memo
            return record

    def memo(self, period_end: str) -> str | None:
        with self._lock:
            if period_end in self._memos:
                return self._memos[period_end]
            if any(c.period_end.isoformat() == period_end for c in self._closes):
                path = self.closes_dir / f"fair-value-memo-{period_end}.html"
                if path.exists():
                    return path.read_text(encoding="utf-8")
            return None

    def result(self) -> Result:
        with self._lock:
            return self._run(self._transactions()).result
