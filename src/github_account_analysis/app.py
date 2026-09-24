"""Local browser UI and JSON/PDF HTTP API for public-profile analysis."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Mapping
from urllib.parse import urlparse

from .github_api import GitHubAPIError, GitHubClient, collect_public_data, normalize_login
from .report import build_report, render_pdf

WEB_ROOT = Path(__file__).resolve().parent / "web"
WEB_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}


def _since(timeframe: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    choices = {"30_days": timedelta(days=30), "90_days": timedelta(days=90), "1_year": timedelta(days=365)}
    if timeframe == "all_available":
        return None
    if timeframe not in choices:
        raise ValueError("timeframe must be 30_days, 90_days, 1_year, or all_available")
    return now - choices[timeframe]


def analyze(payload: Mapping[str, Any], client: GitHubClient | None = None) -> Dict[str, Any]:
    """Validate a UI/API request and produce one evidence-bound report."""
    username = normalize_login(str(payload.get("username", "")))
    timeframe = str(payload.get("timeframe", "1_year"))
    scope = payload.get("scope", ["owned", "contributed"])
    if not isinstance(scope, list) or not set(scope).issubset({"owned", "contributed"}) or not scope:
        raise ValueError("scope must select owned, contributed, or both")
    api_client = client or GitHubClient()
    data = collect_public_data(username, _since(timeframe), scope, api_client)
    return build_report(
        {"username": username, "timeframe": timeframe, "scope": scope, "api_request_budget": api_client.max_requests},
        data,
    )


class ApplicationHandler(BaseHTTPRequestHandler):
    """Serve static UI assets and intentionally small local API endpoints."""

    server_version = "GithubAccountAnalysis/0.1"
    max_body_bytes = 2 * 1024 * 1024

    def log_message(self, format: str, *args: object) -> None:
        if os.environ.get("GAA_HTTP_LOG"):
            super().log_message(format, *args)

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, data: Mapping[str, Any]) -> None:
        self._send(status, json.dumps(data, indent=2).encode("utf-8"), "application/json; charset=utf-8")

    def _read_json(self) -> Mapping[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if not 0 < content_length <= self.max_body_bytes:
            raise ValueError("Request body must be between 1 byte and 2 MiB.")
        value = json.loads(self.rfile.read(content_length).decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("Request body must be a JSON object.")
        return value

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        asset = WEB_ASSETS.get(route)
        if asset is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        filename, content_type = asset
        path = WEB_ROOT / filename
        if not path.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        self._send(HTTPStatus.OK, path.read_bytes(), content_type)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        try:
            payload = self._read_json()
            if route == "/api/analyze":
                self._json(HTTPStatus.OK, analyze(payload))
            elif route == "/api/report.pdf":
                document = render_pdf(analyze(payload))
                self._send(HTTPStatus.OK, document, "application/pdf")
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Request body must contain valid JSON."})
        except (ValueError, GitHubAPIError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})


def main() -> None:
    host, port = os.environ.get("GAA_HOST", "127.0.0.1"), int(os.environ.get("GAA_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), ApplicationHandler)
    print(f"GitHub Account Analysis: http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
