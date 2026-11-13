"""Principal / most advantageous market determination under ASC 820.

ASC 820-10-35-5 assumes the transaction takes place in the principal market:
the market with the greatest volume and level of activity for the asset *that
the entity can access*. Only when there is no principal market does the
entity fall back to the most advantageous market, i.e. the one that maximises
the amount received after transaction costs. In both cases the fair value is
the quoted price itself: transaction costs help choose the market but are not
deducted from the measurement (ASC 820-10-35-9B).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable

from .models import ZERO, MarketQuote

BPS = Decimal("10000")


class NoAccessibleMarketError(ValueError):
    """No market the entity can access quotes this asset on this date."""


@dataclass(frozen=True)
class FairValueDetermination:
    asset: str
    date: date
    market: str
    price: Decimal
    basis: str  # "principal" or "most advantageous"
    level: str
    rationale: str


def determine_fair_value(
    quotes: Iterable[MarketQuote], asset: str, on: date
) -> FairValueDetermination:
    """Pick the market and price used to measure `asset` at `on`."""
    all_quotes = [q for q in quotes if q.asset == asset and q.date == on]
    candidates = [q for q in all_quotes if q.accessible]
    if not candidates:
        raise NoAccessibleMarketError(
            f"No accessible market quote for {asset} on {on.isoformat()}"
        )

    excluded = sorted(q.market for q in all_quotes if not q.accessible)
    excluded_note = (
        f" Excluded as inaccessible: {', '.join(excluded)}." if excluded else ""
    )

    active = [q for q in candidates if q.volume > ZERO]
    if active:
        # Greatest volume wins; market name breaks ties deterministically.
        best = sorted(active, key=lambda q: (-q.volume, q.market))[0]
        return FairValueDetermination(
            asset=asset,
            date=on,
            market=best.market,
            price=best.price,
            basis="principal",
            level="Level 1",
            rationale=(
                f"{best.market} has the greatest volume among "
                f"{len(active)} accessible active market(s).{excluded_note}"
            ),
        )

    # No activity data at all: most advantageous market, net of costs.
    def net(q: MarketQuote) -> Decimal:
        return q.price * (1 - q.fee_bps / BPS)

    best = sorted(candidates, key=lambda q: (-net(q), q.market))[0]
    return FairValueDetermination(
        asset=asset,
        date=on,
        market=best.market,
        price=best.price,
        basis="most advantageous",
        level="Level 2 (judgment required)",
        rationale=(
            f"No volume data for any accessible market; {best.market} gives "
            f"the highest price net of transaction costs. The measurement uses "
            f"the unadjusted price.{excluded_note}"
        ),
    )
