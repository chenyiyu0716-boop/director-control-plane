import tempfile
import unittest
import json
from pathlib import Path

from control_plane.config import ProjectConfig
from control_plane.domain.models import AgentType, TaskState
from control_plane.services import (
    LeaseDispatcher, ReviewGate, TaskRegistry, completion_evidence_from_dict, task_from_dict,
)
from control_plane.storage import Repository
from control_plane.storage.repository import TaskVersionConflictError
from control_plane.main import main


class ReviewGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = Repository(self.root / "control.sqlite3")
        self.repository.migrate()
        self.repository.upsert_project(ProjectConfig(
            id="panel", name="Panel", kind="control_plane", owner="Owner", root=self.root,
            ledger=self.root / "TASKS.md", status=self.root / "STATE.md",
            knowledge_roots=[], enabled_agents=list(AgentType),
        ))
        self.registry = TaskRegistry(self.repository)
        self.dispatcher = LeaseDispatcher(self.repository)
        self.dispatcher.register_executor("codex", ["panel"], "critical")
        self.dispatcher.set_project_baseline("panel", "base-001", "planner")
        self.gate = ReviewGate(self.repository)

    def tearDown(self):
        self.temp.cleanup()

    def completed_task(self, task_id="TASK-REVIEW", risk="low", acceptance=None):
        criteria = acceptance or ["Tests pass", "Scope is respected"]
        task = self.registry.register(task_from_dict({
            "id": task_id, "projectId": "panel", "title": task_id,
            "objective": "Finish with evidence", "scope": ["Bounded implementation"],
            "acceptance": criteria, "priority": "P1", "riskLevel": risk,
            "allowedExecutors": ["codex"], "workspaceRoots": [str(self.root)],
            "dependencies": [],
        }), "planner")
        ready = self.registry.transition(task_id, TaskState.READY, task["version"], "policy", "safe")
        lease = self.dispatcher.claim(task_id, "codex", "base-001", ready["version"], "claim-" + task_id)
        self.dispatcher.heartbeat(lease["lease_id"], "codex", "heartbeat-" + task_id)
        result = self.dispatcher.complete(lease["lease_id"], "codex", "base-001", "complete-" + task_id)
        return result, lease, criteria

    def evidence(self, result, lease, criteria, **updates):
        payload = {
            "taskId": result["task_id"], "taskVersion": result["task_version"],
            "baselineRef": "base-001", "commitRef": (result["task_id"].encode().hex() + "0" * 40)[:40],
            "executorId": "codex", "leaseId": lease["lease_id"],
            "acceptanceChecks": [
                {"criterion": item, "status": "pass", "evidence": "verified"} for item in criteria
            ],
            "testResults": [{"name": "unit", "status": "pass", "command": "unittest"}],
            "changedFiles": [str(self.root / "src" / "change.py")], "riskFindings": [],
        }
        payload.update(updates)
        return completion_evidence_from_dict(payload)

    def test_complete_low_risk_evidence_moves_exactly_once_to_done(self):
        result, lease, criteria = self.completed_task()
        evidence = self.evidence(result, lease, criteria)
        first = self.gate.review(evidence, "reviewer", "review-1")
        replay = self.gate.review(evidence, "reviewer", "review-1")
        self.assertEqual(first, replay)
        self.assertEqual(first["task"]["state"], "DONE")
        self.assertEqual(len(self.repository.list_task_reviews("TASK-REVIEW")), 1)
        transitions = self.repository.list_task_transitions("TASK-REVIEW")
        self.assertEqual([item["to_state"] for item in transitions].count("DONE"), 1)

    def test_incomplete_stale_and_out_of_scope_evidence_cannot_mark_done(self):
        result, lease, criteria = self.completed_task()
        evidence = self.evidence(
            result, lease, criteria,
            taskVersion=result["task_version"] - 1,
            changedFiles=[str(self.root.parent / "outside.py")],
            acceptanceChecks=[{"criterion": criteria[0], "status": "fail", "evidence": "failed"}],
        )
        evaluation = self.gate.evaluate(evidence)
        self.assertEqual(evaluation["outcome"], "NEEDS_FIX")
        self.assertIn("fix.task_version_stale", evaluation["matchedRules"])
        self.assertIn("fix.changed_file_out_of_scope", evaluation["matchedRules"])
        with self.assertRaises(TaskVersionConflictError):
            self.gate.review(evidence, "reviewer", "review-stale")
        self.assertEqual(self.repository.get_task("TASK-REVIEW")["state"], "REVIEW")

    def test_owner_gated_risk_does_not_auto_complete(self):
        result, lease, criteria = self.completed_task("TASK-OWNER", risk="medium")
        reviewed = self.gate.review(self.evidence(result, lease, criteria), "reviewer", "review-owner")
        self.assertEqual(reviewed["review"]["outcome"], "OWNER_CONFIRMATION_REQUIRED")
        self.assertEqual(reviewed["task"]["state"], "REVIEW")

    def test_request_replay_with_changed_evidence_is_rejected(self):
        result, lease, criteria = self.completed_task()
        evidence = self.evidence(result, lease, criteria)
        self.gate.review(evidence, "reviewer", "review-replay")
        changed = dict(evidence)
        changed["commitRef"] = "d" * 40
        with self.assertRaises(TaskVersionConflictError):
            self.gate.review(changed, "reviewer", "review-replay")

    def test_same_commit_cannot_complete_two_tasks(self):
        first, first_lease, first_criteria = self.completed_task("TASK-FIRST")
        second, second_lease, second_criteria = self.completed_task("TASK-SECOND")
        shared = "a" * 40
        self.gate.review(
            self.evidence(first, first_lease, first_criteria, commitRef=shared),
            "reviewer", "review-first",
        )
        with self.assertRaises(TaskVersionConflictError):
            self.gate.review(
                self.evidence(second, second_lease, second_criteria, commitRef=shared),
                "reviewer", "review-second",
            )
        self.assertEqual(self.repository.get_task("TASK-SECOND")["state"], "REVIEW")

    def test_no_change_evidence_closes_read_only_tasks_without_fake_commit(self):
        first, first_lease, first_criteria = self.completed_task("TASK-READ-ONLY-ONE")
        second, second_lease, second_criteria = self.completed_task("TASK-READ-ONLY-TWO")
        self.dispatcher.set_project_baseline("panel", "base-002", "planner")
        for result, lease, criteria, request_id in (
            (first, first_lease, first_criteria, "review-read-only-one"),
            (second, second_lease, second_criteria, "review-read-only-two"),
        ):
            evidence = self.evidence(
                result, lease, criteria, evidenceType="no_change", commitRef=None, changedFiles=[],
            )
            reviewed = self.gate.review(evidence, "reviewer", request_id)
            self.assertEqual(reviewed["task"]["state"], "DONE")
            self.assertEqual(reviewed["review"]["commit_ref"], "")

    def test_no_change_evidence_rejects_commit_or_changed_files(self):
        result, lease, criteria = self.completed_task("TASK-READ-ONLY-INVALID")
        with self.assertRaisesRegex(ValueError, "commitRef must be empty"):
            self.evidence(result, lease, criteria, evidenceType="no_change")
        evidence = self.evidence(
            result, lease, criteria, evidenceType="no_change", commitRef=None,
        )
        evaluation = self.gate.evaluate(evidence)
        self.assertIn("fix.no_change_has_changed_files", evaluation["matchedRules"])

    def test_active_lease_and_malformed_evidence_are_rejected(self):
        task = self.registry.register(task_from_dict({
            "id": "TASK-ACTIVE", "projectId": "panel", "title": "Active",
            "objective": "Do work", "scope": ["Work"], "acceptance": ["Done"],
            "priority": "P1", "riskLevel": "low", "allowedExecutors": ["codex"],
            "workspaceRoots": [str(self.root)], "dependencies": [],
        }), "planner")
        ready = self.registry.transition("TASK-ACTIVE", TaskState.READY, task["version"], "policy", "safe")
        lease = self.dispatcher.claim("TASK-ACTIVE", "codex", "base-001", ready["version"], "claim-active")
        payload = {
            "taskId": "TASK-ACTIVE", "taskVersion": lease["task_version"], "baselineRef": "base-001",
            "commitRef": "b" * 40, "executorId": "codex", "leaseId": lease["lease_id"],
            "acceptanceChecks": [{"criterion": "Done", "status": "pass", "evidence": "claimed"}],
            "testResults": [{"name": "unit", "status": "pass"}], "changedFiles": [], "riskFindings": [],
        }
        evaluation = self.gate.evaluate(completion_evidence_from_dict(payload))
        self.assertIn("fix.task_not_in_review", evaluation["matchedRules"])
        self.assertIn("fix.lease_not_completed", evaluation["matchedRules"])
        malformed = dict(payload)
        malformed.pop("commitRef")
        with self.assertRaises(ValueError):
            completion_evidence_from_dict(malformed)
        malformed = dict(payload)
        malformed["commitRef"] = "not-a-git-object"
        with self.assertRaises(ValueError):
            completion_evidence_from_dict(malformed)

    def test_cli_rejects_evidence_for_a_different_task_argument(self):
        result, lease, criteria = self.completed_task()
        evidence_path = self.root / "evidence.json"
        evidence_path.write_text(json.dumps(self.evidence(result, lease, criteria)), encoding="utf-8")
        config_path = self.root / "projects.json"
        config_path.write_text(json.dumps({"database": str(self.repository.database), "projects": []}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not match"):
            main([
                "--config", str(config_path), "task", "review", "TASK-OTHER",
                "--evidence", str(evidence_path), "--actor", "reviewer", "--dry-run",
            ])


if __name__ == "__main__":
    unittest.main()
