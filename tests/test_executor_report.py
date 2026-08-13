import tempfile
import unittest
from pathlib import Path

from control_plane.config import ProjectConfig
from control_plane.domain.models import AgentType, TaskState
from control_plane.services import (
    ExecutorReportService, LeaseDispatcher, TaskRegistry, completion_evidence_from_report,
    executor_report_from_dict, task_from_dict,
)
from control_plane.storage import Repository, TaskVersionConflictError


class ExecutorReportTest(unittest.TestCase):
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
        self.dispatcher.register_executor("workbuddy-hy3", ["panel"], "low")
        self.dispatcher.set_project_baseline("panel", "base-001", "planner")
        task = self.registry.register(task_from_dict({
            "id": "TASK-REPORT", "projectId": "panel", "title": "Report",
            "objective": "Submit evidence", "scope": ["Read a document"],
            "acceptance": ["Report a digest"], "priority": "P1", "riskLevel": "low",
            "allowedExecutors": ["workbuddy-hy3"], "workspaceRoots": [str(self.root)],
            "dependencies": [],
        }), "planner")
        ready = self.registry.transition("TASK-REPORT", TaskState.READY, task["version"], "policy", "safe")
        self.lease = self.dispatcher.claim(
            "TASK-REPORT", "workbuddy-hy3", "base-001", ready["version"], "claim-report",
        )
        self.dispatcher.heartbeat(self.lease["lease_id"], "workbuddy-hy3", "heartbeat-report")
        self.result = self.dispatcher.complete(
            self.lease["lease_id"], "workbuddy-hy3", "base-001", "complete-report",
        )
        self.service = ExecutorReportService(self.repository)

    def tearDown(self):
        self.temp.cleanup()

    def report(self, **updates):
        payload = {
            "taskId": "TASK-REPORT", "taskVersion": self.result["task_version"],
            "baselineRef": "base-001", "commitRef": None, "evidenceType": "no_change",
            "executorId": "workbuddy-hy3", "leaseId": self.lease["lease_id"],
            "summary": "Read-only probe completed.",
            "acceptanceChecks": [{
                "criterion": "Report a digest", "status": "pass", "evidence": "digest retained",
            }],
            "testResults": [{"name": "probe", "status": "pass", "command": "sha256"}],
            "changedFiles": [], "riskFindings": [],
            "provenance": [{"sourceUri": "docs/example.md", "sha256": "a" * 64}],
        }
        payload.update(updates)
        return executor_report_from_dict(payload)

    def test_submit_is_idempotent_and_does_not_change_review_state(self):
        report = self.report()
        first = self.service.submit(report, "report-1")
        replay = self.service.submit(report, "report-1")
        self.assertEqual(first, replay)
        self.assertEqual(self.repository.get_task("TASK-REPORT")["state"], "REVIEW")
        self.assertEqual(len(self.repository.list_executor_reports("TASK-REPORT")), 1)

    def test_changed_replay_and_wrong_identity_are_rejected(self):
        report = self.report()
        self.service.submit(report, "report-1")
        changed = dict(report)
        changed["summary"] = "changed"
        with self.assertRaises(TaskVersionConflictError):
            self.service.submit(changed, "report-1")
        with self.assertRaisesRegex(ValueError, "identity"):
            self.service.submit(self.report(executorId="codex"), "report-wrong-executor")

    def test_wrong_baseline_and_out_of_scope_changes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "baseline"):
            self.service.submit(self.report(baselineRef="base-002"), "report-wrong-baseline")
        with self.assertRaisesRegex(ValueError, "outside task scope"):
            self.service.submit(self.report(
                evidenceType="commit", commitRef="b" * 40,
                changedFiles=[str(self.root.parent / "outside.py")],
            ), "report-outside")

    def test_provenance_digest_is_validated(self):
        with self.assertRaisesRegex(ValueError, "64-character"):
            self.report(provenance=[{"sourceUri": "docs/example.md", "sha256": "bad"}])

    def test_review_projection_is_deterministic_and_excludes_advisory_fields(self):
        report = self.report()
        first = completion_evidence_from_report(report)
        second = completion_evidence_from_report(report)
        self.assertEqual(first, second)
        self.assertNotIn("summary", first)
        self.assertNotIn("provenance", first)
        self.assertEqual(first["leaseId"], self.lease["lease_id"])


if __name__ == "__main__":
    unittest.main()
