"""CSV loading and writing."""

from __future__ import annotations

import csv
from dataclasses import asdict, fields
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import ZERO, MarketQuote, PriceLow, Transaction, TxType

_TRUE = {"1", "true", "yes", "y"}


def _rows(path: Path, required: set[str]):
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing column(s): {', '.join(sorted(missing))}")
        for n, row in enumerate(reader, start=2):
            if any((v or "").strip() for v in row.values()):  # skip blank lines
                yield n, row


def _dec(row: dict, key: str, path: Path, line: int, default: Decimal | None = None) -> Decimal:
    raw = (row.get(key) or "").strip().replace(",", "")
    if not raw:
        if default is not None:
            return default
        raise ValueError(f"{path}:{line}: missing value for '{key}'")
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{path}:{line}: '{raw}' is not a number ({key})") from None


def _date(row: dict, path: Path, line: int) -> date:
    raw = (row.get("date") or "").strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{path}:{line}: '{raw}' is not a YYYY-MM-DD date") from None


def load_transactions(path: str | Path) -> list[Transaction]:
    path = Path(path)
    out = []
    for n, row in _rows(path, {"tx_id", "date", "asset", "type", "units", "amount"}):
        kind = row["type"].strip().lower()
        try:
            tx_type = TxType(kind)
        except ValueError:
            valid = ", ".join(t.value for t in TxType)
            raise ValueError(f"{path}:{n}: unknown type '{kind}' (expected one of: {valid})") from None
        out.append(Transaction(
            tx_id=row["tx_id"].strip(),
            date=_date(row, path, n),
            asset=row["asset"].strip().upper(),
            type=tx_type,
            units=_dec(row, "units", path, n),
            amount=_dec(row, "amount", path, n),
            fee=_dec(row, "fee", path, n, ZERO),
            to_asset=(row.get("to_asset") or "").strip().upper(),
            to_units=_dec(row, "to_units", path, n, ZERO),
        ))
    return out


def load_quotes(path: str | Path) -> list[MarketQuote]:
    path = Path(path)
    return [
        MarketQuote(
            date=_date(row, path, n),
            asset=row["asset"].strip().upper(),
            market=row["market"].strip(),
            price=_dec(row, "price", path, n),
            volume=_dec(row, "volume", path, n, ZERO),
            accessible=(row.get("accessible") or "true").strip().lower() in _TRUE,
            fee_bps=_dec(row, "fee_bps", path, n, ZERO),
        )
        for n, row in _rows(path, {"date", "asset", "market", "price"})
    ]


def load_price_lows(path: str | Path) -> list[PriceLow]:
    path = Path(path)
    return [
        PriceLow(_date(row, path, n), row["asset"].strip().upper(), _dec(row, "low", path, n))
        for n, row in _rows(path, {"date", "asset", "low"})
    ]


def save_transactions(path: str | Path, txs: list[Transaction]) -> None:
    """Rewrite the transactions file. Written to a temp file first so a crash cannot truncate the ledger."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tx_id", "date", "asset", "type", "units", "amount", "fee", "to_asset", "to_units"])
        for t in sorted(txs, key=lambda t: (t.date, t.tx_id)):
            w.writerow([t.tx_id, t.date.isoformat(), t.asset, t.type.value, format(t.units, "f"),
                        format(t.amount, "f"), format(t.fee, "f"), t.to_asset,
                        format(t.to_units, "f") if t.to_asset else ""])
    tmp.replace(path)


def save_quotes(path: str | Path, quotes: list[MarketQuote]) -> None:
    """Rewrite the period-end quotes file, via a temp file so a crash cannot truncate it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "asset", "market", "price", "volume", "accessible", "fee_bps"])
        for q in sorted(quotes, key=lambda q: (q.date, q.asset, q.market)):
            w.writerow([q.date.isoformat(), q.asset, q.market, format(q.price, "f"), format(q.volume, "f"),
                        "true" if q.accessible else "false", format(q.fee_bps, "f")])
    tmp.replace(path)


def write_csv(path: str | Path, rows: list) -> None:
    """Write a list of dataclass instances (all the same type) to CSV."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[fl.name for fl in fields(rows[0])])
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))
