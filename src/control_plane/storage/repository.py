import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..config import ProjectConfig
from ..domain.models import Check, Finding, KnowledgeCandidate, RunStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Repository:
    def __init__(self, database: Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def migrate(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self.connect() as connection:
            connection.executescript(schema)

    def upsert_project(self, project: ProjectConfig) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO project (id, name, kind, owner, config_json, enabled, updated_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind,
                   owner=excluded.owner, config_json=excluded.config_json, updated_at=excluded.updated_at""",
                (project.id, project.name, project.kind, project.owner, json_text({
                    "root": str(project.root),
                    "ledger": str(project.ledger),
                    "status": str(project.status),
                    "knowledge_roots": [str(value) for value in project.knowledge_roots],
                    "enabled_agents": [value.value for value in project.enabled_agents],
                }), utc_now()),
            )

    def start_run(self, project_id: str, agent_type: str, trigger: str) -> str:
        run_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO agent_run (id, project_id, agent_type, trigger, status, started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, project_id, agent_type, trigger, RunStatus.RUNNING.value, now),
            )
            self._audit(connection, "control-plane", "run.started", "agent_run", run_id, None, {"status": "running"})
        return run_id

    def finish_run(self, run_id: str, status: RunStatus, summary: str, error: Optional[Dict[str, Any]] = None) -> None:
        with self.connect() as connection:
            before = connection.execute("SELECT status FROM agent_run WHERE id = ?", (run_id,)).fetchone()
            connection.execute(
                "UPDATE agent_run SET status = ?, finished_at = ?, summary = ?, error_json = ? WHERE id = ?",
                (status.value, utc_now(), summary, json_text(error) if error else None, run_id),
            )
            self._audit(connection, "control-plane", "run.finished", "agent_run", run_id, dict(before) if before else None, {"status": status.value})

    def add_finding(self, run_id: str, finding: Finding) -> str:
        finding_id = str(uuid.uuid4())
        fingerprint = hashlib.sha256(json_text({
            "category": finding.category,
            "title": finding.title,
            "evidence": finding.evidence,
        }).encode("utf-8")).hexdigest()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO finding
                   (id, run_id, category, severity, title, detail, evidence_json, recommendation, fingerprint, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (finding_id, run_id, finding.category, finding.severity, finding.title, finding.detail,
                 json_text(finding.evidence), finding.recommendation, fingerprint, utc_now()),
            )
        return finding_id

    def add_check(self, run_id: str, check: Check) -> str:
        check_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO check_result
                   (id, run_id, component, check_name, status, latency_ms, evidence_json, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (check_id, run_id, check.component, check.name, check.status, check.latency_ms, json_text(check.evidence), utc_now()),
            )
        return check_id

    def add_candidate(self, run_id: str, project_id: str, candidate: KnowledgeCandidate) -> Tuple[str, bool]:
        candidate_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM knowledge_candidate WHERE project_id = ? AND content_hash = ?",
                (project_id, candidate.content_hash),
            ).fetchone()
            if existing:
                return str(existing["id"]), False
            connection.execute(
                """INSERT INTO knowledge_candidate
                   (id, run_id, project_id, source_uri, content_hash, title, summary, knowledge_type,
                    tags_json, proposed_action, confidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (candidate_id, run_id, project_id, candidate.source_uri, candidate.content_hash,
                 candidate.title, candidate.summary, candidate.knowledge_type, json_text(candidate.tags),
                 candidate.proposed_action, candidate.confidence, now),
            )
            review_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO review_item
                   (id, project_id, item_type, payload_ref, status, created_at)
                   VALUES (?, ?, 'knowledge_change', ?, 'pending', ?)""",
                (review_id, project_id, candidate_id, now),
            )
            self._audit(connection, "knowledge-agent", "review.created", "review_item", review_id, None, {"status": "pending"})
        return candidate_id, True

    def add_release_report(self, run_id: str, report: Dict[str, Any]) -> str:
        report_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO release_report
                   (id, run_id, repo_ref, branch, head_ref, commit_count, dirty, risk_items_json, notes_draft, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (report_id, run_id, report["repo_ref"], report.get("branch"), report.get("head_ref"),
                 report.get("commit_count", 0), int(bool(report.get("dirty"))),
                 json_text(report.get("risk_items", [])), report.get("notes_draft", ""), utc_now()),
            )
        return report_id

    def list_rows(self, table: str, limit: int = 100) -> List[Dict[str, Any]]:
        allowed = {"project", "agent_run", "finding", "review_item", "check_result", "release_report"}
        if table not in allowed:
            raise ValueError("unsupported table")
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM {} ORDER BY rowid DESC LIMIT ?".format(table), (limit,)).fetchall()
        return [dict(row) for row in rows]

    def _audit(self, connection: sqlite3.Connection, actor: str, action: str, object_type: str,
               object_id: str, before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]) -> None:
        connection.execute(
            """INSERT INTO audit_event
               (id, actor, action, object_type, object_id, before_json, after_json, request_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), actor, action, object_type, object_id,
             json_text(before) if before else None, json_text(after) if after else None,
             str(uuid.uuid4()), utc_now()),
        )
