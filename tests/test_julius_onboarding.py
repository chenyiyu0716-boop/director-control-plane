import json
import tempfile
import unittest
from pathlib import Path

from control_plane.config import ProjectConfig
from control_plane.domain.models import AgentType
from control_plane.services import (
    BaselineConflictError, DecisionPolicyEngine, ExecutorUnauthorizedError,
    JULIUS_CORRECTION_EXECUTOR_ID, JULIUS_EXECUTOR_ID, JULIUS_PROJECT_ID,
    JuliusIdleGuard, JuliusIsolationError, LeaseDispatcher, TaskRegistry,
    agent_ops_records, decision_facts_from_dict, parse_episode_ledger,
    review_shadow_task, run_read_only_shadow, shadow_baseline, task_from_dict,
)
from control_plane.storage import Repository


SAFE_FACTS = {
    "architectureChange": False,
    "productionChange": False,
    "permissionChange": False,
    "externalCommunication": False,
    "paidAction": False,
    "destructiveAction": False,
    "releaseAction": False,
    "scopeExpansion": False,
    "safetyEvidenceComplete": True,
    "baselineKnown": True,
    "workspaceAuthorized": True,
    "acceptanceComplete": True,
}


class JuliusOnboardingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.julius = self.root / "julius-source"
        self.julius.mkdir()
        self.ledger = self.julius / "10期实验总表.md"
        self.status = self.julius / "production.json"
        self.readme = self.julius / "README.md"
        self.ledger.write_text(
            "| 期数 | 主来源 | 人的问题 | 来源日期 | 状态 |\n"
            "|---:|---|---|---|---|\n"
            "| 001 | 蔡康永 × 罗永浩 | 人何时开始生活？ | 2026-04-29 | 待录音 |\n"
            "| 002 | — | — | — | 候选 |\n", encoding="utf-8",
        )
        self.status.write_text('{"voiceover":{"status":"waiting"}}\n', encoding="utf-8")
        self.readme.write_text("# Julius\nRead-only shadow source.\n", encoding="utf-8")
        self.database = self.root / "julius-state" / "control-plane.sqlite3"
        self.repository = Repository(self.database)
        self.repository.migrate()
        self.repository.upsert_project(ProjectConfig(
            id=JULIUS_PROJECT_ID, name="Julius", kind="business_agent", owner="Owner",
            root=self.julius, ledger=self.ledger, status=self.status,
            knowledge_roots=[], enabled_agents=list(AgentType),
        ))
        self.registry = TaskRegistry(self.repository)
        self.policy = DecisionPolicyEngine(self.repository)
        self.dispatcher = LeaseDispatcher(self.repository, default_ttl_seconds=30)
        self.dispatcher.register_executor(JULIUS_EXECUTOR_ID, [JULIUS_PROJECT_ID], "low")
        self.dispatcher.register_executor(JULIUS_CORRECTION_EXECUTOR_ID, [JULIUS_PROJECT_ID], "low")
        self.baseline = shadow_baseline("213574b", [self.ledger, self.status])
        self.dispatcher.set_project_baseline(
            JULIUS_PROJECT_ID, self.baseline["baseline_ref"], "julius-planner",
        )

    def tearDown(self):
        self.temp.cleanup()

    def register_shadow(self):
        return self.registry.register(task_from_dict({
            "id": "JUL-SHADOW-001", "projectId": JULIUS_PROJECT_ID,
            "title": "Read-only document fingerprint shadow",
            "objective": "Report allowlisted paths, line counts and SHA-256 without source writes.",
            "scope": ["Read README, Ledger and production status", "Write isolated evidence only"],
            "acceptance": ["Exact paths", "Line counts", "SHA-256", "No source changes"],
            "priority": "P0", "riskLevel": "low",
            "allowedExecutors": [JULIUS_EXECUTOR_ID],
            "workspaceRoots": [str(self.julius), str(self.root / "julius-state")],
            "dependencies": [], "sourceUri": "julius://onboarding/shadow-001",
        }), "julius-planner", request_id="julius:register:JUL-SHADOW-001")

    def test_full_shadow_lifecycle_and_agent_ops(self):
        before = {path: path.read_bytes() for path in (self.ledger, self.status, self.readme)}
        draft = self.register_shadow()
        self.assertEqual((draft["state"], draft["version"]), ("DRAFT", 1))
        ready = self.policy.decide(
            draft["id"], decision_facts_from_dict(SAFE_FACTS), 1, "julius-planner",
            request_id="julius:decide:JUL-SHADOW-001",
        )["task"]
        self.assertEqual((ready["state"], ready["version"]), ("READY", 2))
        lease = self.dispatcher.claim(
            ready["id"], JULIUS_EXECUTOR_ID, self.baseline["baseline_ref"], ready["version"],
            "julius:claim:JUL-SHADOW-001",
        )
        self.dispatcher.heartbeat(
            lease["lease_id"], JULIUS_EXECUTOR_ID, "julius:heartbeat:JUL-SHADOW-001",
        )
        evidence_path = self.root / "julius-state" / "evidence" / "JUL-SHADOW-001.json"
        evidence = run_read_only_shadow([self.readme, self.ledger, self.status], evidence_path)
        completed = self.dispatcher.complete(
            lease["lease_id"], JULIUS_EXECUTOR_ID, self.baseline["baseline_ref"],
            "julius:complete:JUL-SHADOW-001",
        )
        self.assertEqual(completed["task_state"], "REVIEW")
        review = review_shadow_task(
            self.repository, ready["id"], evidence_path, [self.readme, self.ledger, self.status],
        )
        self.assertEqual(review["task"]["state"], "DONE")
        ops = agent_ops_records(ready["id"], evidence, self.root / "julius-agent-ops")
        self.assertEqual(len(ops), 4)
        self.assertTrue(all(path.exists() for path in ops))
        self.assertEqual(before, {path: path.read_bytes() for path in before})

        history = self.repository.list_task_transitions(ready["id"])
        self.assertEqual(
            [item["to_state"] for item in history],
            ["DRAFT", "READY", "CLAIMED", "RUNNING", "REVIEW", "DONE"],
        )
        self.assertEqual(history[2]["actor"], JULIUS_EXECUTOR_ID)
        self.assertEqual(history[3]["actor"], JULIUS_EXECUTOR_ID)
        self.assertEqual(history[4]["actor"], JULIUS_EXECUTOR_ID)
        self.assertEqual(history[5]["actor"], "julius-reviewer")
        lease_row = self.repository.list_rows("task_lease")[0]
        self.assertEqual((lease_row["executor_id"], lease_row["status"]), (JULIUS_EXECUTOR_ID, "completed"))

    def test_wrong_project_old_baseline_and_non_allowlisted_executor_are_rejected(self):
        ready = self.policy.decide(
            self.register_shadow()["id"], decision_facts_from_dict(SAFE_FACTS), 1,
            "julius-planner", request_id="julius:decide:reject-fixture",
        )["task"]
        with self.assertRaises(ExecutorUnauthorizedError):
            self.dispatcher.next(JULIUS_EXECUTOR_ID, "control-panel")
        with self.assertRaises(BaselineConflictError):
            self.dispatcher.claim(
                ready["id"], JULIUS_EXECUTOR_ID, "shadow:git:old0000", ready["version"],
                "julius:claim:old-baseline",
            )
        with self.assertRaises(ExecutorUnauthorizedError):
            self.dispatcher.claim(
                ready["id"], JULIUS_CORRECTION_EXECUTOR_ID, self.baseline["baseline_ref"], ready["version"],
                "julius:claim:unlisted",
            )
        self.assertEqual(self.repository.get_task(ready["id"])["state"], "READY")

    def test_database_and_runtime_namespaces_are_isolated(self):
        panel_database = self.root / "panel-state" / "control-plane.sqlite3"
        panel = Repository(panel_database)
        panel.migrate()
        panel.upsert_project(ProjectConfig(
            id="control-panel", name="Panel", kind="control_plane", owner="Owner",
            root=self.root, ledger=self.root / "PANEL.md", status=self.root / "PANEL.json",
            knowledge_roots=[], enabled_agents=[],
        ))
        panel_before = panel.list_rows("control_task")
        self.register_shadow()
        self.assertEqual(panel.list_rows("control_task"), panel_before)
        self.assertEqual(self.repository.list_tasks(project_id="control-panel"), [])
        self.assertNotEqual(self.database, panel_database)

    def test_ledger_candidates_never_become_ready(self):
        candidates = parse_episode_ledger(self.ledger)
        self.assertEqual([item["candidate_id"] for item in candidates], ["JUL-EP-001", "JUL-EP-002"])
        self.assertTrue(all(item["control_state"] == "CANDIDATE" and not item["ready"] for item in candidates))
        self.assertEqual(self.repository.list_tasks(), [])

    def test_two_empty_rounds_emit_owner_gate_and_third_is_not_silent(self):
        guard = JuliusIdleGuard(self.root / "julius-state" / "planner" / "idle.json")
        first = guard.observe(self.repository, self.dispatcher)
        second = guard.observe(self.repository, self.dispatcher)
        third = guard.observe(self.repository, self.dispatcher)
        self.assertEqual(first, {"empty_rounds": 1, "action": "PLAN_SAFE_SHADOW"})
        self.assertEqual(second["action"], "OWNER_GATE")
        self.assertEqual(third["action"], "OWNER_GATE")
        self.assertGreaterEqual(third["empty_rounds"], 3)

    def test_reviewer_rejects_wrong_project_evidence(self):
        ready = self.policy.decide(
            self.register_shadow()["id"], decision_facts_from_dict(SAFE_FACTS), 1,
            "julius-planner", request_id="julius:decide:bad-evidence",
        )["task"]
        lease = self.dispatcher.claim(
            ready["id"], JULIUS_EXECUTOR_ID, self.baseline["baseline_ref"], ready["version"],
            "julius:claim:bad-evidence",
        )
        self.dispatcher.heartbeat(lease["lease_id"], JULIUS_EXECUTOR_ID, "julius:heartbeat:bad-evidence")
        evidence_path = self.root / "julius-state" / "evidence" / "bad.json"
        run_read_only_shadow([self.readme], evidence_path)
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        payload["project_id"] = "control-panel"
        evidence_path.write_text(json.dumps(payload), encoding="utf-8")
        self.dispatcher.complete(
            lease["lease_id"], JULIUS_EXECUTOR_ID, self.baseline["baseline_ref"],
            "julius:complete:bad-evidence",
        )
        with self.assertRaises(JuliusIsolationError):
            review_shadow_task(self.repository, ready["id"], evidence_path, [self.readme])
        self.assertEqual(self.repository.get_task(ready["id"])["state"], "REVIEW")

    def test_reviewer_recomputes_hash_and_line_count(self):
        ready = self.policy.decide(
            self.register_shadow()["id"], decision_facts_from_dict(SAFE_FACTS), 1,
            "julius-planner", request_id="julius:decide:tampered-evidence",
        )["task"]
        lease = self.dispatcher.claim(
            ready["id"], JULIUS_EXECUTOR_ID, self.baseline["baseline_ref"], ready["version"],
            "julius:claim:tampered-evidence",
        )
        self.dispatcher.heartbeat(lease["lease_id"], JULIUS_EXECUTOR_ID, "julius:heartbeat:tampered-evidence")
        evidence_path = self.root / "julius-state" / "evidence" / "tampered.json"
        payload = run_read_only_shadow([self.readme], evidence_path)
        payload["files"][0]["line_count"] += 1
        evidence_path.write_text(json.dumps(payload), encoding="utf-8")
        self.dispatcher.complete(
            lease["lease_id"], JULIUS_EXECUTOR_ID, self.baseline["baseline_ref"],
            "julius:complete:tampered-evidence",
        )
        with self.assertRaises(JuliusIsolationError):
            review_shadow_task(self.repository, ready["id"], evidence_path, [self.readme])
        self.assertEqual(self.repository.get_task(ready["id"])["state"], "REVIEW")


if __name__ == "__main__":
    unittest.main()
