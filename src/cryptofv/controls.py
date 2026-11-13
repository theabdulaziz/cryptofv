"""Reconciliation controls run after every engine pass.

Each control re-derives a number two independent ways (ledger vs subledger,
ledger vs disclosure, units in vs units out) and reports whether they agree.
The CLI exits non-zero if any control fails.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Sequence

from .models import ZERO, Control, Transaction, TxType

if TYPE_CHECKING:
    from .engine import Result


def run_controls(result: "Result", txs: Sequence[Transaction]) -> list[Control]:
    from .engine import ACCT_CRYPTO, ACCT_GAIN, ACCT_LOSS

    controls: list[Control] = []

    def add(name: str, failures: list[str], ok_detail: str) -> None:
        controls.append(Control(name, not failures, "; ".join(failures) if failures else ok_detail))

    # 1. Double entry.
    by_entry: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for line in result.journal:
        by_entry[line.entry_id] += line.debit - line.credit
    add("Journal entries balance",
        [f"{e} out by {v}" for e, v in by_entry.items() if v != ZERO],
        f"{len(by_entry)} entries, debits equal credits in each")

    ends = result.period_ends
    final = ends[-1] if ends else None
    final_holdings = {h.asset: h for h in result.holdings if h.period_end == final}

    # 2. General ledger to lot subledger, asset by asset.
    gl: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for line in result.journal:
        if line.account == ACCT_CRYPTO:
            gl[line.asset] += line.debit - line.credit
    sub: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for lot in result.lots:
        if lot.period_end == final:
            sub[lot.asset] += lot.fair_value
    add("General ledger agrees to lot subledger",
        [f"{a}: GL {gl[a]} vs lots {sub[a]}" for a in sorted(set(gl) | set(sub)) if gl[a] != sub[a]],
        f"{len(sub)} asset(s), total {sum(sub.values(), ZERO):,.2f}")

    # 3. Rollforward arithmetic.
    add("Rollforward ties to closing fair value",
        [f"{r.asset} {r.period_end}" for r in result.rollforward
         if r.opening + r.additions - r.dispositions + r.gains - r.losses != r.closing],
        f"{len(result.rollforward)} asset-periods: opening + additions - dispositions + gains - losses = closing")

    # 4. Income statement to disclosure.
    bal: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for line in result.journal:
        bal[line.account] += line.debit - line.credit
    gains = sum((r.gains for r in result.rollforward), ZERO)
    losses = sum((r.losses for r in result.rollforward), ZERO)
    pl_fail = []
    if -bal[ACCT_GAIN] != gains:
        pl_fail.append(f"gains: GL {-bal[ACCT_GAIN]} vs rollforward {gains}")
    if bal[ACCT_LOSS] != losses:
        pl_fail.append(f"losses: GL {bal[ACCT_LOSS]} vs rollforward {losses}")
    add("Income statement agrees to rollforward", pl_fail,
        f"gains {gains:,.2f}, losses {losses:,.2f}")

    # 5. Units: everything acquired less everything disposed equals what is held.
    units: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for t in txs:
        if t.type in (TxType.BUY, TxType.REWARD):
            units[t.asset] += t.units
        else:
            units[t.asset] -= t.units
            if t.type is TxType.SWAP:
                units[t.to_asset] += t.to_units
    held = {a: h.units for a, h in final_holdings.items()}
    add("Units reconcile to transaction history",
        [f"{a}: history {units[a]} vs held {held.get(a, ZERO)}"
         for a in sorted(set(units) | set(held)) if units[a] != held.get(a, ZERO)],
        f"{len(units)} asset(s) traced from {len(txs)} transactions")

    # 6. Cumulative P&L splits into realised and unrealised.
    net = gains - losses
    realized = sum((r.realized_gains - r.realized_losses for r in result.rollforward), ZERO)
    unrealized = sum((h.fair_value - h.cost_basis for h in final_holdings.values()), ZERO)
    add("Cumulative gain equals realised plus unrealised",
        [] if net == realized + unrealized else [f"{net} vs {realized} + {unrealized}"],
        f"{net:,.2f} = {realized:,.2f} realised + {unrealized:,.2f} unrealised")

    # 7. Legacy model sanity: impairment only ever reduces, never above cost.
    add("Legacy carrying amount never exceeds cost",
        [f"{h.asset} {h.period_end}" for h in result.holdings if h.legacy_carrying > h.cost_basis],
        "cost-less-impairment is at or below cost for every holding")

    return controls
