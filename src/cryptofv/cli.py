"""Command-line interface: cryptofv run --transactions ... --quotes ..."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import analytics
from .engine import COST_METHODS, FairValueEngine
from .feeds import EXCHANGES, ExchangeFeed, SimulatedFeed
from .html_report import to_html
from .io import load_price_lows, load_quotes, load_transactions, write_csv
from .report import to_markdown


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cryptofv",
        description="Fair value measurement and disclosures for crypto assets (ASC 350-60 / ASC 820).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="process transactions and produce journal entries, disclosures and a dashboard")
    run.add_argument("--transactions", required=True, help="CSV: tx_id,date,asset,type,units,amount[,fee,to_asset,to_units]")
    run.add_argument("--quotes", required=True, help="CSV: date,asset,market,price[,volume,accessible,fee_bps]")
    run.add_argument("--price-lows", help="CSV: date,asset,low. Price history for the legacy impairment comparison")
    run.add_argument("--period-ends", help="comma-separated ISO dates (default: every date in the quotes file)")
    run.add_argument("--method", choices=COST_METHODS, default="fifo", help="cost basis method (default: fifo)")
    run.add_argument("--capitalize-fees", action="store_true", help="add purchase fees to cost basis instead of expensing")
    run.add_argument("--adoption-date", help="ISO date ASU 2023-08 is adopted; reports the transition adjustment")
    run.add_argument("--out", default="out", help="output directory (default: out)")

    serve = sub.add_parser("serve", help="open the live web app: holdings marked to market at current prices")
    serve.add_argument("--transactions", required=True, help="transactions CSV (created if it does not exist)")
    serve.add_argument("--quotes", help="historical period-end quotes CSV; each date becomes a prior reporting period")
    serve.add_argument("--price-lows", help="price history CSV for the legacy impairment comparison")
    serve.add_argument("--method", choices=COST_METHODS, default="fifo")
    serve.add_argument("--capitalize-fees", action="store_true")
    serve.add_argument("--demo", action="store_true", help="simulate prices around the --quotes file instead of calling exchanges")
    serve.add_argument("--exchanges", default="coinbase,kraken,bitstamp", help=f"comma-separated, from: {', '.join(EXCHANGES)}")
    serve.add_argument("--inaccessible", default="", help="exchanges to quote but treat as markets the entity cannot access")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--refresh", type=int, help="seconds between updates (default: 15 live, 3 demo)")
    serve.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    return parser


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _serve(args) -> int:
    import webbrowser

    from .live import LiveSession
    from .server import make_server

    try:
        if args.demo:
            if not args.quotes:
                raise ValueError("--demo needs --quotes to simulate prices from")
            feed = SimulatedFeed(load_quotes(args.quotes))
        else:
            feed = ExchangeFeed(_split(args.exchanges), _split(args.inaccessible))
        refresh = max(1, args.refresh or (3 if args.demo else 15))
        session = LiveSession(args.transactions, feed, args.quotes, args.price_lows, args.method, args.capitalize_fees)
        server = make_server(session, args.host, args.port, refresh)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    url = f"http://{args.host}:{server.server_address[1]}/"
    print(f"cryptofv is running at {url}  ({feed.label}; refresh every {refresh}s)")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    try:
        txs = load_transactions(args.transactions)
        quotes = load_quotes(args.quotes)
        lows = load_price_lows(args.price_lows) if args.price_lows else []
        if args.period_ends:
            ends = [date.fromisoformat(d.strip()) for d in args.period_ends.split(",")]
        else:
            ends = sorted({q.date for q in quotes})
        adoption = date.fromisoformat(args.adoption_date) if args.adoption_date else None
        result = FairValueEngine(args.method, args.capitalize_fees).run(txs, quotes, ends, lows)
        markdown = to_markdown(result, adoption)
        html = to_html(result, adoption)
    except (ValueError, KeyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out = Path(args.out)
    write_csv(out / "journal_entries.csv", result.journal)
    write_csv(out / "holdings.csv", result.holdings)
    write_csv(out / "lots.csv", result.lots)
    write_csv(out / "rollforward.csv", result.rollforward)
    write_csv(out / "fair_value_determinations.csv", result.determinations)
    write_csv(out / "market_alternatives.csv", result.alternatives)
    write_csv(out / "model_comparison.csv", analytics.compare_models(result))
    write_csv(out / "trial_balance.csv", analytics.trial_balance(result.journal))
    (out / "report.md").write_text(markdown, encoding="utf-8")
    (out / "dashboard.html").write_text(html, encoding="utf-8")

    passed = sum(c.passed for c in result.controls)
    print(f"Processed {len(txs)} transactions across {len(ends)} period(s). Output in {out}/")
    print(f"Controls: {passed} of {len(result.controls)} passed")
    for c in result.controls:
        if not c.passed:
            print(f"  FAIL {c.name}: {c.detail}", file=sys.stderr)
    return 0 if result.controls_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
