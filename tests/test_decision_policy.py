import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

from control_plane.api.server import ThreadingHTTPServer, create_handler
from control_plane.config import ProjectConfig
from control_plane.domain.models import AgentType, TaskState
from control_plane.main import main
from control_plane.services import DecisionPolicyEngine, TaskRegistry, TaskValidationError, decision_facts_from_dict, task_from_dict
from control_plane.storage import Repository, TaskVersionConflictError


BASE_FACTS = {
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


class DecisionPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = Repository(self.root / "control-plane.sqlite3")
        self.repository.migrate()
        self.repository.upsert_project(ProjectConfig(
            id="panel", name="Panel", kind="control_plane", owner="Owner", root=self.root,
            ledger=self.root / "TASK_QUEUE.md", status=self.root / "CURRENT_STATE.md",
            knowledge_roots=[], enabled_agents=list(AgentType),
        ))
        self.registry = TaskRegistry(self.repository)
        self.policy = DecisionPolicyEngine(self.repository)

    def tearDown(self):
        self.temp.cleanup()

    def register(self, task_id, risk="low", dependencies=None):
        return self.registry.register(task_from_dict({
            "id": task_id,
            "projectId": "panel",
            "title": "Policy fixture {}".format(task_id),
            "objective": "Classify a task with deterministic safety facts.",
            "scope": ["Policy evaluation"],
            "acceptance": ["Persist an auditable decision"],
            "priority": "P1",
            "riskLevel": risk,
            "allowedExecutors": ["codex"],
            "workspaceRoots": [str(self.root)],
            "dependencies": dependencies or [],
            "sourceUri": "fixtures/{}".format(task_id.lower()),
        }), "test")

    def facts(self, overrides=None, advisory=None):
        payload = dict(BASE_FACTS)
        payload.update(overrides or {})
        if advisory is not None:
            payload["modelAdvisory"] = advisory
        return decision_facts_from_dict(payload)

    def test_twenty_four_policy_fixtures(self):
        fixtures = json.loads(
            (Path(__file__).parent / "fixtures/decision-policy.json").read_text(encoding="utf-8")
        )
        self.assertGreaterEqual(len(fixtures), 20)
        for index, fixture in enumerate(fixtures, start=1):
            with self.subTest(fixture=fixture["name"]):
                task_id = "TASK-FIX-{:03d}".format(index)
                self.register(task_id, fixture["risk"])
                result = self.policy.evaluate(
                    task_id, self.facts(fixture.get("overrides"), fixture.get("advisory"))
                )
                self.assertEqual(result["outcome"], fixture["outcome"])
                self.assertIn(fixture["rule"], result["matchedRules"])
                if fixture["risk"] in {"high", "critical"}:
                    self.assertNotEqual(result["outcome"], "READY")

    def test_decision_is_atomic_versioned_and_audited(self):
        self.register("TASK-016")
        result = self.policy.decide(
            "TASK-016", self.facts(), expected_version=1, actor="codex", request_id="decision-016"
        )
        self.assertEqual((result["task"]["state"], result["task"]["version"]), ("READY", 2))
        decision = result["decision"]
        self.assertEqual(decision["policy_version"], "decision-policy/1.0.0")
        self.assertEqual((decision["task_version"], decision["result_version"]), (1, 2))
        self.assertEqual(decision["request_id"], "decision-016")
        self.assertEqual(len(decision["input_fingerprint"]), 64)
        history = self.repository.list_task_transitions("TASK-016")
        self.assertEqual(history[-1]["to_state"], "READY")
        self.assertTrue(any(
            item["action"] == "task.decision_applied" and item["request_id"] == "decision-016"
            for item in self.repository.list_rows("audit_event")
        ))

    def test_dependencies_force_blocked_then_can_be_reassessed(self):
        self.register("TASK-BASE")
        self.register("TASK-CHILD", dependencies=["TASK-BASE"])
        blocked = self.policy.decide("TASK-CHILD", self.facts(), 1, "codex")
        self.assertEqual(blocked["task"]["state"], "BLOCKED")
        self.assertIn("block.dependencies_incomplete", blocked["decision"]["matched_rules"])
        for state, version in [
            (TaskState.READY, 1), (TaskState.CLAIMED, 2), (TaskState.RUNNING, 3),
            (TaskState.REVIEW, 4), (TaskState.DONE, 5),
        ]:
            self.registry.transition("TASK-BASE", state, version, "codex", "dependency completion")
        ready = self.policy.decide("TASK-CHILD", self.facts(), 2, "codex")
        self.assertEqual((ready["task"]["state"], ready["task"]["version"]), ("READY", 3))
        self.assertEqual(len(self.repository.list_task_decisions("TASK-CHILD")), 2)

    def test_model_advisory_does_not_change_fingerprint_or_outcome(self):
        self.register("TASK-ADVISORY")
        first = self.policy.evaluate("TASK-ADVISORY", self.facts(advisory={
            "recommendation": "BLOCKED", "explanation": "Uncertain."
        }))
        second = self.policy.evaluate("TASK-ADVISORY", self.facts(advisory={
            "recommendation": "READY", "explanation": "Looks safe."
        }))
        self.assertEqual(first["outcome"], "READY")
        self.assertEqual(first["inputFingerprint"], second["inputFingerprint"])

    def test_stale_version_and_invalid_facts_are_rejected(self):
        self.register("TASK-STALE")
        with self.assertRaises(TaskVersionConflictError):
            self.policy.decide("TASK-STALE", self.facts(), 2, "codex")
        self.assertEqual(self.repository.list_task_decisions("TASK-STALE"), [])
        invalid = dict(BASE_FACTS)
        invalid.pop("baselineKnown")
        with self.assertRaises(TaskValidationError):
            decision_facts_from_dict(invalid)
        invalid = dict(BASE_FACTS, unexpected=True)
        with self.assertRaises(TaskValidationError):
            decision_facts_from_dict(invalid)

    def test_reused_request_id_rolls_back_without_partial_state(self):
        self.register("TASK-FIRST")
        self.register("TASK-SECOND")
        self.policy.decide("TASK-FIRST", self.facts(), 1, "codex", request_id="same-request")
        with self.assertRaises(TaskVersionConflictError):
            self.policy.decide("TASK-SECOND", self.facts(), 1, "codex", request_id="same-request")
        second = self.repository.get_task("TASK-SECOND")
        self.assertEqual((second["state"], second["version"]), ("DRAFT", 1))
        self.assertEqual(self.repository.list_task_decisions("TASK-SECOND"), [])
        with self.assertRaises(TaskValidationError):
            self.policy.evaluate("TASK-FIRST", self.facts())

    def test_cli_and_read_only_api_expose_decision_evidence(self):
        config_path = self.root / "projects.json"
        task_path = self.root / "task.json"
        facts_path = self.root / "facts.json"
        config_path.write_text(json.dumps({
            "database": str(self.root / "cli.sqlite3"),
            "projects": [{
                "id": "panel", "name": "Panel", "kind": "control_plane", "owner": "Owner",
                "root": str(self.root), "ledger": "TASK_QUEUE.md", "status": "CURRENT_STATE.md",
                "knowledge_roots": [], "enabled_agents": [],
            }],
        }), encoding="utf-8")
        task_path.write_text(json.dumps({
            "id": "TASK-CLI", "projectId": "panel", "title": "CLI policy", "objective": "Decide",
            "scope": ["Policy"], "acceptance": ["Evidence"], "priority": "P1", "riskLevel": "low",
            "allowedExecutors": ["codex"], "workspaceRoots": [str(self.root)], "dependencies": [],
        }), encoding="utf-8")
        facts_path.write_text(json.dumps(BASE_FACTS), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            main(["--config", str(config_path), "task", "register", "--file", str(task_path), "--actor", "codex"])
            main(["--config", str(config_path), "task", "decide", "TASK-CLI", "--facts", str(facts_path),
                  "--expected-version", "1", "--actor", "codex", "--request-id", "cli-decision"])
            main(["--config", str(config_path), "task", "decisions", "TASK-CLI"])
        self.assertIn('"policy_version": "decision-policy/1.0.0"', stdout.getvalue())

        repository = Repository(self.root / "cli.sqlite3")
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(repository))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:{}".format(server.server_port)
            with urlopen(base + "/api/tasks/TASK-CLI/decisions", timeout=2) as response:
                body = response.read().decode("utf-8")
            self.assertIn('"input_fingerprint"', body)
            self.assertIn('"outcome": "READY"', body)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
