import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Tuple
from urllib.parse import parse_qs, urlparse

from ..storage import Repository


ROUTES: Dict[str, str] = {
    "/api/projects": "project",
    "/api/runs": "agent_run",
    "/api/findings": "finding",
    "/api/reviews": "review_item",
    "/api/checks": "check_result",
    "/api/releases": "release_report",
}


def create_handler(repository: Repository):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DirectorControlPlane/0.1"

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._json(200, {"status": "ok", "service": "director-control-plane", "version": "0.1.0"})
                return
            table = ROUTES.get(parsed.path)
            if table:
                query = parse_qs(parsed.query)
                limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
                self._json(200, {"items": repository.list_rows(table, limit), "limit": limit})
                return
            self._json(404, {"error": "not_found"})

        def log_message(self, _format, *_args):
            return

        def _json(self, status: int, payload):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(repository: Repository, address: Tuple[str, int]) -> None:
    server = ThreadingHTTPServer(address, create_handler(repository))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
