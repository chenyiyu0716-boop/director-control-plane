import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Tuple
from urllib.parse import parse_qs, urlparse

from ..storage import Repository
from ..domain.models import DecisionOutcome, TaskState


ROUTES: Dict[str, str] = {
    "/api/projects": "project",
    "/api/runs": "agent_run",
    "/api/findings": "finding",
    "/api/reviews": "review_item",
    "/api/checks": "check_result",
    "/api/releases": "release_report",
    "/api/executors": "executor_profile",
    "/api/baselines": "project_baseline",
    "/api/leases": "task_lease",
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
            if parsed.path == "/api/tasks":
                query = parse_qs(parsed.query)
                limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
                project_id = query.get("project_id", [None])[0]
                state = query.get("state", [None])[0]
                if state and state not in {value.value for value in TaskState}:
                    self._json(400, {"error": "invalid_task_state"})
                    return
                self._json(200, {"items": repository.list_tasks(project_id, state, limit), "limit": limit})
                return
            if parsed.path == "/api/decisions":
                query = parse_qs(parsed.query)
                limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
                task_id = query.get("task_id", [None])[0]
                outcome = query.get("outcome", [None])[0]
                if outcome and outcome not in {value.value for value in DecisionOutcome}:
                    self._json(400, {"error": "invalid_decision_outcome"})
                    return
                self._json(200, {
                    "items": repository.list_task_decisions(task_id, outcome, limit), "limit": limit,
                })
                return
            if parsed.path == "/api/owner-decisions":
                query = parse_qs(parsed.query)
                limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
                task_id = query.get("task_id", [None])[0]
                self._json(200, {"items": repository.list_owner_decisions(task_id, limit), "limit": limit})
                return
            if parsed.path == "/api/task-reviews":
                query = parse_qs(parsed.query)
                limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
                task_id = query.get("task_id", [None])[0]
                outcome = query.get("outcome", [None])[0]
                if outcome and outcome not in {"DONE", "NEEDS_FIX", "OWNER_CONFIRMATION_REQUIRED"}:
                    self._json(400, {"error": "invalid_review_outcome"})
                    return
                self._json(200, {
                    "items": repository.list_task_reviews(task_id, outcome, limit), "limit": limit,
                })
                return
            if parsed.path == "/api/requirement-intakes":
                query = parse_qs(parsed.query)
                limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
                project_id = query.get("project_id", [None])[0]
                self._json(200, {
                    "items": repository.list_requirement_intakes(project_id, limit), "limit": limit,
                })
                return
            if parsed.path == "/api/executor-reports":
                query = parse_qs(parsed.query)
                limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
                task_id = query.get("task_id", [None])[0]
                self._json(200, {
                    "items": repository.list_executor_reports(task_id, limit), "limit": limit,
                })
                return
            if parsed.path.startswith("/api/tasks/"):
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) == 3:
                    task = repository.get_task(parts[2])
                    self._json(200, task) if task else self._json(404, {"error": "task_not_found"})
                    return
                if len(parts) == 4 and parts[3] == "history":
                    task = repository.get_task(parts[2])
                    if not task:
                        self._json(404, {"error": "task_not_found"})
                        return
                    self._json(200, {"items": repository.list_task_transitions(parts[2])})
                    return
                if len(parts) == 4 and parts[3] == "decisions":
                    task = repository.get_task(parts[2])
                    if not task:
                        self._json(404, {"error": "task_not_found"})
                        return
                    self._json(200, {"items": repository.list_task_decisions(task_id=parts[2])})
                    return
                if len(parts) == 4 and parts[3] == "reviews":
                    task = repository.get_task(parts[2])
                    if not task:
                        self._json(404, {"error": "task_not_found"})
                        return
                    query = parse_qs(parsed.query)
                    limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
                    self._json(200, {
                        "items": repository.list_task_reviews(parts[2], None, limit), "limit": limit,
                    })
                    return
                if len(parts) == 4 and parts[3] == "reports":
                    task = repository.get_task(parts[2])
                    if not task:
                        self._json(404, {"error": "task_not_found"})
                        return
                    query = parse_qs(parsed.query)
                    limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
                    self._json(200, {
                        "items": repository.list_executor_reports(parts[2], limit), "limit": limit,
                    })
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
