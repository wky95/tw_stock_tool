#!/usr/bin/env python3
from __future__ import annotations

import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from twbacktest.api import default_controller
from twbacktest.integrations import build_broker, build_quote_provider, build_research_provider


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
MAX_BODY_BYTES = 20 * 1024 * 1024
API = default_controller(build_quote_provider(), build_broker(), build_research_provider())


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "TWBacktest/1.0"

    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            response = API.handle("GET", path, parse_qs(parsed.query))
            return self._json(response.payload, response.status)
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR not in target.parents or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                return self._json({"ok": False, "error": {"code": "invalid_body", "message": "請求內容為空或超過 20 MB"}}, 400)
            body = json.loads(self.rfile.read(length))
            response = API.handle("POST", parsed.path, parse_qs(parsed.query), body)
            self._json(response.payload, response.status)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"ok": False, "error": {"code": "invalid_json", "message": str(exc)}}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            print(f"Unexpected error: {exc!r}", file=sys.stderr)
            self._json({"ok": False, "error": {"code": "internal_error", "message": "伺服器發生未預期錯誤"}}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {format % args}")


def main():
    host = "127.0.0.1"
    port = 8000
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    server = ThreadingHTTPServer((host, port), RequestHandler)
    print(f"台股回測系統已啟動：http://{host}:{port}")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
