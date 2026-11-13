"""Period close: freeze the evidence, write the memo, lock the period.

Closing does three things a live monitor cannot:

1. The price snapshot used for the measurement (every market observed, not
   only the one selected) is frozen as that period's quotes.
2. A fair value memo is written that documents the principal-market
   determination, the measurement, the closing entry and the controls.
3. A SHA-256 fingerprint of the frozen quotes and of every transaction up to
   the close date is recorded. It is recomputed on every later run, so a
   closed period that has been edited after the fact is detected.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html import escape
from typing import Iterable

from . import analytics
from .engine import Result
from .html_report import _CSS, _METHOD, _PAGE, _card, _m, _rollforward_table, _table, _units
from .models import ZERO, MarketQuote, Transaction


@dataclass(frozen=True)
class CloseRecord:
    period_end: date
    closed_at: str  # UTC timestamp of the price snapshot
    fair_value: Decimal
    period_gain_loss: Decimal
    prepared_by: str
    fingerprint: str
    feed: str
    simulated: bool


def _n(x: Decimal) -> str:
    """Canonical number text, so 2.5 and 2.50 fingerprint identically."""
    return format(x.normalize(), "f")


def fingerprint(quotes: Iterable[MarketQuote], transactions: Iterable[Transaction], period_end: date) -> str:
    lines = [f"close|{period_end.isoformat()}"]
    lines += sorted(
        f"q|{q.asset}|{q.market}|{_n(q.price)}|{_n(q.volume)}|{int(q.accessible)}"
        for q in quotes if q.date == period_end
    )
    lines += sorted(
        f"t|{t.date.isoformat()}|{t.tx_id}|{t.asset}|{t.type.value}|{_n(t.units)}|{_n(t.amount)}|{_n(t.fee)}|{t.to_asset}|{_n(t.to_units)}"
        for t in transactions if t.date <= period_end
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _compact(v: Decimal) -> str:
    n = float(v)
    return f"{n / 1e9:.1f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n:,.0f}"


_MEMO_CSS = """
main{grid-template-columns:minmax(0,1fr);max-width:900px}
.memo p{max-width:80ch;margin:8px 0}.memo ul{margin:6px 0;padding-left:20px;max-width:80ch}
.watermark{border:2px solid var(--critical);color:var(--critical);border-radius:8px;padding:10px 14px;font-weight:650}
.fp{font-family:ui-monospace,Consolas,monospace;font-size:12px;overflow-wrap:anywhere}
.sign{display:grid;grid-template-columns:1fr 1fr;gap:28px;margin-top:18px}
.sign div{border-top:1px solid var(--base);padding-top:6px;color:var(--text-secondary);font-size:13px}
h3{font-size:14px;margin:16px 0 4px}
@media print{.card{border:none;padding:8px 0}}
"""


def build_memo(result: Result, quotes: list[MarketQuote], record: CloseRecord, cost_method: str,
               capitalize_fees: bool, note: str = "") -> str:
    """The fair value memo for a closed period, as a self-contained printable page."""
    end = record.period_end
    iso = end.isoformat()
    holdings = [h for h in result.holdings if h.period_end == end]
    dets = {d.asset: d for d in result.determinations if d.date == end}
    spreads = {s.asset: s for s in analytics.market_spreads(result) if s.period_end == end}
    volume = {(q.asset, q.market): q.volume for q in quotes if q.date == end}
    ends = result.period_ends
    prior = ends[ends.index(end) - 1] if ends.index(end) > 0 else None
    cost = sum((h.cost_basis for h in holdings), ZERO)

    parts = [f'<header><div><h1>Fair value measurement memo</h1>'
             f'<p class="sub">Crypto assets · measurement date {iso}</p></div></header>']
    if record.simulated:
        parts.append('<div class="watermark wide">SIMULATED PRICES. This memo was produced in demo mode from a random '
                     'walk, not market data. It illustrates the format only and is not a record of anything.</div>')

    facts = [
        ["Measurement date", iso],
        ["Price snapshot taken (UTC)", record.closed_at],
        ["Price source", record.feed + (" (simulated)" if record.simulated else ", public last-trade tickers")],
        ["Prior reporting date", prior.isoformat() if prior else "None (first period)"],
        ["Cost basis method", _METHOD[cost_method]],
        ["Purchase fees", "Capitalised" if capitalize_fees else "Expensed as incurred"],
        ["Fair value of crypto assets", _m(record.fair_value)],
        ["Cost basis", _m(cost)],
        ["Gain or (loss) recognised in the period", _m(record.period_gain_loss)],
        ["Prepared by", record.prepared_by or "Not stated"],
    ]
    parts.append(_card("Summary", "What was measured, when, and from what evidence.",
                       _table(["Item", "Value"], facts, text_cols=2), wide=True))

    levels = sorted({h.level for h in holdings})
    conclusion = (
        f"<p>Crypto assets within the scope of ASC 350-60 are measured at fair value, with changes recognised in net "
        f"income. Fair value is the quoted price in the principal market for each asset, determined under ASC 820 as "
        f"the market with the greatest volume and level of activity that the entity can access. Transaction costs are "
        f"not deducted from the measurement.</p>"
        f"<p>At {iso} the entity held {len(holdings)} crypto asset{'s' if len(holdings) != 1 else ''} with a total "
        f"fair value of <strong>{_m(record.fair_value)}</strong>. "
        + (f"All measurements are categorised as {escape(levels[0])}." if len(levels) == 1 else
           f"Measurements fall in: {escape(', '.join(levels))}." if levels else "No assets were held.")
        + "</p>"
    )
    if note:
        conclusion += f"<p><strong>Preparer's note:</strong> {escape(note)}</p>"
    parts.append(f'<section class="card wide memo"><h2>Conclusion</h2>{conclusion}</section>')

    body = ""
    for h in holdings:
        alts = [a for a in result.alternatives if a.period_end == end and a.asset == h.asset]
        rows = [[a.market, "Selected" if a.selected else ("Accessible" if a.accessible else "Not accessible"),
                 _m(a.price), _compact(volume.get((a.asset, a.market), ZERO)), _m(a.fair_value), _m(a.difference)]
                for a in alts]
        s = spreads.get(h.asset)
        spread = (f" Had another accessible market been used, the measurement would have differed by at most "
                  f"{_m(s.spread)} ({s.spread_bps} basis points).") if s and s.spread else ""
        body += (f"<h3>{escape(h.asset)}: {_units(h.units)} units at {_m(h.price)} = {_m(h.fair_value)}</h3>"
                 + _table(["Market", "Status", "Price", "24h volume (USD)", "Fair value if used", "Difference"],
                          rows, text_cols=2)
                 + f"<p>{escape(dets[h.asset].rationale)} Categorised as {escape(h.level)}.{spread}</p>")
    parts.append(f'<section class="card wide memo"><h2>Principal market determination</h2>'
                 f'<p class="sub">Every market observed at the snapshot, including those not used.</p>'
                 f'{body or "<p>No holdings.</p>"}</section>')

    parts.append(_card("Rollforward for the period",
                       f"Activity from {prior.isoformat() if prior else 'inception'} to {iso}.",
                       _rollforward_table([r for r in result.rollforward if r.period_end == end]), wide=True))

    entry = [j for j in result.journal if j.date == end and j.memo.startswith("Remeasure")]
    rows = [[j.entry_id, j.account, j.asset, _m(j.debit) if j.debit else "", _m(j.credit) if j.credit else ""]
            for j in entry]
    parts.append(_card("Closing remeasurement entries", "Posted at the measurement date.",
                       _table(["Entry", "Account", "Asset", "Debit", "Credit"], rows, text_cols=3) if rows
                       else "<p>No remeasurement was required.</p>", wide=True))

    ctl = "".join(
        f'<li><span class="mark {"good" if c.passed else "bad"}" aria-hidden="true">{"&#10003;" if c.passed else "&#10005;"}</span>'
        f'<div><strong>{escape(c.name)}</strong> <span class="state">{"Passed" if c.passed else "Failed"}</span>'
        f'<br><span class="sub">{escape(c.detail)}</span></div></li>' for c in result.controls)
    parts.append(_card("Reconciliation controls", "All were required to pass before the period could be closed.",
                       f'<ul class="controls">{ctl}</ul>', wide=True))

    parts.append(
        '<section class="card wide memo"><h2>Matters for the reviewer</h2><ul>'
        '<li>Whether each asset is within the scope of ASC 350-60. This tool assumes it is.</li>'
        '<li>Whether the markets marked accessible are in fact accessible to the entity at the measurement date.</li>'
        '<li>Whether 24-hour USD volume is a fair proxy for volume and level of activity in each market.</li>'
        '<li>Whether the snapshot time is consistent with the entity\'s documented cut-off. Prices are last trades, '
        'not a closing auction, and crypto markets do not close.</li></ul>'
        '<h3>Evidence fingerprint</h3>'
        '<p>SHA-256 over the frozen quotes for this date and every transaction dated on or before it. cryptofv '
        'recomputes it on every run and reports the period as altered if it no longer matches.</p>'
        f'<p class="fp">{record.fingerprint}</p>'
        '<div class="sign"><div>Prepared by' + (f": {escape(record.prepared_by)}" if record.prepared_by else "")
        + '<br>Date</div><div>Reviewed by<br>Date</div></div></section>')

    parts.append('<footer>Generated by cryptofv. Supports, and does not replace, professional judgment.</footer>')
    return _PAGE.format(title=f"Fair value memo {iso}", css=_CSS + _MEMO_CSS, js="", body="\n".join(parts))
