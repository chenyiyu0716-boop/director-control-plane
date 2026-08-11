import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from control_plane.config import ProjectConfig
from control_plane.domain.models import AgentType, TaskState
from control_plane.services import (
    BaselineConflictError, ExecutorUnauthorizedError, LeaseConflictError, LeaseDispatcher,
    TaskRegistry, task_from_dict,
)
from control_plane.storage import Repository


class DispatcherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = Repository(self.root / "control.sqlite3")
        self.repository.migrate()
        for project_id in ("panel", "director"):
            self.repository.upsert_project(ProjectConfig(
                id=project_id, name=project_id, kind="control_plane", owner="Owner", root=self.root,
                ledger=self.root / "TASKS.md", status=self.root / "STATE.md",
                knowledge_roots=[], enabled_agents=list(AgentType),
            ))
        self.registry = TaskRegistry(self.repository)
        self.dispatcher = LeaseDispatcher(self.repository, default_ttl_seconds=30)
        self.now = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
        self.dispatcher._now = lambda: self.now
        self.dispatcher.register_executor("workbuddy-hy3", ["panel"], "low")
        self.dispatcher.register_executor("codex", ["panel", "director"], "critical")
        self.dispatcher.set_project_baseline("panel", "base-001", "planner")

    def tearDown(self):
        self.temp.cleanup()

    def ready_task(self, task_id="TASK-LOW", risk="low", project="panel", executors=None):
        task = self.registry.register(task_from_dict({
            "id": task_id, "projectId": project, "title": task_id, "objective": "Execute safely",
            "scope": ["Scoped work"], "acceptance": ["Evidence"], "priority": "P1",
            "riskLevel": risk, "allowedExecutors": executors or ["workbuddy-hy3", "codex"],
            "workspaceRoots": [str(self.root)], "dependencies": [],
        }), "planner")
        return self.registry.transition(task_id, TaskState.READY, task["version"], "policy", "safe")

    def test_next_and_claim_enforce_project_risk_and_executor_allowlist(self):
        self.ready_task("TASK-HIGH", risk="high")
        low = self.ready_task("TASK-LOW")
        self.assertEqual(self.dispatcher.next("workbuddy-hy3")["id"], "TASK-LOW")
        with self.assertRaises(ExecutorUnauthorizedError):
            self.dispatcher.next("workbuddy-hy3", "director")
        lease = self.dispatcher.claim("TASK-LOW", "workbuddy-hy3", "base-001", low["version"], "claim-1")
        self.assertEqual(lease["status"], "active")
        self.assertEqual(self.repository.get_task("TASK-LOW")["state"], "CLAIMED")
        with self.assertRaises(ExecutorUnauthorizedError):
            self.dispatcher.claim("TASK-HIGH", "workbuddy-hy3", "base-001", 2, "claim-high")

    def test_claim_is_idempotent_and_only_one_concurrent_executor_wins(self):
        task = self.ready_task()
        first = self.dispatcher.claim(task["id"], "codex", "base-001", task["version"], "same-claim")
        self.assertEqual(first, self.dispatcher.claim(task["id"], "codex", "base-001", task["version"], "same-claim"))

        task2 = self.ready_task("TASK-RACE")
        barrier = threading.Barrier(3)
        outcomes = []

        def claim(executor, request_id):
            barrier.wait()
            try:
                outcomes.append(("won", self.dispatcher.claim(
                    task2["id"], executor, "base-001", task2["version"], request_id,
                )["executor_id"]))
            except (LeaseConflictError, ExecutorUnauthorizedError) as error:
                outcomes.append(("lost", type(error).__name__))

        threads = [
            threading.Thread(target=claim, args=("codex", "race-codex")),
            threading.Thread(target=claim, args=("workbuddy-hy3", "race-workbuddy")),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual([item[0] for item in outcomes].count("won"), 1)
        self.assertEqual([item[0] for item in outcomes].count("lost"), 1)

    def test_heartbeat_complete_and_old_baseline_rejection(self):
        task = self.ready_task()
        lease = self.dispatcher.claim(task["id"], "codex", "base-001", task["version"], "claim")
        heartbeat = self.dispatcher.heartbeat(lease["lease_id"], "codex", "heartbeat")
        self.assertEqual(self.repository.get_task(task["id"])["state"], "RUNNING")
        self.dispatcher.set_project_baseline("panel", "base-002", "planner")
        with self.assertRaises(BaselineConflictError):
            self.dispatcher.complete(lease["lease_id"], "codex", "base-001", "complete-stale")
        self.assertEqual(self.repository.get_task(task["id"])["state"], "RUNNING")
        self.assertEqual(heartbeat, self.dispatcher.heartbeat(lease["lease_id"], "codex", "heartbeat"))

    def test_complete_moves_to_review_and_fail_is_auditable(self):
        task = self.ready_task("TASK-COMPLETE")
        lease = self.dispatcher.claim(task["id"], "codex", "base-001", task["version"], "claim-complete")
        self.dispatcher.heartbeat(lease["lease_id"], "codex", "heartbeat-complete")
        result = self.dispatcher.complete(lease["lease_id"], "codex", "base-001", "complete")
        self.assertEqual((result["status"], result["task_state"]), ("completed", "REVIEW"))

        failed_task = self.ready_task("TASK-FAIL")
        failed_lease = self.dispatcher.claim(
            failed_task["id"], "codex", "base-001", failed_task["version"], "claim-fail",
        )
        failed = self.dispatcher.fail(
            failed_lease["lease_id"], "codex", "base-001", "fail", "tests failed",
        )
        self.assertEqual((failed["status"], failed["task_state"]), ("failed", "FAILED"))
        self.assertEqual((failed["consecutive_failures"], failed["executor_enabled"]), (1, True))
        self.assertIn("tests failed", self.repository.list_task_transitions("TASK-FAIL")[-1]["reason"])

    def test_two_consecutive_failures_disable_executor_and_complete_resets_streak(self):
        first = self.ready_task("TASK-FAIL-ONE")
        first_lease = self.dispatcher.claim(
            first["id"], "workbuddy-hy3", "base-001", first["version"], "claim-fail-one",
        )
        first_result = self.dispatcher.fail(
            first_lease["lease_id"], "workbuddy-hy3", "base-001", "fail-one", "bounded probe failed",
        )
        self.assertEqual((first_result["consecutive_failures"], first_result["executor_enabled"]), (1, True))

        successful = self.ready_task("TASK-SUCCESS-RESET")
        success_lease = self.dispatcher.claim(
            successful["id"], "workbuddy-hy3", "base-001", successful["version"], "claim-success-reset",
        )
        self.dispatcher.heartbeat(success_lease["lease_id"], "workbuddy-hy3", "heartbeat-success-reset")
        success_result = self.dispatcher.complete(
            success_lease["lease_id"], "workbuddy-hy3", "base-001", "complete-success-reset",
        )
        self.assertEqual((success_result["consecutive_failures"], success_result["executor_enabled"]), (0, True))

        second = self.ready_task("TASK-FAIL-TWO")
        second_lease = self.dispatcher.claim(
            second["id"], "workbuddy-hy3", "base-001", second["version"], "claim-fail-two",
        )
        second_result = self.dispatcher.fail(
            second_lease["lease_id"], "workbuddy-hy3", "base-001", "fail-two", "bounded probe failed",
        )
        self.assertEqual((second_result["consecutive_failures"], second_result["executor_enabled"]), (1, True))

        third = self.ready_task("TASK-FAIL-THREE")
        third_lease = self.dispatcher.claim(
            third["id"], "workbuddy-hy3", "base-001", third["version"], "claim-fail-three",
        )
        third_result = self.dispatcher.fail(
            third_lease["lease_id"], "workbuddy-hy3", "base-001", "fail-three", "bounded probe failed again",
        )
        self.assertEqual((third_result["consecutive_failures"], third_result["executor_enabled"]), (2, False))
        self.assertEqual(third_result["executor_disabled_reason"], "two consecutive executor failures")
        with self.assertRaises(ExecutorUnauthorizedError):
            self.dispatcher.next("workbuddy-hy3")

    def test_expired_lease_recovers_task_for_new_claim(self):
        task = self.ready_task()
        old = self.dispatcher.claim(task["id"], "codex", "base-001", task["version"], "old-claim", ttl_seconds=5)
        self.dispatcher.heartbeat(old["lease_id"], "codex", "old-heartbeat", ttl_seconds=5)
        self.now += timedelta(seconds=6)
        self.assertEqual(self.dispatcher.recover_expired(), [old["lease_id"]])
        recovered = self.repository.get_task(task["id"])
        self.assertEqual(recovered["state"], "READY")
        with self.assertRaises(LeaseConflictError):
            self.dispatcher.claim(task["id"], "codex", "base-001", task["version"], "old-claim", ttl_seconds=5)
        replacement = self.dispatcher.claim(
            task["id"], "workbuddy-hy3", "base-001", recovered["version"], "replacement-claim",
        )
        self.assertNotEqual(replacement["lease_id"], old["lease_id"])


if __name__ == "__main__":
    unittest.main()
