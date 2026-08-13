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

    def connect(self, timeout: float = 5.0) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database), timeout=timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def migrate(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self.connect() as connection:
            connection.executescript(schema)
            review_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(task_review)").fetchall()
            }
            if "commit_ref" not in review_columns:
                connection.execute("ALTER TABLE task_review ADD COLUMN commit_ref TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_review_done_commit "
                "ON task_review(commit_ref) WHERE outcome = 'DONE'"
            )

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

    def get_project_baseline(self, project_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT project_id, baseline_ref, updated_by, updated_at FROM project_baseline WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_task_lease(self, lease_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM task_lease WHERE id = ?", (lease_id,)).fetchone()
        return dict(row) if row else None

    def apply_review_result(self, task_id: str, task_version: int, outcome: str, gate_version: str,
                            reasons: List[str], matched_rules: List[str], evidence: Dict[str, Any],
                            evidence_fingerprint: str, executor_id: str, lease_id: str,
                            baseline_ref: str, commit_ref: str, actor: str, request_id: str) -> Dict[str, Any]:
        """Persist a review-gate outcome; only a DONE outcome transitions the task."""
        now = utc_now()
        review_id = str(uuid.uuid4())
        result_version: Optional[int] = None
        with self.connect() as connection:
            current = connection.execute("SELECT state, version FROM control_task WHERE id = ?", (task_id,)).fetchone()
            if not current:
                raise TaskNotFoundError("task is not registered: {}".format(task_id))
            if current["state"] != TaskState.REVIEW.value or int(current["version"]) != task_version:
                raise TaskVersionConflictError(
                    "expected REVIEW v{}, found {} v{}".format(
                        task_version, current["state"], current["version"]
                    )
                )
            if outcome == "DONE":
                result_version = task_version + 1
                updated = connection.execute(
                    """UPDATE control_task SET state = ?, version = ?, updated_at = ?
                       WHERE id = ? AND state = ? AND version = ?""",
                    (TaskState.DONE.value, result_version, now, task_id,
                     TaskState.REVIEW.value, task_version),
                )
                if updated.rowcount != 1:
                    raise TaskVersionConflictError("task changed during review")
                try:
                    connection.execute(
                        """INSERT INTO control_task_transition
                           (id, task_id, from_state, to_state, actor, reason, previous_version,
                            result_version, request_id, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (str(uuid.uuid4()), task_id, TaskState.REVIEW.value, TaskState.DONE.value, actor,
                         "review gate {}: {}".format(gate_version, "; ".join(reasons)),
                         task_version, result_version, request_id, now),
                    )
                except sqlite3.IntegrityError as error:
                    raise TaskVersionConflictError("review request id already used") from error
            try:
                connection.execute(
                    """INSERT INTO task_review
                       (id, task_id, task_version, result_version, gate_version, outcome, reasons_json,
                        matched_rules_json, evidence_json, evidence_fingerprint, executor_id, lease_id,
                        baseline_ref, commit_ref, actor, request_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (review_id, task_id, task_version, result_version, gate_version, outcome,
                     json_text(reasons), json_text(matched_rules), json_text(evidence),
                     evidence_fingerprint, executor_id, lease_id, baseline_ref, commit_ref,
                     actor, request_id, now),
                )
            except sqlite3.IntegrityError as error:
                raise TaskVersionConflictError("review request id or completed commit already used") from error
            self._audit(
                connection, actor, "task.review_evaluated", "control_task", task_id,
                {"state": current["state"], "version": int(current["version"])},
                {"outcome": outcome, "review_id": review_id, "gate_version": gate_version,
                 "evidence_fingerprint": evidence_fingerprint,
                 "state": TaskState.DONE.value if result_version else current["state"],
                 "version": result_version or int(current["version"])},
                request_id=request_id,
            )
        review = self.get_task_review(review_id)
        task = self.get_task(task_id)
        if review is None or task is None:
            raise TaskNotFoundError("review result was not persisted: {}".format(task_id))
        return {"review": review, "task": task}

    def get_task_review(self, review_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM task_review WHERE id = ?", (review_id,)).fetchone()
        return self._review_record(row) if row else None

    def get_task_review_by_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM task_review WHERE request_id = ?", (request_id,)).fetchone()
        return self._review_record(row) if row else None

    def list_task_reviews(self, task_id: Optional[str] = None, outcome: Optional[str] = None,
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
                "SELECT * FROM task_review{} ORDER BY created_at DESC, rowid DESC LIMIT ?".format(where),
                tuple(values),
            ).fetchall()
        return [self._review_record(row) for row in rows]

    @staticmethod
    def _review_record(row: sqlite3.Row) -> Dict[str, Any]:
        record = dict(row)
        for field_name in ("reasons_json", "matched_rules_json", "evidence_json"):
            record[field_name[:-5]] = json.loads(record.pop(field_name))
        return record

    def receive_feishu_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        with self.connect(timeout=0.25) as connection:
            existing = connection.execute(
                "SELECT status FROM feishu_inbox_event WHERE event_id = ? OR nonce = ?",
                (event["event_id"], event["nonce"]),
            ).fetchone()
            if existing:
                return {"accepted": True, "duplicate": True, "status": existing["status"]}
            try:
                connection.execute(
                    """INSERT INTO feishu_inbox_event
                       (event_id, event_type, operator_id, nonce, expires_at, message_ref, payload_json,
                        status, received_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (event["event_id"], event["event_type"], event["operator_id"], event["nonce"],
                     event["expires_at"], event.get("message_ref"), json_text(event["payload"]), utc_now()),
                )
            except sqlite3.IntegrityError:
                duplicate = connection.execute(
                    "SELECT status FROM feishu_inbox_event WHERE event_id = ? OR nonce = ?",
                    (event["event_id"], event["nonce"]),
                ).fetchone()
                if duplicate:
                    return {"accepted": True, "duplicate": True, "status": duplicate["status"]}
                raise
            self._audit(
                connection, "feishu-inbox", "feishu.event_received", "feishu_inbox_event",
                event["event_id"], None, {"status": "pending", "event_type": event["event_type"]},
                request_id=event["event_id"],
            )
        return {"accepted": True, "duplicate": False, "status": "pending"}

    def list_pending_feishu_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM feishu_inbox_event WHERE status = 'pending' ORDER BY received_at, rowid LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._feishu_event_record(row) for row in rows]

    def finish_feishu_event(self, event_id: str, status: str, result: Dict[str, Any]) -> None:
        if status not in {"processed", "rejected"}:
            raise ValueError("invalid Feishu event status")
        with self.connect() as connection:
            updated = connection.execute(
                """UPDATE feishu_inbox_event SET status = ?, result_json = ?, processed_at = ?
                   WHERE event_id = ? AND status = 'pending'""",
                (status, json_text(result), utc_now(), event_id),
            )
            if updated.rowcount != 1:
                return
            self._audit(
                connection, "feishu-control", "feishu.event_{}".format(status), "feishu_inbox_event",
                event_id, {"status": "pending"}, {"status": status, "result": result},
                request_id="finish:{}".format(event_id),
            )

    def apply_owner_decision(self, event_id: str, task_id: str, expected_version: int,
                             action: str, operator_id: str, reason: str) -> Dict[str, Any]:
        if action not in {"approve", "reject", "request_changes"}:
            raise ValueError("unsupported owner decision action")
        now = utc_now()
        to_state = TaskState.READY if action == "approve" else TaskState.BLOCKED
        result_version = expected_version + 1
        decision_id = str(uuid.uuid4())
        with self.connect() as connection:
            current = connection.execute("SELECT state, version FROM control_task WHERE id = ?", (task_id,)).fetchone()
            if not current:
                raise TaskNotFoundError("task is not registered: {}".format(task_id))
            if current["state"] != TaskState.NEEDS_DECISION.value or int(current["version"]) != expected_version:
                raise TaskVersionConflictError(
                    "expected NEEDS_DECISION v{}, found {} v{}".format(
                        expected_version, current["state"], current["version"]
                    )
                )
            if to_state is TaskState.READY:
                blocked = connection.execute(
                    """SELECT dependency.id, dependency.state FROM control_task_dependency link
                       JOIN control_task dependency ON dependency.id = link.depends_on_task_id
                       WHERE link.task_id = ? AND dependency.state <> 'DONE' ORDER BY dependency.id""",
                    (task_id,),
                ).fetchall()
                if blocked:
                    raise TaskDependencyBlockedError("dependencies are not DONE")
            connection.execute(
                """UPDATE control_task SET state = ?, version = ?, updated_at = ?
                   WHERE id = ? AND state = 'NEEDS_DECISION' AND version = ?""",
                (to_state.value, result_version, now, task_id, expected_version),
            )
            try:
                connection.execute(
                    """INSERT INTO owner_decision
                       (id, task_id, task_version, action, operator_id, reason, event_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (decision_id, task_id, expected_version, action, operator_id, reason, event_id, now),
                )
                connection.execute(
                    """INSERT INTO control_task_transition
                       (id, task_id, from_state, to_state, actor, reason, previous_version,
                        result_version, request_id, created_at)
                       VALUES (?, ?, 'NEEDS_DECISION', ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), task_id, to_state.value, "feishu-owner:{}".format(operator_id),
                     "owner {}: {}".format(action, reason), expected_version, result_version,
                     "feishu:{}".format(event_id), now),
                )
            except sqlite3.IntegrityError as error:
                raise TaskVersionConflictError("owner decision event was already applied") from error
            self._audit(
                connection, "feishu-owner:{}".format(operator_id), "owner.decision_applied",
                "control_task", task_id,
                {"state": "NEEDS_DECISION", "version": expected_version},
                {"state": to_state.value, "version": result_version, "decision_id": decision_id},
                request_id="owner:{}".format(event_id),
            )
        return {"decision_id": decision_id, "task": self.get_task(task_id)}

    def create_requirement_intake(self, event_id: str, project_id: str, kind: str, objective: str,
                                  requested_priority: Optional[str], operator_id: str,
                                  preview: Dict[str, Any]) -> Dict[str, Any]:
        intake_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            if not connection.execute("SELECT id FROM project WHERE id = ?", (project_id,)).fetchone():
                raise TaskNotFoundError("project is not registered: {}".format(project_id))
            connection.execute(
                """INSERT INTO requirement_intake
                   (id, project_id, kind, objective, requested_priority, operator_id, source_event_id,
                    status, preview_json, version, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'PREVIEW_PENDING', ?, 1, ?, ?)""",
                (intake_id, project_id, kind, objective, requested_priority, operator_id, event_id,
                 json_text(preview), now, now),
            )
            self._audit(
                connection, "feishu-owner:{}".format(operator_id), "requirement.preview_created",
                "requirement_intake", intake_id, None,
                {"status": "PREVIEW_PENDING", "project_id": project_id, "kind": kind},
                request_id="intake:{}".format(event_id),
            )
        return self.get_requirement_intake(intake_id)

    def confirm_requirement_intake(self, event_id: str, intake_id: str, expected_version: int,
                                   operator_id: str, confirm: bool) -> Dict[str, Any]:
        now = utc_now()
        status = "CONFIRMED" if confirm else "REJECTED"
        with self.connect() as connection:
            current = connection.execute("SELECT status, version FROM requirement_intake WHERE id = ?", (intake_id,)).fetchone()
            if not current:
                raise TaskNotFoundError("requirement intake is not registered: {}".format(intake_id))
            if current["status"] != "PREVIEW_PENDING" or int(current["version"]) != expected_version:
                raise TaskVersionConflictError("requirement intake version or state changed")
            connection.execute(
                """UPDATE requirement_intake SET status = ?, version = ?, confirmed_event_id = ?, updated_at = ?
                   WHERE id = ? AND status = 'PREVIEW_PENDING' AND version = ?""",
                (status, expected_version + 1, event_id, now, intake_id, expected_version),
            )
            self._audit(
                connection, "feishu-owner:{}".format(operator_id), "requirement.{}".format(status.lower()),
                "requirement_intake", intake_id,
                {"status": "PREVIEW_PENDING", "version": expected_version},
                {"status": status, "version": expected_version + 1},
                request_id="intake-confirm:{}".format(event_id),
            )
        return self.get_requirement_intake(intake_id)

    def get_requirement_intake(self, intake_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM requirement_intake WHERE id = ?", (intake_id,)).fetchone()
        return self._requirement_record(row) if row else None

    def list_requirement_intakes(self, project_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if project_id:
            query = "SELECT * FROM requirement_intake WHERE project_id = ? ORDER BY created_at DESC LIMIT ?"
            values = (project_id, limit)
        else:
            query = "SELECT * FROM requirement_intake ORDER BY created_at DESC LIMIT ?"
            values = (limit,)
        with self.connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [self._requirement_record(row) for row in rows]

    def list_owner_decisions(self, task_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if task_id:
            query = "SELECT * FROM owner_decision WHERE task_id = ? ORDER BY created_at DESC LIMIT ?"
            values = (task_id, limit)
        else:
            query = "SELECT * FROM owner_decision ORDER BY created_at DESC LIMIT ?"
            values = (limit,)
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, values).fetchall()]

    def get_owner_decision_by_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM owner_decision WHERE event_id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    def get_requirement_intake_by_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM requirement_intake WHERE source_event_id = ? OR confirmed_event_id = ?",
                (event_id, event_id),
            ).fetchone()
        return self._requirement_record(row) if row else None

    @staticmethod
    def _feishu_event_record(row: sqlite3.Row) -> Dict[str, Any]:
        record = dict(row)
        record["payload"] = json.loads(record.pop("payload_json"))
        result = record.pop("result_json")
        record["result"] = json.loads(result) if result else None
        return record

    @staticmethod
    def _requirement_record(row: sqlite3.Row) -> Dict[str, Any]:
        record = dict(row)
        record["preview"] = json.loads(record.pop("preview_json"))
        return record

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
            "control_task", "control_task_transition", "task_decision", "task_review", "feishu_inbox_event",
            "owner_decision", "requirement_intake", "executor_profile", "project_baseline",
            "task_lease", "dispatcher_operation", "audit_event",
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
