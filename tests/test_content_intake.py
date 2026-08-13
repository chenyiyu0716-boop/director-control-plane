import tempfile
import threading
import unittest
from pathlib import Path

from control_plane.config import ProjectConfig
from control_plane.domain.models import AgentType
from control_plane.services import ChiefIntakeService, content_intake_from_dict
from control_plane.storage import Repository


class ContentIntakeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = Repository(self.root / "control.sqlite3")
        self.repository.migrate()
        self.repository.upsert_project(ProjectConfig(
            id="julius", name="Julius", kind="business_agent", owner="Owner",
            root=self.root / "julius", ledger=self.root / "ledger.md", status=self.root / "status.json",
            knowledge_roots=[], enabled_agents=list(AgentType),
        ))
        self.service = ChiefIntakeService(self.repository)

    def tearDown(self):
        self.temp.cleanup()

    def intake(self, **updates):
        payload = {
            "projectId": "julius", "intakeType": "candidate_research", "subjectName": "仲树",
            "objective": "Assess whether the source can support a Julius story candidate.",
            "targetStage": "story_ready",
            "sources": [{"sourceUri": "https://example.test/interview", "title": "Interview"}],
            "assertions": [
                {"kind": "fact", "statement": "An interview source was provided.",
                 "sourceUri": "https://example.test/interview"},
                {"kind": "analysis", "statement": "Check attribution before promotion."},
            ],
        }
        payload.update(updates)
        return content_intake_from_dict(payload)

    def test_submit_creates_one_draft_without_touching_project(self):
        result = self.service.submit(self.intake(), "chief", "intake-1")
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["task"]["state"], "DRAFT")
        self.assertEqual(result["intake"]["target_stage"], "story_ready")
        self.assertFalse((self.root / "julius").exists())
        self.assertIn("Do not produce Story Ready", result["task"]["scope"][2])

    def test_same_subject_and_source_is_deduplicated(self):
        first = self.service.submit(self.intake(), "chief", "intake-1")
        duplicate = self.service.submit(self.intake(objective="A revised request"), "chief", "intake-2")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["task"]["id"], duplicate["task"]["id"])
        self.assertEqual(len(self.repository.list_content_intakes("julius")), 1)

    def test_concurrent_duplicate_creates_one_intake_and_one_task(self):
        barrier = threading.Barrier(3)
        outcomes = []

        def submit(request_id):
            barrier.wait()
            outcomes.append(self.service.submit(self.intake(), "chief", request_id))

        threads = [
            threading.Thread(target=submit, args=("intake-race-1",)),
            threading.Thread(target=submit, args=("intake-race-2",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len(self.repository.list_content_intakes("julius")), 1)
        tasks = self.repository.list_tasks("julius")
        self.assertEqual(len(tasks), 1)
        self.assertEqual({item["task"]["id"] for item in outcomes}, {tasks[0]["id"]})

    def test_fact_and_quote_require_registered_source(self):
        with self.assertRaisesRegex(ValueError, "registered source"):
            self.intake(assertions=[{
                "kind": "quote", "statement": "A short quote", "sourceUri": "https://other.test",
            }])

    def test_duplicate_sources_and_source_body_are_rejected(self):
        source = {"sourceUri": "https://example.test/interview"}
        with self.assertRaisesRegex(ValueError, "duplicates"):
            self.intake(sources=[source, source])
        with self.assertRaisesRegex(ValueError, "source bodies"):
            self.intake(assertions=[{"kind": "analysis", "statement": "x" * 1001}])


if __name__ == "__main__":
    unittest.main()
