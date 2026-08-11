import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from control_plane.config import ProjectConfig
from control_plane.domain.models import AgentType, TaskState
from control_plane.main import main
from control_plane.services import IllegalTaskTransitionError, TaskRegistry, task_from_dict
from control_plane.storage import (
    DuplicateTaskError,
    Repository,
    TaskDependencyBlockedError,
    TaskVersionConflictError,
)


class TaskRegistryTest(unittest.TestCase):
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

    def tearDown(self):
        self.temp.cleanup()

    def task(self, task_id="TASK-015", dependencies=None, title=None):
        return task_from_dict({
            "id": task_id,
            "projectId": "panel",
            "title": title or "Build task registry",
            "objective": "Create an auditable task source of truth.",
            "scope": ["Schema", "State transitions", "Markdown projection"],
            "acceptance": ["Reject illegal transitions", "Rebuild state from history"],
            "priority": "P0",
            "riskLevel": "medium",
            "allowedExecutors": ["codex", "workbuddy-hy3"],
            "workspaceRoots": [str(self.root)],
            "dependencies": dependencies or [],
            "sourceUri": "docs/control-plane/TASK_QUEUE.md#{}".format(task_id.lower()),
        })

    def transition_to_done(self, task_id):
        steps = [
            (TaskState.READY, 1),
            (TaskState.CLAIMED, 2),
            (TaskState.RUNNING, 3),
            (TaskState.REVIEW, 4),
            (TaskState.DONE, 5),
        ]
        for state, version in steps:
            self.registry.transition(task_id, state, version, "codex", "acceptance step")

    def test_registration_is_unique_and_audited(self):
        task = self.registry.register(self.task(), "codex", request_id="register-015")
        self.assertEqual((task["state"], task["version"]), ("DRAFT", 1))
        with self.assertRaises(DuplicateTaskError):
            self.registry.register(self.task(), "codex", request_id="register-duplicate")
        history = self.repository.list_task_transitions("TASK-015")
        audit = self.repository.list_rows("audit_event")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["request_id"], "register-015")
        self.assertTrue(any(item["action"] == "task.registered" for item in audit))

    def test_illegal_and_stale_transitions_are_rejected(self):
        self.registry.register(self.task(), "codex")
        with self.assertRaises(IllegalTaskTransitionError):
            self.registry.transition("TASK-015", TaskState.RUNNING, 1, "codex", "skip claim")
        self.registry.transition("TASK-015", TaskState.READY, 1, "codex", "ready")
        with self.assertRaises(TaskVersionConflictError):
            self.registry.transition("TASK-015", TaskState.CLAIMED, 1, "codex", "stale claim")
        stored = self.repository.get_task("TASK-015")
        self.assertEqual((stored["state"], stored["version"]), ("READY", 2))

    def test_dependencies_must_be_done_before_ready(self):
        self.registry.register(self.task("TASK-014", title="Prerequisite"), "codex")
        self.registry.register(self.task("TASK-015", dependencies=["TASK-014"]), "codex")
        with self.assertRaises(TaskDependencyBlockedError):
            self.registry.transition("TASK-015", TaskState.READY, 1, "codex", "blocked")
        self.transition_to_done("TASK-014")
        task = self.registry.transition("TASK-015", TaskState.READY, 1, "codex", "dependency done")
        self.assertEqual(task["state"], "READY")

    def test_state_is_reconstructible_and_projection_is_deterministic(self):
        self.registry.register(self.task(), "codex")
        self.transition_to_done("TASK-015")
        state, version = self.registry.reconstruct_state("TASK-015")
        self.assertEqual((state, version), (TaskState.DONE, 6))
        first = self.registry.render_markdown("panel")
        second = self.registry.render_markdown("panel")
        self.assertEqual(first, second)
        self.assertIn("## DONE", first)
        self.assertIn("TASK-015", first)
        self.assertIn("`workbuddy-hy3`", first)
        output = self.root / "generated" / "TASKS.md"
        self.registry.render_to_file(output, "panel")
        self.assertEqual(output.read_text(encoding="utf-8"), first)

    def test_cli_register_transition_list_history_and_render(self):
        config_path = self.root / "projects.json"
        task_path = self.root / "task.json"
        output_path = self.root / "TASKS.md"
        config_path.write_text(json.dumps({
            "database": str(self.root / "cli.sqlite3"),
            "projects": [{
                "id": "panel", "name": "Panel", "kind": "control_plane", "owner": "Owner",
                "root": str(self.root), "ledger": "TASK_QUEUE.md", "status": "CURRENT_STATE.md",
                "knowledge_roots": [], "enabled_agents": [],
            }],
        }), encoding="utf-8")
        task_path.write_text(json.dumps({
            "id": "TASK-015", "projectId": "panel", "title": "Registry",
            "objective": "Auditable state", "scope": ["Schema"], "acceptance": ["History"],
            "priority": "P0", "riskLevel": "low", "allowedExecutors": ["codex"],
            "workspaceRoots": [str(self.root)], "dependencies": [],
        }), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["--config", str(config_path), "task", "register", "--file", str(task_path), "--actor", "codex"]), 0)
            self.assertEqual(main(["--config", str(config_path), "task", "transition", "TASK-015", "--to", "READY", "--expected-version", "1", "--actor", "codex", "--reason", "approved"]), 0)
            self.assertEqual(main(["--config", str(config_path), "task", "list", "--state", "READY"]), 0)
            self.assertEqual(main(["--config", str(config_path), "task", "history", "TASK-015"]), 0)
            self.assertEqual(main(["--config", str(config_path), "task", "render", "--output", str(output_path)]), 0)
        self.assertIn('"state": "READY"', stdout.getvalue())
        self.assertIn("TASK-015", output_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
