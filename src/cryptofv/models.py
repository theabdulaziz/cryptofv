"""Core data types. All monetary values are Decimal; floats never touch the ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

CENT = Decimal("0.01")
ZERO = Decimal("0")


def money(value: Decimal) -> Decimal:
    """Round to cents, half-up (the convention used throughout the ledger)."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


class TxType(str, Enum):
    BUY = "buy"        # acquire for cash
    SELL = "sell"      # dispose for cash
    SWAP = "swap"      # exchange one crypto asset for another
    REWARD = "reward"  # staking reward, airdrop, mining receipt: income at fair value


@dataclass(frozen=True)
class Transaction:
    """One economic event.

    amount is always in the reporting currency:
      buy / sell  gross cash consideration, before fees
      swap        fair value of the exchange at the transaction date
      reward      fair value of the units received
    For a swap, `asset`/`units` are given up and `to_asset`/`to_units` received.
    fee is always a cash fee.
    """

    tx_id: str
    date: date
    asset: str
    type: TxType
    units: Decimal
    amount: Decimal
    fee: Decimal = ZERO
    to_asset: str = ""
    to_units: Decimal = ZERO


@dataclass(frozen=True)
class MarketQuote:
    """A period-end price for one asset in one market.

    accessible: whether the reporting entity can transact in this market at
    the measurement date (ASC 820-10-35-6A). volume is any consistent activity
    measure (e.g. trailing 30-day USD volume).
    """

    date: date
    asset: str
    market: str
    price: Decimal
    volume: Decimal
    accessible: bool = True
    fee_bps: Decimal = ZERO


@dataclass(frozen=True)
class PriceLow:
    """Lowest price observed for an asset up to `date` since the previous row.

    Only the legacy impairment model uses this. Daily rows give exact results;
    sparser rows are accepted but can impair a lot for a low that predates it.
    """

    date: date
    asset: str
    low: Decimal


@dataclass
class Lot:
    """A layer of units acquired in a single transaction.

    Three measurement bases are carried side by side:
      cost            historical cost, never remeasured
      carrying        ASC 350-60 fair value at the last reporting date
      legacy_carrying cost less impairment under the superseded ASC 350-30 model
    """

    lot_id: str
    asset: str
    acquired: date
    units: Decimal
    cost: Decimal
    carrying: Decimal
    legacy_carrying: Decimal
    legacy_tested: date  # impairment has been assessed through this date

    @property
    def unit_cost(self) -> Decimal:
        return self.cost / self.units


@dataclass(frozen=True)
class JournalLine:
    entry_id: str
    date: date
    account: str
    debit: Decimal
    credit: Decimal
    memo: str
    asset: str = ""  # subledger tag for crypto asset lines


@dataclass(frozen=True)
class Holding:
    """One row of the significant-holdings disclosure (ASC 350-60-50-1)."""

    period_end: date
    asset: str
    units: Decimal
    cost_basis: Decimal
    fair_value: Decimal
    legacy_carrying: Decimal
    price: Decimal
    market: str
    basis: str
    level: str


@dataclass(frozen=True)
class LotSnapshot:
    """An open lot at a reporting date: the subledger behind a Holding."""

    period_end: date
    asset: str
    lot_id: str
    acquired: date
    units: Decimal
    cost_basis: Decimal
    fair_value: Decimal
    unrealized: Decimal
    legacy_carrying: Decimal


@dataclass(frozen=True)
class RollforwardRow:
    """One asset's activity for one reporting period (ASC 350-60-50-2).

    The legacy_* fields are the same period under cost-less-impairment.
    """

    period_end: date
    asset: str
    opening: Decimal
    additions: Decimal
    dispositions: Decimal
    gains: Decimal
    losses: Decimal
    closing: Decimal
    realized_gains: Decimal
    realized_losses: Decimal
    legacy_impairment: Decimal
    legacy_disposal_gain: Decimal  # signed
    legacy_closing: Decimal


@dataclass(frozen=True)
class MarketAlternative:
    """What a holding would be worth if measured in a given market."""

    period_end: date
    asset: str
    market: str
    accessible: bool
    selected: bool
    price: Decimal
    fair_value: Decimal
    difference: Decimal  # vs the reported fair value


@dataclass(frozen=True)
class Control:
    name: str
    passed: bool
    detail: str
