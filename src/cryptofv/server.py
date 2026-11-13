"""Local web app for live valuation. Standard library only.

    cryptofv serve --transactions my.csv            live exchange prices
    cryptofv serve --transactions my.csv --demo     simulated prices, works offline

The server binds to 127.0.0.1 by default and has no authentication: it is a
single-user tool for your own machine, not something to expose to a network.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import analytics
from .html_report import to_html
from .live import LiveSession

_INDEX = Path(__file__).parent / "web" / "index.html"
_MAX_BODY = 64 * 1024
_CSP = "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"


def _csv(rows: list) -> bytes:
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=[f.name for f in fields(rows[0])])
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))
    return buf.getvalue().encode("utf-8")


def make_handler(session: LiveSession, refresh: int, allowed_hosts: set[str]):
    exports = {
        "journal_entries": lambda r: r.journal,
        "holdings": lambda r: r.holdings,
        "lots": lambda r: r.lots,
        "rollforward": lambda r: r.rollforward,
        "model_comparison": analytics.compare_models,
        "market_alternatives": lambda r: r.alternatives,
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "cryptofv"

        def log_message(self, *args) -> None:  # keep the terminal quiet
            pass

        # ------------------------------------------------------------ plumbing
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", _CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            """Reject requests addressed to an unexpected Host (DNS rebinding)."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in allowed_hosts

        def _origin_ok(self) -> bool:
            """Writes must come from this page, not from another site open in the browser."""
            origin = self.headers.get("Origin")
            return origin is None or origin == f"http://{self.headers.get('Host')}"

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_BODY:
                raise ValueError("request too large")
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                raise ValueError("Content-Type must be application/json")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        # -------------------------------------------------------------- routes
        def do_GET(self) -> None:
            if not self._host_ok():
                return self._json(403, {"error": "unexpected Host header"})
            path = urlsplit(self.path).path
            if path == "/":
                return self._send(200, _INDEX.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(200, {**session.state(), "refresh": refresh})
            try:
                if path.startswith("/closes/") and path.endswith(".html"):
                    memo = session.memo(path[len("/closes/"):-5])
                    if memo is not None:
                        return self._send(200, memo.encode("utf-8"), "text/html; charset=utf-8")
                if path == "/report.html":
                    return self._send(200, to_html(session.result()).encode("utf-8"), "text/html; charset=utf-8")
                if path.startswith("/api/export/") and path.endswith(".csv"):
                    name = path[len("/api/export/"):-4]
                    if name in exports:
                        return self._send(200, _csv(exports[name](session.result())), "text/csv; charset=utf-8",
                                          {"Content-Disposition": f'attachment; filename="{name}.csv"'})
            except (ValueError, OSError) as exc:
                return self._json(400, {"error": str(exc)})
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not (self._host_ok() and self._origin_ok()):
                return self._json(403, {"error": "cross-origin request refused"})
            path = urlsplit(self.path).path
            try:
                data = self._body()
                if path == "/api/transactions":
                    tx = session.add_transaction(data)
                    return self._json(201, {"tx_id": tx.tx_id})
                if path == "/api/close":
                    record = session.close_period(str(data.get("prepared_by", "")), str(data.get("note", "")))
                    return self._json(201, {"period_end": record.period_end.isoformat(),
                                            "fingerprint": record.fingerprint})
                if path == "/api/settings":
                    session.set_cost_method(str(data.get("cost_method", "")))
                    return self._json(200, {"ok": True})
            except (ValueError, OSError) as exc:  # includes malformed JSON
                return self._json(400, {"error": str(exc)})
            self._json(404, {"error": "not found"})

        def do_DELETE(self) -> None:
            if not (self._host_ok() and self._origin_ok()):
                return self._json(403, {"error": "cross-origin request refused"})
            path = urlsplit(self.path).path
            if not path.startswith("/api/transactions/"):
                return self._json(404, {"error": "not found"})
            tx_id = unquote(path[len("/api/transactions/"):])
            try:
                session.delete_transaction(tx_id)
            except KeyError:
                return self._json(404, {"error": f"no transaction {tx_id}"})
            except (ValueError, OSError) as exc:
                return self._json(400, {"error": str(exc)})
            self._json(200, {"ok": True})

    return Handler


def make_server(session: LiveSession, host: str = "127.0.0.1", port: int = 8765, refresh: int = 15) -> ThreadingHTTPServer:
    allowed = {host.lower(), "localhost", "127.0.0.1", "::1"}
    server = ThreadingHTTPServer((host, port), make_handler(session, refresh, allowed))
    server.daemon_threads = True
    return server
