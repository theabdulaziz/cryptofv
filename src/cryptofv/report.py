"""Markdown disclosure report built from an engine Result."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from . import analytics
from .engine import Result
from .models import ZERO

_METHOD_NAMES = {
    "fifo": "first-in, first-out",
    "lifo": "last-in, first-out",
    "hifo": "specific identification (highest cost first)",
}


def _m(x: Decimal) -> str:
    return f"({-x:,.2f})" if x < ZERO else f"{x:,.2f}"


def _table(header: list[str], rows: list[list[str]], text_cols: int = 1) -> str:
    align = ["---"] * text_cols + ["---:"] * (len(header) - text_cols)
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(align) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def to_markdown(result: Result, adoption_date: date | None = None) -> str:
    out = ["# Crypto asset disclosures (ASC 350-60)", ""]
    out.append(
        f"Crypto assets are measured at fair value with changes recognised in net income. "
        f"Cost basis of units disposed of is determined using the "
        f"{_METHOD_NAMES[result.cost_method]} method. Purchase fees are "
        f"{'capitalised' if result.capitalize_fees else 'expensed as incurred'}. All amounts in USD."
    )

    passed = sum(c.passed for c in result.controls)
    out += ["", f"**Controls: {passed} of {len(result.controls)} passed.**", ""]
    out += [f"- {'PASS' if c.passed else 'FAIL'}: {c.name} ({c.detail})" for c in result.controls]

    comp = analytics.compare_models(result)
    if adoption_date is not None:
        t = analytics.transition_adjustment(result, adoption_date)
        out += ["", f"## Transition adjustment on adoption at {adoption_date.isoformat()}", "",
                f"Fair value of {_m(t.fair_value)} less legacy carrying amount of {_m(t.legacy_carrying)} "
                f"at {t.period_end.isoformat()} gives a pre-tax cumulative-effect adjustment to opening "
                f"retained earnings of **{_m(t.cumulative_difference)}**."]

    out += ["", "## Fair value against the legacy cost-less-impairment model", "", _table(
        ["Period end", "Fair value", "Legacy carrying", "Cumulative difference",
         "Fair value gain/(loss)", "Legacy gain/(loss)", "Period difference"],
        [[c.period_end.isoformat(), _m(c.fair_value), _m(c.legacy_carrying), _m(c.cumulative_difference),
          _m(c.fv_gain_loss), _m(c.legacy_gain_loss), _m(c.period_difference)] for c in comp])]
    if not result.has_price_history:
        out += ["", "No price history was supplied, so legacy impairment is tested at reporting dates only "
                    "and is understated."]

    holdings = defaultdict(list)
    for h in result.holdings:
        holdings[h.period_end].append(h)
    rolls = defaultdict(list)
    for r in result.rollforward:
        rolls[r.period_end].append(r)
    dets = {(d.date, d.asset): d for d in result.determinations}
    spreads = {(s.period_end, s.asset): s for s in analytics.market_spreads(result)}

    for end in sorted(rolls):
        out += ["", f"## Period ended {end.isoformat()}", "", "### Significant holdings", ""]
        hs = holdings.get(end, [])
        if hs:
            rows = [
                [h.asset, f"{h.units.normalize():f}", _m(h.cost_basis), _m(h.fair_value),
                 f"{h.market} ({h.basis})", h.level]
                for h in hs
            ]
            rows.append([
                "**Total**", "", _m(sum((h.cost_basis for h in hs), ZERO)),
                _m(sum((h.fair_value for h in hs), ZERO)), "", "",
            ])
            out.append(_table(["Asset", "Units", "Cost basis", "Fair value", "Market", "Hierarchy"], rows))
        else:
            out.append("No crypto assets held.")

        rs = rolls[end]
        cols = ["opening", "additions", "dispositions", "gains", "losses", "closing"]
        neg = {"dispositions", "losses"}
        rows = [[r.asset] + [_m(-getattr(r, c) if c in neg else getattr(r, c)) for c in cols] for r in rs]
        totals = {c: sum((getattr(r, c) for r in rs), ZERO) for c in cols}
        rows.append(["**Total**"] + [_m(-totals[c] if c in neg else totals[c]) for c in cols])
        out += ["", "### Rollforward", "", _table(
            ["Asset", "Opening FV", "Additions", "Dispositions", "Gains", "Losses", "Closing FV"], rows
        )]

        rg = sum((r.realized_gains for r in rs), ZERO)
        rl = sum((r.realized_losses for r in rs), ZERO)
        if rg or rl:
            out += ["", f"Dispositions in the period realised cumulative gains of {_m(rg)} "
                        f"and losses of {_m(rl)}, measured as proceeds less cost basis."]

        if hs:
            out += ["", "### Fair value determination", ""]
            for h in hs:
                line = f"- **{h.asset}** at {h.price:,.2f}: {dets[(end, h.asset)].rationale}"
                s = spreads.get((end, h.asset))
                if s and s.spread:
                    line += (f" Across accessible markets the measurement would range from {_m(s.lowest)} "
                             f"to {_m(s.highest)} ({s.spread_bps} bps).")
                out.append(line)

    shocks = analytics.price_shocks(result)
    if shocks:
        out += ["", f"## Price sensitivity at {shocks[0].period_end.isoformat()}", "", _table(
            ["Price move", "Fair value", "Effect on pre-tax income"],
            [[f"{s.shock_pct:+d}%", _m(s.fair_value), _m(s.pre_tax_effect)] for s in shocks])]

    return "\n".join(out) + "\n"
