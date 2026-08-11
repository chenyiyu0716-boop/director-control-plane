import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..storage import Repository


RISK_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class DispatcherError(Exception):
    pass


class ExecutorUnauthorizedError(DispatcherError):
    pass


class LeaseConflictError(DispatcherError):
    pass


class BaselineConflictError(DispatcherError):
    pass


class LeaseDispatcher:
    def __init__(self, repository: Repository, default_ttl_seconds: int = 900):
        self.repository = repository
        self.default_ttl_seconds = default_ttl_seconds

    def register_executor(self, executor_id: str, project_ids: List[str], max_risk: str,
                          enabled: bool = True) -> Dict[str, Any]:
        if not executor_id or not project_ids or max_risk not in RISK_RANK:
            raise DispatcherError("invalid executor profile")
        now = self._now().isoformat()
        with self.repository.connect() as connection:
            for project_id in project_ids:
                if not connection.execute("SELECT id FROM project WHERE id = ?", (project_id,)).fetchone():
                    raise DispatcherError("unknown project: {}".format(project_id))
            connection.execute(
                """INSERT INTO executor_profile (id, project_ids_json, max_risk, enabled, updated_at)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
                   project_ids_json=excluded.project_ids_json, max_risk=excluded.max_risk,
                   enabled=excluded.enabled, updated_at=excluded.updated_at""",
                (executor_id, json.dumps(sorted(set(project_ids))), max_risk, int(enabled), now),
            )
        return {"id": executor_id, "project_ids": sorted(set(project_ids)), "max_risk": max_risk, "enabled": enabled}

    def set_project_baseline(self, project_id: str, baseline_ref: str, actor: str) -> Dict[str, Any]:
        if not baseline_ref.strip() or not actor.strip():
            raise DispatcherError("baseline_ref and actor are required")
        now = self._now().isoformat()
        with self.repository.connect() as connection:
            if not connection.execute("SELECT id FROM project WHERE id = ?", (project_id,)).fetchone():
                raise DispatcherError("unknown project")
            connection.execute(
                """INSERT INTO project_baseline (project_id, baseline_ref, updated_by, updated_at)
                   VALUES (?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET
                   baseline_ref=excluded.baseline_ref, updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                (project_id, baseline_ref, actor, now),
            )
        return {"project_id": project_id, "baseline_ref": baseline_ref, "updated_by": actor}

    def next(self, executor_id: str, project_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        now = self._now()
        with self.repository.connect(timeout=1.0) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired(connection, now)
            profile = self._profile(connection, executor_id)
            allowed_projects = set(profile["project_ids"])
            if project_id and project_id not in allowed_projects:
                raise ExecutorUnauthorizedError("executor is not authorized for project")
            rows = connection.execute(
                "SELECT * FROM control_task WHERE state = 'READY' ORDER BY priority, updated_at, id"
            ).fetchall()
            for row in rows:
                task = self._task(connection, row)
                if self._eligible(task, profile, project_id):
                    return task
        return None

    def claim(self, task_id: str, executor_id: str, baseline_ref: str, expected_version: int,
              request_id: str, ttl_seconds: Optional[int] = None) -> Dict[str, Any]:
        now = self._now()
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        self._validate_request(request_id, ttl)
        with self.repository.connect(timeout=1.0) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired(connection, now)
            replay = self._replay(connection, request_id, "claim", executor_id)
            if replay:
                return self._active_replay(connection, replay, executor_id)
            profile = self._profile(connection, executor_id)
            row = connection.execute("SELECT * FROM control_task WHERE id = ?", (task_id,)).fetchone()
            if not row:
                raise DispatcherError("task not found")
            task = self._task(connection, row)
            if not self._eligible(task, profile, None):
                raise ExecutorUnauthorizedError("executor is not eligible for task")
            if task["state"] != "READY" or task["version"] != expected_version:
                raise LeaseConflictError("task is not READY at expected version")
            current_baseline = self._baseline(connection, task["project_id"])
            if current_baseline != baseline_ref:
                raise BaselineConflictError("claim baseline is stale")
            lease_id = str(uuid.uuid4())
            expires_at = (now + timedelta(seconds=ttl)).isoformat()
            try:
                connection.execute(
                    """INSERT INTO task_lease
                       (id, task_id, executor_id, baseline_ref, claimed_version, status, expires_at, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                    (lease_id, task_id, executor_id, baseline_ref, expected_version, expires_at,
                     now.isoformat(), now.isoformat()),
                )
            except sqlite3.IntegrityError as error:
                raise LeaseConflictError("task already has an active lease") from error
            self._transition(connection, task_id, "READY", "CLAIMED", expected_version,
                             executor_id, "lease claimed", request_id)
            result = {"lease_id": lease_id, "task_id": task_id, "executor_id": executor_id,
                      "baseline_ref": baseline_ref, "expires_at": expires_at,
                      "task_version": expected_version + 1, "status": "active"}
            self._record_operation(connection, request_id, "claim", executor_id, result)
            return result

    def heartbeat(self, lease_id: str, executor_id: str, request_id: str,
                  ttl_seconds: Optional[int] = None) -> Dict[str, Any]:
        now = self._now()
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        self._validate_request(request_id, ttl)
        with self.repository.connect(timeout=1.0) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired(connection, now)
            replay = self._replay(connection, request_id, "heartbeat", executor_id)
            if replay:
                return self._active_replay(connection, replay, executor_id)
            lease, task = self._active_lease(connection, lease_id, executor_id)
            if task["state"] == "CLAIMED":
                self._transition(connection, task["id"], "CLAIMED", "RUNNING", task["version"],
                                 executor_id, "executor heartbeat started work", request_id + ":start")
                task["version"] += 1
            expires_at = (now + timedelta(seconds=ttl)).isoformat()
            connection.execute("UPDATE task_lease SET expires_at = ?, updated_at = ? WHERE id = ?",
                               (expires_at, now.isoformat(), lease_id))
            result = {"lease_id": lease_id, "status": "active", "expires_at": expires_at,
                      "task_id": task["id"], "task_version": task["version"]}
            self._record_operation(connection, request_id, "heartbeat", executor_id, result)
            return result

    def complete(self, lease_id: str, executor_id: str, baseline_ref: str,
                 request_id: str) -> Dict[str, Any]:
        return self._finish(lease_id, executor_id, baseline_ref, request_id, True, "completed")

    def fail(self, lease_id: str, executor_id: str, baseline_ref: str,
             request_id: str, reason: str) -> Dict[str, Any]:
        if not reason.strip():
            raise DispatcherError("failure reason is required")
        return self._finish(lease_id, executor_id, baseline_ref, request_id, False, reason)

    def _finish(self, lease_id: str, executor_id: str, baseline_ref: str, request_id: str,
                success: bool, reason: str) -> Dict[str, Any]:
        now = self._now()
        operation = "complete" if success else "fail"
        self._validate_request(request_id, self.default_ttl_seconds)
        with self.repository.connect(timeout=1.0) as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._replay(connection, request_id, operation, executor_id)
            if replay:
                return replay
            self._recover_expired(connection, now)
            lease, task = self._active_lease(connection, lease_id, executor_id)
            if lease["baseline_ref"] != baseline_ref or self._baseline(connection, task["project_id"]) != baseline_ref:
                raise BaselineConflictError("completion baseline is stale")
            if success and task["state"] != "RUNNING":
                raise LeaseConflictError("task must be RUNNING before completion")
            target = "REVIEW" if success else "FAILED"
            if not success and task["state"] not in {"CLAIMED", "RUNNING"}:
                raise LeaseConflictError("task cannot fail from current state")
            self._transition(connection, task["id"], task["state"], target, task["version"],
                             executor_id, reason, request_id + ":task")
            lease_status = "completed" if success else "failed"
            connection.execute("UPDATE task_lease SET status = ?, updated_at = ? WHERE id = ?",
                               (lease_status, now.isoformat(), lease_id))
            profile_row = connection.execute(
                "SELECT enabled, updated_at FROM executor_profile WHERE id = ?", (executor_id,)
            ).fetchone()
            previous_failures = self._consecutive_failures(
                connection, executor_id, profile_row["updated_at"]
            )
            consecutive_failures = 0 if success else previous_failures + 1
            executor_enabled = bool(profile_row["enabled"])
            if not success and consecutive_failures >= 2:
                executor_enabled = False
                connection.execute(
                    "UPDATE executor_profile SET enabled = 0, updated_at = ? WHERE id = ?",
                    (now.isoformat(), executor_id),
                )
            result = {"lease_id": lease_id, "status": lease_status, "task_id": task["id"],
                      "task_state": target, "task_version": task["version"] + 1,
                      "consecutive_failures": consecutive_failures,
                      "executor_enabled": executor_enabled}
            if not executor_enabled:
                result["executor_disabled_reason"] = "two consecutive executor failures"
            self._record_operation(connection, request_id, operation, executor_id, result)
            return result

    @staticmethod
    def _consecutive_failures(connection: sqlite3.Connection, executor_id: str,
                              since: str) -> int:
        rows = connection.execute(
            """SELECT operation FROM dispatcher_operation
               WHERE executor_id = ? AND operation IN ('complete', 'fail') AND created_at >= ?
               ORDER BY created_at DESC""",
            (executor_id, since),
        ).fetchall()
        count = 0
        for row in rows:
            if row["operation"] != "fail":
                break
            count += 1
        return count

    def recover_expired(self) -> List[str]:
        with self.repository.connect(timeout=1.0) as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._recover_expired(connection, self._now())

    def _recover_expired(self, connection: sqlite3.Connection, now: datetime) -> List[str]:
        rows = connection.execute(
            "SELECT * FROM task_lease WHERE status = 'active' AND expires_at <= ? ORDER BY expires_at",
            (now.isoformat(),),
        ).fetchall()
        recovered = []
        for lease in rows:
            task = connection.execute("SELECT * FROM control_task WHERE id = ?", (lease["task_id"],)).fetchone()
            connection.execute("UPDATE task_lease SET status = 'expired', updated_at = ? WHERE id = ?",
                               (now.isoformat(), lease["id"]))
            if task and task["state"] in {"CLAIMED", "RUNNING"}:
                self._transition(connection, task["id"], task["state"], "READY", int(task["version"]),
                                 "dispatcher", "lease expired and task recovered", "expire:" + lease["id"])
            recovered.append(lease["id"])
        return recovered

    def _profile(self, connection: sqlite3.Connection, executor_id: str) -> Dict[str, Any]:
        row = connection.execute("SELECT * FROM executor_profile WHERE id = ?", (executor_id,)).fetchone()
        if not row or not row["enabled"]:
            raise ExecutorUnauthorizedError("executor is unknown or disabled")
        result = dict(row)
        result["project_ids"] = json.loads(result.pop("project_ids_json"))
        return result

    def _eligible(self, task: Dict[str, Any], profile: Dict[str, Any], project_id: Optional[str]) -> bool:
        return (
            task["project_id"] in profile["project_ids"]
            and (not project_id or task["project_id"] == project_id)
            and profile["id"] in task["allowed_executors"]
            and RISK_RANK[task["risk_level"]] <= RISK_RANK[profile["max_risk"]]
        )

    def _active_lease(self, connection: sqlite3.Connection, lease_id: str,
                      executor_id: str):
        lease = connection.execute("SELECT * FROM task_lease WHERE id = ?", (lease_id,)).fetchone()
        if not lease or lease["status"] != "active" or lease["executor_id"] != executor_id:
            raise LeaseConflictError("active lease not found for executor")
        task_row = connection.execute("SELECT * FROM control_task WHERE id = ?", (lease["task_id"],)).fetchone()
        return dict(lease), self._task(connection, task_row)

    @staticmethod
    def _baseline(connection: sqlite3.Connection, project_id: str) -> str:
        row = connection.execute("SELECT baseline_ref FROM project_baseline WHERE project_id = ?", (project_id,)).fetchone()
        if not row:
            raise BaselineConflictError("project baseline is not registered")
        return row["baseline_ref"]

    def _task(self, connection: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
        record = dict(row)
        record["allowed_executors"] = json.loads(record.pop("allowed_executors_json"))
        record["workspace_roots"] = json.loads(record.pop("workspace_roots_json"))
        record["scope"] = json.loads(record.pop("scope_json"))
        record["acceptance"] = json.loads(record.pop("acceptance_json"))
        return record

    def _transition(self, connection: sqlite3.Connection, task_id: str, from_state: str, to_state: str,
                    version: int, actor: str, reason: str, request_id: str) -> None:
        now = self._now().isoformat()
        updated = connection.execute(
            "UPDATE control_task SET state = ?, version = ?, updated_at = ? WHERE id = ? AND state = ? AND version = ?",
            (to_state, version + 1, now, task_id, from_state, version),
        )
        if updated.rowcount != 1:
            raise LeaseConflictError("task state changed concurrently")
        connection.execute(
            """INSERT INTO control_task_transition
               (id, task_id, from_state, to_state, actor, reason, previous_version, result_version, request_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), task_id, from_state, to_state, actor, reason, version, version + 1, request_id, now),
        )

    @staticmethod
    def _replay(connection: sqlite3.Connection, request_id: str, operation: str,
                executor_id: str) -> Optional[Dict[str, Any]]:
        row = connection.execute("SELECT * FROM dispatcher_operation WHERE request_id = ?", (request_id,)).fetchone()
        if not row:
            return None
        if row["operation"] != operation or row["executor_id"] != executor_id:
            raise LeaseConflictError("idempotency key was used for a different operation")
        return json.loads(row["result_json"])

    @staticmethod
    def _active_replay(connection: sqlite3.Connection, result: Dict[str, Any],
                       executor_id: str) -> Dict[str, Any]:
        lease = connection.execute(
            "SELECT status, executor_id FROM task_lease WHERE id = ?", (result["lease_id"],)
        ).fetchone()
        if not lease or lease["status"] != "active" or lease["executor_id"] != executor_id:
            raise LeaseConflictError("idempotent retry refers to a lease that is no longer active")
        return result

    @staticmethod
    def _record_operation(connection: sqlite3.Connection, request_id: str, operation: str,
                          executor_id: str, result: Dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO dispatcher_operation (request_id, operation, executor_id, result_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (request_id, operation, executor_id, json.dumps(result, sort_keys=True),
             datetime.now(timezone.utc).isoformat()),
        )

    @staticmethod
    def _validate_request(request_id: str, ttl: int) -> None:
        if not request_id or not request_id.strip():
            raise DispatcherError("request_id is required")
        if ttl < 5 or ttl > 86400:
            raise DispatcherError("lease ttl must be between 5 and 86400 seconds")

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
