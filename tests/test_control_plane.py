import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

from control_plane.api.server import ThreadingHTTPServer, create_handler
from control_plane.config import ProjectConfig
from control_plane.domain.models import AgentType
from control_plane.services import Orchestrator
from control_plane.storage import Repository


class ControlPlaneTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "TASK_QUEUE.md").write_text("# Tasks\n", encoding="utf-8")
        (self.root / "docs" / "PROJECT_STATUS.json").write_text("{}\n", encoding="utf-8")
        (self.root / "docs" / "knowledge.md").write_text("Reusable production knowledge.\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "docs"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "fixture"], check=True)
        self.project = ProjectConfig(
            id="fixture", name="Fixture", kind="business_agent", owner="Owner", root=self.root,
            ledger=self.root / "docs/TASK_QUEUE.md", status=self.root / "docs/PROJECT_STATUS.json",
            knowledge_roots=[self.root / "docs"], enabled_agents=list(AgentType),
        )
        self.repository = Repository(self.root / "control-plane.sqlite3")
        self.repository.migrate()
        self.orchestrator = Orchestrator(self.repository)

    def tearDown(self):
        self.temp.cleanup()

    def test_three_agents_persist_traceable_runs(self):
        run_ids = [self.orchestrator.run(self.project, agent) for agent in AgentType]
        self.assertEqual(len(set(run_ids)), 3)
        runs = self.repository.list_rows("agent_run")
        self.assertEqual(len(runs), 3)
        self.assertTrue(all(item["status"] in {"succeeded", "partial"} for item in runs))
        self.assertGreaterEqual(len(self.repository.list_rows("check_result")), 4)
        self.assertEqual(len(self.repository.list_rows("release_report")), 1)

    def test_knowledge_candidates_are_idempotent_and_reviewable(self):
        self.orchestrator.run(self.project, AgentType.KNOWLEDGE)
        first = self.repository.list_rows("review_item")
        self.orchestrator.run(self.project, AgentType.KNOWLEDGE)
        second = self.repository.list_rows("review_item")
        self.assertGreaterEqual(len(first), 1)
        self.assertEqual(len(first), len(second))
        self.assertTrue(all(item["status"] == "pending" for item in second))

    def test_database_does_not_store_source_body(self):
        secret_marker = "BODY_SHOULD_NOT_BE_IN_DATABASE"
        (self.root / "docs" / "knowledge.md").write_text(secret_marker, encoding="utf-8")
        self.orchestrator.run(self.project, AgentType.KNOWLEDGE)
        raw = (self.root / "control-plane.sqlite3").read_bytes()
        self.assertNotIn(secret_marker.encode("utf-8"), raw)

    def test_local_api_exposes_health_and_runs(self):
        self.orchestrator.run(self.project, AgentType.OBSERVER)
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.repository))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:{}".format(server.server_port)
            with urlopen(base + "/health", timeout=2) as response:
                health = response.read().decode("utf-8")
            with urlopen(base + "/api/runs?limit=5", timeout=2) as response:
                runs = response.read().decode("utf-8")
            self.assertIn('"status": "ok"', health)
            self.assertIn('"items"', runs)
            self.assertIn('"observer"', runs)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
