"""Lot-level ledger that measures crypto assets at fair value through net income.

Implements the subsequent-measurement model of ASC 350-60 (ASU 2023-08):
holdings are remeasured to fair value at each reporting date with the change
recognised in net income. Alongside the primary ledger the engine keeps a
shadow measurement of every lot under the superseded cost-less-impairment
model (ASC 350-30), so the two models can be compared period by period and
the transition adjustment can be read off at any reporting date.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Sequence

from .models import (
    ZERO,
    Control,
    Holding,
    JournalLine,
    Lot,
    LotSnapshot,
    MarketAlternative,
    MarketQuote,
    PriceLow,
    RollforwardRow,
    Transaction,
    TxType,
    money,
)
from .principal_market import FairValueDetermination, determine_fair_value

ACCT_CRYPTO = "Crypto assets, at fair value"
ACCT_CASH = "Cash"
ACCT_FEES = "Transaction fee expense"
ACCT_GAIN = "Gain on crypto assets"
ACCT_LOSS = "Loss on crypto assets"
ACCT_INCOME = "Crypto rewards income"

COST_METHODS = ("fifo", "lifo", "hifo")


class InsufficientUnitsError(ValueError):
    """A disposal exceeds the units held."""


@dataclass
class _Movement:
    additions: Decimal = ZERO
    dispositions: Decimal = ZERO
    gains: Decimal = ZERO
    losses: Decimal = ZERO
    realized_gains: Decimal = ZERO
    realized_losses: Decimal = ZERO
    legacy_impairment: Decimal = ZERO
    legacy_disposal_gain: Decimal = ZERO

    def book(self, delta: Decimal) -> None:
        if delta > ZERO:
            self.gains += delta
        elif delta < ZERO:
            self.losses += -delta


@dataclass
class Result:
    cost_method: str
    capitalize_fees: bool = False
    journal: list[JournalLine] = field(default_factory=list)
    holdings: list[Holding] = field(default_factory=list)
    lots: list[LotSnapshot] = field(default_factory=list)
    rollforward: list[RollforwardRow] = field(default_factory=list)
    determinations: list[FairValueDetermination] = field(default_factory=list)
    alternatives: list[MarketAlternative] = field(default_factory=list)
    controls: list[Control] = field(default_factory=list)
    has_price_history: bool = False

    @property
    def period_ends(self) -> list[date]:
        return sorted({r.period_end for r in self.rollforward})

    @property
    def controls_passed(self) -> bool:
        return all(c.passed for c in self.controls)


class FairValueEngine:
    """Processes transactions and period-end quotes into ASC 350-60 outputs.

    cost_method: order in which lots are relieved on disposal. "fifo" and
        "lifo" are by acquisition order; "hifo" is a specific-identification
        policy that relieves the highest-cost units first.
    capitalize_fees: ASC 350-60 is silent on acquisition costs, so this is an
        accounting policy choice. Default is to expense them.
    """

    def __init__(self, cost_method: str = "fifo", capitalize_fees: bool = False):
        if cost_method not in COST_METHODS:
            raise ValueError(f"cost_method must be one of {COST_METHODS}")
        self.cost_method = cost_method
        self.capitalize_fees = capitalize_fees
        self._entry_no = 0
        self._lows: dict[str, list[PriceLow]] = {}

    # ------------------------------------------------------------------ run
    def run(
        self,
        transactions: Iterable[Transaction],
        quotes: Sequence[MarketQuote],
        period_ends: Iterable[date],
        price_lows: Iterable[PriceLow] = (),
    ) -> Result:
        ends = sorted(set(period_ends))
        txs = sorted(transactions, key=lambda t: (t.date, t.tx_id))
        if txs and (not ends or txs[-1].date > ends[-1]):
            raise ValueError(
                "Transactions exist after the last period end; add a later "
                "period end so every transaction falls in a reporting period."
            )
        ids = [t.tx_id for t in txs]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"Duplicate tx_id: {', '.join(dupes)}")

        self._entry_no = 0
        self._lows = defaultdict(list)
        for p in sorted(price_lows, key=lambda p: p.date):
            self._lows[p.asset].append(p)

        result = Result(
            cost_method=self.cost_method,
            capitalize_fees=self.capitalize_fees,
            has_price_history=bool(self._lows),
        )
        lots: dict[str, list[Lot]] = defaultdict(list)
        opening: dict[str, Decimal] = {}
        i = 0

        for end in ends:
            moves: dict[str, _Movement] = defaultdict(_Movement)
            while i < len(txs) and txs[i].date <= end:
                self._apply(txs[i], lots, result.journal, moves)
                i += 1

            closing: dict[str, Decimal] = {}
            legacy_closing: dict[str, Decimal] = {}
            for asset in sorted(a for a, ls in lots.items() if ls):
                det = determine_fair_value(quotes, asset, end)
                result.determinations.append(det)
                holding = self._remeasure(det, lots[asset], result.journal, moves[asset])
                result.holdings.append(holding)
                closing[asset] = holding.fair_value
                legacy_closing[asset] = holding.legacy_carrying
                result.lots += [
                    LotSnapshot(end, asset, l.lot_id, l.acquired, l.units, l.cost,
                                l.carrying, l.carrying - l.cost, l.legacy_carrying)
                    for l in lots[asset]
                ]
                result.alternatives += [
                    MarketAlternative(
                        end, asset, q.market, q.accessible, q.market == det.market,
                        q.price, money(holding.units * q.price),
                        money(holding.units * q.price) - money(holding.units * det.price),
                    )
                    for q in sorted(quotes, key=lambda q: -q.volume)
                    if q.asset == asset and q.date == end
                ]

            for asset in sorted(set(opening) | set(moves) | set(closing)):
                m = moves[asset]
                result.rollforward.append(RollforwardRow(
                    period_end=end,
                    asset=asset,
                    opening=opening.get(asset, ZERO),
                    additions=m.additions,
                    dispositions=m.dispositions,
                    gains=m.gains,
                    losses=m.losses,
                    closing=closing.get(asset, ZERO),
                    realized_gains=m.realized_gains,
                    realized_losses=m.realized_losses,
                    legacy_impairment=m.legacy_impairment,
                    legacy_disposal_gain=m.legacy_disposal_gain,
                    legacy_closing=legacy_closing.get(asset, ZERO),
                ))
            opening = closing

        from .controls import run_controls  # local import avoids a cycle

        result.controls = run_controls(result, txs)
        return result

    # --------------------------------------------------------------- events
    def _next_entry(self) -> str:
        self._entry_no += 1
        return f"JE-{self._entry_no:04d}"

    def _apply(self, tx: Transaction, lots, journal, moves) -> None:
        if tx.units <= ZERO or tx.amount < ZERO or tx.fee < ZERO:
            raise ValueError(f"{tx.tx_id}: units must be positive; amount and fee non-negative")
        if tx.type is TxType.BUY:
            self._buy(tx, lots, journal, moves)
        elif tx.type is TxType.SELL:
            self._sell(tx, lots, journal, moves)
        elif tx.type is TxType.SWAP:
            self._swap(tx, lots, journal, moves)
        else:
            self._reward(tx, lots, journal, moves)

    def _add_lot(self, tx: Transaction, asset: str, units: Decimal, cost: Decimal, lots, moves) -> None:
        lots[asset].append(Lot(tx.tx_id, asset, tx.date, units, cost, cost, cost, tx.date))
        moves[asset].additions += cost

    def _buy(self, tx: Transaction, lots, journal, moves) -> None:
        amount, fee = money(tx.amount), money(tx.fee)
        cost = amount + fee if self.capitalize_fees else amount
        self._add_lot(tx, tx.asset, tx.units, cost, lots, moves)

        eid, memo = self._next_entry(), f"{tx.tx_id}: buy {tx.units} {tx.asset}"
        journal.append(JournalLine(eid, tx.date, ACCT_CRYPTO, cost, ZERO, memo, tx.asset))
        if fee and not self.capitalize_fees:
            journal.append(JournalLine(eid, tx.date, ACCT_FEES, fee, ZERO, memo))
        journal.append(JournalLine(eid, tx.date, ACCT_CASH, ZERO, amount + fee, memo))

    def _reward(self, tx: Transaction, lots, journal, moves) -> None:
        value, fee = money(tx.amount), money(tx.fee)
        self._add_lot(tx, tx.asset, tx.units, value, lots, moves)

        eid, memo = self._next_entry(), f"{tx.tx_id}: receive {tx.units} {tx.asset} as reward"
        journal.append(JournalLine(eid, tx.date, ACCT_CRYPTO, value, ZERO, memo, tx.asset))
        if fee:
            journal.append(JournalLine(eid, tx.date, ACCT_FEES, fee, ZERO, memo))
        journal.append(JournalLine(eid, tx.date, ACCT_INCOME, ZERO, value, memo))
        if fee:
            journal.append(JournalLine(eid, tx.date, ACCT_CASH, ZERO, fee, memo))

    def _sell(self, tx: Transaction, lots, journal, moves) -> None:
        proceeds, fee = money(tx.amount), money(tx.fee)
        carrying_out, delta = self._dispose(tx, proceeds, lots[tx.asset], moves[tx.asset])

        eid, memo = self._next_entry(), f"{tx.tx_id}: sell {tx.units} {tx.asset}"
        journal.append(JournalLine(eid, tx.date, ACCT_CASH, proceeds - fee, ZERO, memo))
        if fee:
            journal.append(JournalLine(eid, tx.date, ACCT_FEES, fee, ZERO, memo))
        if delta < ZERO:
            journal.append(JournalLine(eid, tx.date, ACCT_LOSS, -delta, ZERO, memo))
        journal.append(JournalLine(eid, tx.date, ACCT_CRYPTO, ZERO, carrying_out, memo, tx.asset))
        if delta > ZERO:
            journal.append(JournalLine(eid, tx.date, ACCT_GAIN, ZERO, delta, memo))

    def _swap(self, tx: Transaction, lots, journal, moves) -> None:
        if not tx.to_asset or tx.to_units <= ZERO:
            raise ValueError(f"{tx.tx_id}: a swap needs to_asset and a positive to_units")
        if tx.to_asset == tx.asset:
            raise ValueError(f"{tx.tx_id}: a swap must be between two different assets")
        value, fee = money(tx.amount), money(tx.fee)
        carrying_out, delta = self._dispose(tx, value, lots[tx.asset], moves[tx.asset])
        self._add_lot(tx, tx.to_asset, tx.to_units, value, lots, moves)

        eid = self._next_entry()
        memo = f"{tx.tx_id}: swap {tx.units} {tx.asset} for {tx.to_units} {tx.to_asset}"
        journal.append(JournalLine(eid, tx.date, ACCT_CRYPTO, value, ZERO, memo, tx.to_asset))
        if fee:
            journal.append(JournalLine(eid, tx.date, ACCT_FEES, fee, ZERO, memo))
        if delta < ZERO:
            journal.append(JournalLine(eid, tx.date, ACCT_LOSS, -delta, ZERO, memo))
        journal.append(JournalLine(eid, tx.date, ACCT_CRYPTO, ZERO, carrying_out, memo, tx.asset))
        if delta > ZERO:
            journal.append(JournalLine(eid, tx.date, ACCT_GAIN, ZERO, delta, memo))
        if fee:
            journal.append(JournalLine(eid, tx.date, ACCT_CASH, ZERO, fee, memo))

    # ------------------------------------------------------------ mechanics
    def _relief_order(self, lots: list[Lot]) -> list[Lot]:
        if self.cost_method == "fifo":
            return list(lots)
        if self.cost_method == "lifo":
            return list(reversed(lots))
        return sorted(lots, key=lambda l: -l.unit_cost)  # hifo; stable on ties

    def _dispose(
        self, tx: Transaction, proceeds: Decimal, lots: list[Lot], move: _Movement
    ) -> tuple[Decimal, Decimal]:
        """Relieve lots for a sale or swap. Returns (carrying relieved, P&L delta)."""
        held = sum((l.units for l in lots), ZERO)
        if tx.units > held:
            raise InsufficientUnitsError(
                f"{tx.tx_id}: cannot dispose of {tx.units} {tx.asset}; only {held} held"
            )
        # Legacy model: impairment is assessed up to the disposal date first.
        for lot in lots:
            self._impair(lot, tx.date, move)

        remaining, cost_out, carrying_out, legacy_out = tx.units, ZERO, ZERO, ZERO
        for lot in self._relief_order(lots):
            if remaining == ZERO:
                break
            take = min(lot.units, remaining)
            if take == lot.units:
                c, v, g = lot.cost, lot.carrying, lot.legacy_carrying
            else:
                share = take / lot.units
                c, v, g = money(lot.cost * share), money(lot.carrying * share), money(lot.legacy_carrying * share)
            lot.units -= take
            lot.cost -= c
            lot.carrying -= v
            lot.legacy_carrying -= g
            cost_out += c
            carrying_out += v
            legacy_out += g
            remaining -= take
        lots[:] = [l for l in lots if l.units > ZERO]

        delta = proceeds - carrying_out  # P&L effect in this period
        realized = proceeds - cost_out   # cumulative, for disclosure
        move.dispositions += proceeds
        move.book(delta)
        if realized >= ZERO:
            move.realized_gains += realized
        else:
            move.realized_losses += -realized
        move.legacy_disposal_gain += proceeds - legacy_out
        return carrying_out, delta

    def _impair(self, lot: Lot, through: date, move: _Movement, closing_price: Decimal | None = None) -> None:
        """Write a lot's legacy carrying amount down to the lowest observed value.

        Impairment losses are never reversed, which is the asymmetry ASU 2023-08
        removed.
        """
        prices = [p.low for p in self._lows.get(lot.asset, ()) if lot.legacy_tested < p.date <= through]
        if closing_price is not None:
            prices.append(closing_price)
        lot.legacy_tested = max(lot.legacy_tested, through)
        if not prices:
            return
        floor = money(lot.units * min(prices))
        if floor < lot.legacy_carrying:
            move.legacy_impairment += lot.legacy_carrying - floor
            lot.legacy_carrying = floor

    def _remeasure(
        self, det: FairValueDetermination, lots: list[Lot], journal, move: _Movement
    ) -> Holding:
        gain = loss = ZERO
        for lot in lots:
            self._impair(lot, det.date, move, det.price)
            new = money(lot.units * det.price)
            delta = new - lot.carrying
            lot.carrying = new
            move.book(delta)
            if delta > ZERO:
                gain += delta
            else:
                loss += -delta

        if gain or loss:
            eid = self._next_entry()
            memo = (
                f"Remeasure {det.asset} to fair value at {det.price} "
                f"({det.market}, {det.basis} market)"
            )
            # Gains and losses are kept gross, matching the rollforward.
            if gain:
                journal.append(JournalLine(eid, det.date, ACCT_CRYPTO, gain, ZERO, memo, det.asset))
                journal.append(JournalLine(eid, det.date, ACCT_GAIN, ZERO, gain, memo))
            if loss:
                journal.append(JournalLine(eid, det.date, ACCT_LOSS, loss, ZERO, memo))
                journal.append(JournalLine(eid, det.date, ACCT_CRYPTO, ZERO, loss, memo, det.asset))

        return Holding(
            period_end=det.date,
            asset=det.asset,
            units=sum((l.units for l in lots), ZERO),
            cost_basis=sum((l.cost for l in lots), ZERO),
            fair_value=sum((l.carrying for l in lots), ZERO),
            legacy_carrying=sum((l.legacy_carrying for l in lots), ZERO),
            price=det.price,
            market=det.market,
            basis=det.basis,
            level=det.level,
        )
