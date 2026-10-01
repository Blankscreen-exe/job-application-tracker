"""The local web server: serves the page and a small JSON API over the database.

It only listens on 127.0.0.1. Every API call must carry the random token baked into the page, and
the Host header must be ours, so other websites open in your browser can't read or change your data.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import sys
import traceback
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import db, exporter, importer, paths, resumes

PAGE = Path(__file__).resolve().parent / "static" / "index.html"
MAX_BODY = 20 * 1024 * 1024
PORT_ATTEMPTS = 20


class TrackerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, db_file: Path, cfg: dict, resumes_dir: Path | None = None):
        super().__init__(address, Handler)
        self.db_file = db_file
        self.cfg = cfg
        self.resumes_dir = resumes_dir or paths.resumes_dir()
        self.token = secrets.token_urlsafe(24)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/"


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 with a Content-Length on every response, so bodies are never cut short on Windows.
    protocol_version = "HTTP/1.1"
    timeout = 30
    server: TrackerServer

    def log_message(self, format, *args):  # noqa: A002 - quiet; errors are reported below
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # --- plumbing -------------------------------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise db.ValidationError("That upload is too big")
        return self.rfile.read(length) if length else b""

    def _json_body(self) -> dict:
        raw = self._body()
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise db.ValidationError("Body must be JSON") from None
        if not isinstance(data, dict):
            raise db.ValidationError("Body must be a JSON object")
        return data

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _token_ok(self, query: dict) -> bool:
        given = self.headers.get("X-Token") or (query.get("t") or [""])[0]
        return hmac.compare_digest(given.encode(), self.server.token.encode())

    def _dispatch(self, method: str) -> None:
        if not self._host_ok():
            return self._send(403, b"Forbidden", "text/plain")
        url = urlparse(self.path)
        query = parse_qs(url.query)
        parts = [p for p in url.path.split("/") if p]
        if method == "GET" and not parts:
            html = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", self.server.token)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if not self._token_ok(query):
            return self._json(403, {"error": "Reload the page: the tracker was restarted"})
        conn = db.connect(self.server.db_file)
        try:
            self._route(conn, method, parts, query)
        except db.ValidationError as exc:
            conn.rollback()
            self._json(400, {"error": str(exc)})
        except db.NotFound:
            conn.rollback()
            self._json(404, {"error": "That item no longer exists; reload the page"})
        except Exception as exc:  # noqa: BLE001 - report, don't kill the server
            conn.rollback()
            traceback.print_exc(file=sys.stderr)
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            conn.close()

    # --- routes ---------------------------------------------------------------------------------

    def _route(self, conn: sqlite3.Connection, method: str, parts: list[str], query: dict) -> None:
        if parts[0] == "export" and len(parts) == 2 and method == "GET":
            if parts[1] not in exporter.EXPORTS:
                raise db.NotFound(parts[1])
            ctype, body = exporter.render(conn, parts[1])
            stem, ext = parts[1].rsplit(".", 1)
            filename = f"job-tracker-{stem}-{date.today().isoformat()}.{ext}"
            return self._send(200, body, f"{ctype}; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="{filename}"'})
        if parts[0] == "resume" and len(parts) == 2 and parts[1].isdigit() and method == "GET":
            path = resumes.path_of(conn, self.server.resumes_dir, int(parts[1]))
            disposition = f"inline; filename*=UTF-8''{quote(path.name)}"
            return self._send(200, path.read_bytes(), resumes.content_type(path), {"Content-Disposition": disposition})
        if parts[0] != "api" or len(parts) < 2:
            raise db.NotFound("/".join(parts))
        name = parts[1]

        if name == "data" and method == "GET":
            folder = self.server.resumes_dir
            with conn:
                resumes.sync(conn, folder)
            return self._json(200, {"config": self.server.cfg, "today": date.today().isoformat(),
                                    **db.all_data(conn), "resumes": resumes.listing(conn, folder),
                                    "resumes_dir": str(folder)})
        if name == "resumes":
            done = self._resume_route(conn, method, parts[2:], query)
            if done:
                return None
        if name == "quick-add" and method == "POST":
            body = self._json_body()
            with conn:
                out = db.quick_add(conn, body.get("company") or {}, body.get("application"))
            return self._json(200, out)
        if name == "import" and method == "POST":
            text = self._body().decode("utf-8-sig", errors="replace")
            result = importer.import_csv(
                conn, text, statuses=self.server.cfg["statuses"],
                date_order=(query.get("order") or ["auto"])[0],
                dry_run=(query.get("dry") or ["0"])[0] == "1",
            )
            return self._json(200, result.as_dict())
        if name == "import-projects" and method == "POST":
            text = self._body().decode("utf-8-sig", errors="replace")
            result = importer.import_projects(conn, text, dry_run=(query.get("dry") or ["0"])[0] == "1")
            return self._json(200, result.as_dict())
        if name in db.TABLES:
            if len(parts) == 2 and method == "POST":
                with conn:
                    row = db.insert(conn, name, self._json_body())
                return self._json(200, row)
            if len(parts) == 3 and parts[2].isdigit():
                row_id = int(parts[2])
                if method == "PATCH":
                    with conn:
                        row = db.update(conn, name, row_id, self._json_body())
                    return self._json(200, row)
                if method == "DELETE":
                    with conn:
                        db.delete(conn, name, row_id)
                    return self._json(200, {"ok": True})
        raise db.NotFound("/".join(parts))

    def _resume_route(self, conn: sqlite3.Connection, method: str, rest: list[str], query: dict) -> bool:
        """Resume calls that touch the folder. Label, notes and default go through the generic PATCH."""
        folder = self.server.resumes_dir
        if rest == ["upload"] and method == "POST":
            with conn:
                row = resumes.upload(conn, folder, (query.get("name") or [""])[0], self._body())
            self._json(200, row)
        elif rest == ["open-folder"] and method == "POST":
            resumes.open_folder(folder)
            self._json(200, {"ok": True})
        elif len(rest) == 2 and rest[0].isdigit() and rest[1] in ("reveal", "copy") and method == "POST":
            path = resumes.path_of(conn, folder, int(rest[0]))
            (resumes.reveal if rest[1] == "reveal" else resumes.copy_to_clipboard)(path)
            self._json(200, {"ok": True})
        elif len(rest) == 1 and rest[0].isdigit() and method == "DELETE":
            with conn:
                resumes.remove(conn, folder, int(rest[0]))
            self._json(200, {"ok": True})
        else:
            return False
        return True


def serve(db_file: Path, cfg: dict, *, port: int | None = None, open_browser: bool = True) -> None:
    first = port or cfg["port"]
    server = None
    for candidate in range(first, first + PORT_ATTEMPTS):
        try:
            server = TrackerServer(("127.0.0.1", candidate), db_file, cfg)
            break
        except OSError:
            continue
    if server is None:
        sys.exit(f"No free port between {first} and {first + PORT_ATTEMPTS - 1}")
    print(f"Job Tracker running at {server.url}")
    print(f"Data: {db_file}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
