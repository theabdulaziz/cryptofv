"""Analysis built on an engine Result: model comparison, sensitivity, trial balance."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .engine import Result
from .models import ZERO, JournalLine, money

DEFAULT_SHOCKS = (-50, -25, -10, 10, 25, 50)


@dataclass(frozen=True)
class ComparisonRow:
    """ASC 350-60 fair value vs legacy cost-less-impairment, one period.

    cumulative_difference is fair value less legacy carrying amount at the
    period end. For an entity adopting ASU 2023-08 the day after, it is the
    pre-tax cumulative-effect adjustment to opening retained earnings.
    """

    period_end: date
    cost_basis: Decimal
    fair_value: Decimal
    legacy_carrying: Decimal
    cumulative_difference: Decimal
    fv_gain_loss: Decimal
    legacy_gain_loss: Decimal
    legacy_impairment: Decimal
    period_difference: Decimal


@dataclass(frozen=True)
class ShockRow:
    period_end: date
    shock_pct: int
    fair_value: Decimal
    pre_tax_effect: Decimal


@dataclass(frozen=True)
class MarketSpread:
    """How much the measurement depends on which market is chosen."""

    period_end: date
    asset: str
    selected_market: str
    fair_value: Decimal
    lowest: Decimal
    highest: Decimal
    spread: Decimal
    spread_bps: Decimal


@dataclass(frozen=True)
class TrialBalanceRow:
    account: str
    debit: Decimal
    credit: Decimal


def compare_models(result: Result) -> list[ComparisonRow]:
    rows = []
    for end in result.period_ends:
        hs = [h for h in result.holdings if h.period_end == end]
        rs = [r for r in result.rollforward if r.period_end == end]
        fv = sum((h.fair_value for h in hs), ZERO)
        legacy = sum((h.legacy_carrying for h in hs), ZERO)
        fv_pl = sum((r.gains - r.losses for r in rs), ZERO)
        impairment = sum((r.legacy_impairment for r in rs), ZERO)
        legacy_pl = sum((r.legacy_disposal_gain for r in rs), ZERO) - impairment
        rows.append(ComparisonRow(
            period_end=end,
            cost_basis=sum((h.cost_basis for h in hs), ZERO),
            fair_value=fv,
            legacy_carrying=legacy,
            cumulative_difference=fv - legacy,
            fv_gain_loss=fv_pl,
            legacy_gain_loss=legacy_pl,
            legacy_impairment=impairment,
            period_difference=fv_pl - legacy_pl,
        ))
    return rows


def transition_adjustment(result: Result, adoption_date: date) -> ComparisonRow:
    """The comparison row at the reporting date immediately before adoption."""
    target = adoption_date - timedelta(days=1)
    for row in compare_models(result):
        if row.period_end == target:
            return row
    raise ValueError(
        f"Adoption on {adoption_date} needs a period end on {target}; "
        f"available: {', '.join(d.isoformat() for d in result.period_ends)}"
    )


def price_shocks(result: Result, shocks=DEFAULT_SHOCKS) -> list[ShockRow]:
    """Pre-tax income effect of a uniform price move on the latest holdings."""
    ends = result.period_ends
    if not ends:
        return []
    fv = sum((h.fair_value for h in result.holdings if h.period_end == ends[-1]), ZERO)
    out = []
    for s in shocks:
        shocked = money(fv * (Decimal(100 + s) / Decimal(100)))
        out.append(ShockRow(ends[-1], s, shocked, shocked - fv))
    return out


def market_spreads(result: Result) -> list[MarketSpread]:
    """Range of fair values across accessible markets, per holding."""
    grouped = defaultdict(list)
    for a in result.alternatives:
        if a.accessible:
            grouped[(a.period_end, a.asset)].append(a)
    out = []
    for (end, asset), alts in sorted(grouped.items()):
        chosen = next(a for a in alts if a.selected)
        lo, hi = min(a.fair_value for a in alts), max(a.fair_value for a in alts)
        bps = ((hi - lo) / chosen.fair_value * 10000).quantize(Decimal("0.1")) if chosen.fair_value else ZERO
        out.append(MarketSpread(end, asset, chosen.market, chosen.fair_value, lo, hi, hi - lo, bps))
    return out


def trial_balance(journal: list[JournalLine]) -> list[TrialBalanceRow]:
    bal: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for line in journal:
        bal[line.account] += line.debit - line.credit
    return [
        TrialBalanceRow(acct, v if v > ZERO else ZERO, -v if v < ZERO else ZERO)
        for acct, v in sorted(bal.items())
    ]
