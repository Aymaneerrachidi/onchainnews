from __future__ import annotations

import asyncio
import json
import logging
import threading
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from brief.config import Settings
from brief.delivery import write_html
from brief.engine import build_brief
from brief.ledger import open_ledger
from brief.render.html import render_html


log = logging.getLogger("brief.interface")
UTC = timezone.utc


class InterfaceState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.message = "Ready."
        self.started_at: str | None = None
        self.completed_at: str | None = None
        self.error: str | None = None

    def payload(self, stats: dict[str, int]) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "message": self.message,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "error": self.error,
                "stats": stats,
            }


def _run_report(settings: Settings, state: InterfaceState) -> None:
    with state.lock:
        state.running = True
        state.message = "Fetching sources and rebuilding the brief."
        state.started_at = datetime.now(UTC).isoformat()
        state.error = None
    ledger = open_ledger(settings)
    try:
        brief = asyncio.run(build_brief(settings, ledger, commit=True))
        write_html(settings.path("run", "html_path"), render_html(brief))
        with state.lock:
            state.message = "Brief complete."
            state.completed_at = datetime.now(UTC).isoformat()
    except Exception as exc:
        log.exception("Interface-triggered brief failed")
        with state.lock:
            state.error = str(exc)
            state.message = "Brief failed."
    finally:
        ledger.close()
        with state.lock:
            state.running = False


def _handler(settings: Settings, state: InterfaceState):
    report_path = settings.path("run", "html_path")

    class Handler(BaseHTTPRequestHandler):
        server_version = "SolanaBrief/1"

        def log_message(self, format: str, *args: object) -> None:
            log.info("interface_http client=%s message=%s", self.client_address[0], format % args)

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/state":
                ledger = open_ledger(settings)
                try:
                    payload = state.payload(ledger.stats())
                finally:
                    ledger.close()
                self._json(HTTPStatus.OK, payload)
                return
            if path not in {"/", "/index.html"}:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            if not report_path.exists():
                body = b"<!doctype html><meta charset=utf-8><title>Solana Brief</title><p>No report exists yet. Run solana-brief run first.</p>"
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, body, "text/html; charset=utf-8")
                return
            self._send(HTTPStatus.OK, report_path.read_bytes(), "text/html; charset=utf-8")

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/api/run":
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and urlsplit(origin).netloc != host:
                self._json(HTTPStatus.FORBIDDEN, {"error": "Cross-origin request rejected"})
                return
            with state.lock:
                if state.running:
                    self._json(HTTPStatus.CONFLICT, {"error": "A brief run is already active"})
                    return
                state.running = True
                state.message = "Run queued."
                state.error = None
            worker = threading.Thread(target=_run_report, args=(settings, state), daemon=True, name="brief-run")
            worker.start()
            self._json(HTTPStatus.ACCEPTED, {"started": True})

    return Handler


def serve_interface(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The interface may only bind to the local machine")
    state = InterfaceState()
    server = ThreadingHTTPServer((host, port), _handler(settings, state))
    url = f"http://{host}:{port}"
    log.info("interface_started url=%s", url)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
