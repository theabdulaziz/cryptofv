"""Price feeds for live valuation.

A feed turns a set of asset symbols into MarketQuote rows dated "now", one per
market, in the same shape the engine uses for period-end quotes. Two feeds are
provided:

ExchangeFeed   polls the public REST tickers of real exchanges (no API key).
SimulatedFeed  a seeded random walk around a set of base quotes, for demos,
               tests and offline use. Never a substitute for market data.
"""

from __future__ import annotations

import json
import math
import random
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from typing import Callable, Iterable, Protocol

from .models import ZERO, MarketQuote

_SYMBOL = re.compile(r"^[A-Z0-9]{2,10}$")
JsonGetter = Callable[[str], dict]


class PriceFeed(Protocol):
    label: str
    simulated: bool

    def quotes(self, assets: Iterable[str], on: date) -> tuple[list[MarketQuote], list[str]]:
        """Return (quotes dated `on`, human-readable warnings)."""


# ------------------------------------------------------------------ exchanges
def _http_get(url: str, timeout: float = 6.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "cryptofv", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _coinbase(asset: str, get: JsonGetter) -> tuple[Decimal, Decimal]:
    d = get(f"https://api.exchange.coinbase.com/products/{asset}-USD/stats")
    return Decimal(d["last"]), Decimal(d["volume"])  # volume: 24h, base units


def _kraken(asset: str, get: JsonGetter) -> tuple[Decimal, Decimal]:
    symbol = {"BTC": "XBT", "DOGE": "XDG"}.get(asset, asset)
    d = get(f"https://api.kraken.com/0/public/Ticker?pair={symbol}USD")
    if d.get("error"):
        raise ValueError("; ".join(d["error"]))
    ticker = next(iter(d["result"].values()))  # key is Kraken's own pair name
    return Decimal(ticker["c"][0]), Decimal(ticker["v"][1])  # last trade, 24h volume


def _bitstamp(asset: str, get: JsonGetter) -> tuple[Decimal, Decimal]:
    d = get(f"https://www.bitstamp.net/api/v2/ticker/{asset.lower()}usd/")
    return Decimal(d["last"]), Decimal(d["volume"])


EXCHANGES: dict[str, tuple[str, Callable[[str, JsonGetter], tuple[Decimal, Decimal]]]] = {
    "coinbase": ("Coinbase", _coinbase),
    "kraken": ("Kraken", _kraken),
    "bitstamp": ("Bitstamp", _bitstamp),
}


class ExchangeFeed:
    """Live USD spot prices and 24-hour volume from public exchange tickers.

    Volume is converted to USD (base volume x last price) so markets are
    compared on one activity measure when the principal market is chosen.
    Results are cached for `ttl` seconds to stay well inside rate limits.
    """

    simulated = False

    def __init__(
        self,
        exchanges: Iterable[str] = ("coinbase", "kraken", "bitstamp"),
        inaccessible: Iterable[str] = (),
        ttl: float = 10.0,
        get: JsonGetter = _http_get,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.exchanges = [e.lower() for e in exchanges]
        unknown = [e for e in self.exchanges if e not in EXCHANGES]
        if unknown:
            raise ValueError(f"Unknown exchange(s): {', '.join(unknown)}. Available: {', '.join(EXCHANGES)}")
        self.inaccessible = {e.lower() for e in inaccessible}
        self.ttl, self._get, self._clock = ttl, get, clock
        self._lock = threading.Lock()
        self._cache: tuple[float, frozenset[str], list[tuple], list[str]] | None = None
        self.label = ", ".join(EXCHANGES[e][0] for e in self.exchanges)

    def _fetch_one(self, exchange: str, asset: str):
        name, adapter = EXCHANGES[exchange]
        try:
            price, base_volume = adapter(asset, self._get)
            if price <= ZERO:
                raise ValueError("non-positive price")
            return (asset, name, price, price * base_volume, exchange not in self.inaccessible), None
        except urllib.error.HTTPError as exc:
            return None, f"{name} has no {asset}-USD market (HTTP {exc.code})"
        except Exception as exc:  # network errors, malformed payloads
            return None, f"{name}: {asset} unavailable ({type(exc).__name__})"

    def quotes(self, assets: Iterable[str], on: date) -> tuple[list[MarketQuote], list[str]]:
        wanted = frozenset(assets)
        with self._lock:
            now = self._clock()
            if self._cache and self._cache[1] == wanted and now - self._cache[0] < self.ttl:
                _, _, rows, warnings = self._cache
            else:
                warnings = [f"{a}: not a valid ticker symbol, skipped" for a in sorted(wanted) if not _SYMBOL.match(a)]
                jobs = [(e, a) for a in sorted(wanted) if _SYMBOL.match(a) for e in self.exchanges]
                rows = []
                if jobs:
                    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
                        for row, warning in pool.map(lambda j: self._fetch_one(*j), jobs):
                            if row:
                                rows.append(row)
                            else:
                                warnings.append(warning)
                self._cache = (now, wanted, rows, warnings)
        quotes = [MarketQuote(on, a, m, p, v, acc) for a, m, p, v, acc in rows]
        return quotes, list(warnings)


# ------------------------------------------------------------------ simulated
class SimulatedFeed:
    """Random-walk prices around base quotes. For demos and tests only.

    Markets for one asset share a common shock plus a small idiosyncratic one,
    so they drift together but never match exactly, like real venues.
    """

    simulated = True
    label = "Simulated prices"

    def __init__(self, base: Iterable[MarketQuote], seed: int = 7, volatility: float = 0.004,
                 interval: float = 2.0, clock: Callable[[], float] = time.monotonic):
        latest: dict[tuple[str, str], MarketQuote] = {}
        for q in sorted(base, key=lambda q: q.date):
            latest[(q.asset, q.market)] = q
        self._base = list(latest.values())
        self._level = {q.asset: 0.0 for q in self._base}        # log drift per asset
        self._basis = {(q.asset, q.market): 0.0 for q in self._base}
        self._rng = random.Random(seed)
        self.volatility, self.interval, self._clock = volatility, interval, clock
        self._last_step: float | None = None
        self._lock = threading.Lock()

    def step(self) -> None:
        for asset in self._level:
            self._level[asset] += self._rng.gauss(0, self.volatility)
        for key in self._basis:
            self._basis[key] = 0.8 * self._basis[key] + self._rng.gauss(0, self.volatility / 10)

    def quotes(self, assets: Iterable[str], on: date) -> tuple[list[MarketQuote], list[str]]:
        wanted = set(assets)
        with self._lock:
            now = self._clock()
            if self._last_step is None or now - self._last_step >= self.interval:
                self.step()
                self._last_step = now
            out = []
            for q in self._base:
                if q.asset not in wanted:
                    continue
                factor = Decimal(repr(math.exp(self._level[q.asset] + self._basis[(q.asset, q.market)])))
                out.append(MarketQuote(on, q.asset, q.market, (q.price * factor).quantize(Decimal("0.01")),
                                       q.volume, q.accessible, q.fee_bps))
        missing = sorted(wanted - {q.asset for q in out})
        return out, [f"{a}: no base quote to simulate from" for a in missing]
