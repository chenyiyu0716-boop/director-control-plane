import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..config import ProjectConfig
from ..domain.models import Check, ControlTask, DecisionOutcome, Finding, KnowledgeCandidate, RunStatus, TaskState


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class TaskRepositoryError(Exception):
    pass


class DuplicateTaskError(TaskRepositoryError):
    pass


class TaskNotFoundError(TaskRepositoryError):
    pass


class TaskVersionConflictError(TaskRepositoryError):
    pass


class TaskDependencyBlockedError(TaskRepositoryError):
    pass


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

    def register_task(self, task: ControlTask, source_fingerprint: str, actor: str, request_id: str) -> Dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            project = connection.execute("SELECT id FROM project WHERE id = ?", (task.project_id,)).fetchone()
            if not project:
                raise TaskNotFoundError("project is not registered: {}".format(task.project_id))
            dependencies = []
            for dependency_id in task.dependencies:
                dependency = connection.execute(
                    "SELECT id, project_id FROM control_task WHERE id = ?", (dependency_id,)
                ).fetchone()
                if not dependency:
                    raise TaskNotFoundError("dependency is not registered: {}".format(dependency_id))
                if dependency["project_id"] != task.project_id:
                    raise TaskDependencyBlockedError("dependency belongs to a different project: {}".format(dependency_id))
                dependencies.append(dependency_id)
            try:
                connection.execute(
                    """INSERT INTO control_task
                       (id, project_id, title, objective, scope_json, acceptance_json, priority, state,
                        version, risk_level, allowed_executors_json, workspace_roots_json, source_uri,
                        source_fingerprint, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
                    (task.id, task.project_id, task.title, task.objective, json_text(task.scope),
                     json_text(task.acceptance), task.priority.value, TaskState.DRAFT.value,
                     task.risk_level.value, json_text(task.allowed_executors), json_text(task.workspace_roots),
                     task.source_uri, source_fingerprint, now, now),
                )
                for dependency_id in dependencies:
                    connection.execute(
                        "INSERT INTO control_task_dependency (task_id, depends_on_task_id, created_at) VALUES (?, ?, ?)",
                        (task.id, dependency_id, now),
                    )
                connection.execute(
                    """INSERT INTO control_task_transition
                       (id, task_id, from_state, to_state, actor, reason, previous_version, result_version, request_id, created_at)
                       VALUES (?, ?, NULL, ?, ?, ?, 0, 1, ?, ?)""",
                    (str(uuid.uuid4()), task.id, TaskState.DRAFT.value, actor, "task registered", request_id, now),
                )
            except sqlite3.IntegrityError as error:
                raise DuplicateTaskError("task id or source fingerprint already exists") from error
            self._audit(
                connection, actor, "task.registered", "control_task", task.id, None,
                {"state": TaskState.DRAFT.value, "version": 1}, request_id=request_id,
            )
        registered = self.get_task(task.id)
        if registered is None:
            raise TaskNotFoundError("task registration was not persisted: {}".format(task.id))
        return registered

    def transition_task(self, task_id: str, from_state: TaskState, to_state: TaskState, expected_version: int,
                        actor: str, reason: str, request_id: str) -> Dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            current = connection.execute("SELECT * FROM control_task WHERE id = ?", (task_id,)).fetchone()
            if not current:
                raise TaskNotFoundError("task is not registered: {}".format(task_id))
            if current["state"] != from_state.value or int(current["version"]) != expected_version:
                raise TaskVersionConflictError(
                    "expected {} v{}, found {} v{}".format(
                        from_state.value, expected_version, current["state"], current["version"]
                    )
                )
            if to_state is TaskState.READY:
                blocked = connection.execute(
                    """SELECT dependency.id, dependency.state
                       FROM control_task_dependency link
                       JOIN control_task dependency ON dependency.id = link.depends_on_task_id
                       WHERE link.task_id = ? AND dependency.state <> 'DONE'
                       ORDER BY dependency.id""",
                    (task_id,),
                ).fetchall()
                if blocked:
                    details = ", ".join("{}={}".format(item["id"], item["state"]) for item in blocked)
                    raise TaskDependencyBlockedError("dependencies are not DONE: {}".format(details))
            result_version = expected_version + 1
            result = connection.execute(
                """UPDATE control_task SET state = ?, version = ?, updated_at = ?
                   WHERE id = ? AND state = ? AND version = ?""",
                (to_state.value, result_version, now, task_id, from_state.value, expected_version),
            )
            if result.rowcount != 1:
                raise TaskVersionConflictError("task changed during transition")
            try:
                connection.execute(
                    """INSERT INTO control_task_transition
                       (id, task_id, from_state, to_state, actor, reason, previous_version, result_version, request_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), task_id, from_state.value, to_state.value, actor, reason,
                     expected_version, result_version, request_id, now),
                )
            except sqlite3.IntegrityError as error:
                raise TaskVersionConflictError("transition request id already used") from error
            self._audit(
                connection, actor, "task.transitioned", "control_task", task_id,
                {"state": from_state.value, "version": expected_version},
                {"state": to_state.value, "version": result_version}, request_id=request_id,
            )
        transitioned = self.get_task(task_id)
        if transitioned is None:
            raise TaskNotFoundError("task transition was not persisted: {}".format(task_id))
        return transitioned

    def dependency_snapshot(self, task_id: str) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT dependency.id, dependency.state, dependency.version
                   FROM control_task_dependency link
                   JOIN control_task dependency ON dependency.id = link.depends_on_task_id
                   WHERE link.task_id = ? ORDER BY dependency.id""",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_task_decision(
        self, task_id: str, from_state: TaskState, outcome: DecisionOutcome, expected_version: int,
        policy_version: str, reasons: List[str], matched_rules: List[str], facts: Dict[str, Any],
        dependency_snapshot: List[Dict[str, Any]], advisory: Optional[Dict[str, Any]],
        input_fingerprint: str, actor: str, request_id: str,
    ) -> Dict[str, Any]:
        now = utc_now()
        to_state = TaskState(outcome.value)
        result_version = expected_version + 1
        decision_id = str(uuid.uuid4())
        with self.connect() as connection:
            current = connection.execute("SELECT state, version FROM control_task WHERE id = ?", (task_id,)).fetchone()
            if not current:
                raise TaskNotFoundError("task is not registered: {}".format(task_id))
            if current["state"] != from_state.value or int(current["version"]) != expected_version:
                raise TaskVersionConflictError(
                    "expected {} v{}, found {} v{}".format(
                        from_state.value, expected_version, current["state"], current["version"]
                    )
                )
            if outcome is DecisionOutcome.READY:
                blocked = connection.execute(
                    """SELECT dependency.id, dependency.state
                       FROM control_task_dependency link
                       JOIN control_task dependency ON dependency.id = link.depends_on_task_id
                       WHERE link.task_id = ? AND dependency.state <> 'DONE'
                       ORDER BY dependency.id""",
                    (task_id,),
                ).fetchall()
                if blocked:
                    details = ", ".join("{}={}".format(item["id"], item["state"]) for item in blocked)
                    raise TaskDependencyBlockedError("dependencies are not DONE: {}".format(details))
            result = connection.execute(
                """UPDATE control_task SET state = ?, version = ?, updated_at = ?
                   WHERE id = ? AND state = ? AND version = ?""",
                (to_state.value, result_version, now, task_id, from_state.value, expected_version),
            )
            if result.rowcount != 1:
                raise TaskVersionConflictError("task changed during policy decision")
            try:
                connection.execute(
                    """INSERT INTO task_decision
                       (id, task_id, task_version, result_version, policy_version, outcome, reasons_json,
                        matched_rules_json, facts_json, dependency_snapshot_json, advisory_json,
                        input_fingerprint, actor, request_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (decision_id, task_id, expected_version, result_version, policy_version, outcome.value,
                     json_text(reasons), json_text(matched_rules), json_text(facts),
                     json_text(dependency_snapshot), json_text(advisory) if advisory else None,
                     input_fingerprint, actor, request_id, now),
                )
                connection.execute(
                    """INSERT INTO control_task_transition
                       (id, task_id, from_state, to_state, actor, reason, previous_version, result_version, request_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), task_id, from_state.value, to_state.value, actor,
                     "policy decision {}: {}".format(policy_version, "; ".join(reasons)),
                     expected_version, result_version, request_id, now),
                )
            except sqlite3.IntegrityError as error:
                raise TaskVersionConflictError("decision request id already used") from error
            self._audit(
                connection, actor, "task.decision_applied", "control_task", task_id,
                {"state": from_state.value, "version": expected_version},
                {"state": to_state.value, "version": result_version, "decision_id": decision_id,
                 "policy_version": policy_version, "input_fingerprint": input_fingerprint},
                request_id=request_id,
            )
        decision = self.get_task_decision(decision_id)
        task = self.get_task(task_id)
        if decision is None or task is None:
            raise TaskNotFoundError("policy decision was not persisted: {}".format(task_id))
        return {"decision": decision, "task": task}

    def get_task_decision(self, decision_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM task_decision WHERE id = ?", (decision_id,)).fetchone()
        return self._decision_record(row) if row else None

    def list_task_decisions(self, task_id: Optional[str] = None, outcome: Optional[str] = None,
                            limit: int = 100) -> List[Dict[str, Any]]:
        clauses = []
        values: List[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            values.append(task_id)
        if outcome:
            clauses.append("outcome = ?")
            values.append(outcome)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_decision{} ORDER BY created_at DESC, rowid DESC LIMIT ?".format(where),
                tuple(values),
            ).fetchall()
        return [self._decision_record(row) for row in rows]

    def _decision_record(self, row: sqlite3.Row) -> Dict[str, Any]:
        record = dict(row)
        for field_name in ("reasons_json", "matched_rules_json", "facts_json", "dependency_snapshot_json"):
            record[field_name[:-5]] = json.loads(record.pop(field_name))
        advisory = record.pop("advisory_json")
        record["advisory"] = json.loads(advisory) if advisory else None
        return record

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM control_task WHERE id = ?", (task_id,)).fetchone()
            return self._task_record(connection, row) if row else None

    def list_tasks(self, project_id: Optional[str] = None, state: Optional[str] = None,
                   limit: int = 100) -> List[Dict[str, Any]]:
        clauses = []
        values: List[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            values.append(project_id)
        if state:
            clauses.append("state = ?")
            values.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM control_task{} ORDER BY priority, id LIMIT ?".format(where), tuple(values)
            ).fetchall()
            return [self._task_record(connection, row) for row in rows]

    def list_task_transitions(self, task_id: str) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM control_task_transition WHERE task_id = ? ORDER BY result_version, created_at",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _task_record(self, connection: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
        record = dict(row)
        for field_name in ("scope_json", "acceptance_json", "allowed_executors_json", "workspace_roots_json"):
            record[field_name[:-5]] = json.loads(record.pop(field_name))
        dependencies = connection.execute(
            "SELECT depends_on_task_id FROM control_task_dependency WHERE task_id = ? ORDER BY depends_on_task_id",
            (record["id"],),
        ).fetchall()
        record["dependencies"] = [item["depends_on_task_id"] for item in dependencies]
        return record

    def list_rows(self, table: str, limit: int = 100) -> List[Dict[str, Any]]:
        allowed = {
            "project", "agent_run", "finding", "review_item", "check_result", "release_report",
            "control_task", "control_task_transition", "task_decision", "audit_event",
        }
        if table not in allowed:
            raise ValueError("unsupported table")
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM {} ORDER BY rowid DESC LIMIT ?".format(table), (limit,)).fetchall()
        return [dict(row) for row in rows]

    def _audit(self, connection: sqlite3.Connection, actor: str, action: str, object_type: str,
               object_id: str, before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]],
               request_id: Optional[str] = None) -> None:
        connection.execute(
            """INSERT INTO audit_event
               (id, actor, action, object_type, object_id, before_json, after_json, request_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), actor, action, object_type, object_id,
             json_text(before) if before else None, json_text(after) if after else None,
             request_id or str(uuid.uuid4()), utc_now()),
        )
