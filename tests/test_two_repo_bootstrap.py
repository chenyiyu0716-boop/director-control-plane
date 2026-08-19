import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from control_plane.adapters.git import git, git_at_root
from control_plane.config import load_settings
from control_plane.domain.models import AgentType
from control_plane.main import main
from control_plane.services import ExecutorUnauthorizedError, LeaseDispatcher, Orchestrator
from control_plane.storage import Repository


REPO = Path(__file__).resolve().parents[1]


class TwoRepoBootstrapTest(unittest.TestCase):
    def test_example_registers_exactly_julius_and_director(self):
        settings = load_settings(str(REPO / "config" / "projects.example.json"))
        self.assertTrue(str(settings.database).endswith("demo-control-plane.sqlite3"))
        self.assertEqual([project.id for project in settings.projects], ["julius", "director-agent"])
        self.assertTrue(all(project.deployment is None for project in settings.projects))
        self.assertEqual(settings.projects[0].root, (REPO / "fixtures" / "demo-julius").resolve())
        self.assertEqual(settings.projects[1].root, (REPO / "fixtures" / "demo-director").resolve())
        self.assertTrue((settings.projects[0].ledger).is_file())
        self.assertTrue((settings.projects[1].status).is_file())

    def test_relative_roots_resolve_from_config_not_cwd(self):
        original = Path.cwd()
        try:
            os.chdir(tempfile.gettempdir())
            settings = load_settings(str(REPO / "config" / "projects.example.json"))
        finally:
            os.chdir(original)
        self.assertEqual(settings.projects[0].root, (REPO / "fixtures" / "demo-julius").resolve())

    def test_readme_first_run_is_observation_not_dispatch(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        first_run = readme.split("### 测试", 1)[0]
        self.assertIn("不替代两仓执行", readme)
        self.assertIn("./scripts/bootstrap_demo.sh", first_run)
        self.assertNotIn("dispatch next", first_run)
        self.assertNotIn("workbuddy-hy3", first_run)

    def test_julius_stays_isolated_after_example_upsert(self):
        database = REPO / "var" / "wrap001-isolation.sqlite3"
        database.parent.mkdir(parents=True, exist_ok=True)
        if database.exists():
            database.unlink()
        os.environ["CONTROL_PLANE_DB"] = str(database)
        try:
            self.assertEqual(main(["--config", str(REPO / "config" / "projects.example.json"), "init-db"]), 0)
            repository = Repository(database)
            dispatcher = LeaseDispatcher(repository)
            with self.assertRaisesRegex(ExecutorUnauthorizedError, "isolated"):
                dispatcher.register_executor("any-executor", ["julius"], "low")
        finally:
            os.environ.pop("CONTROL_PLANE_DB", None)
            if database.exists():
                database.unlink()

    def test_run_all_lists_checks_for_both_demo_projects(self):
        workspace = REPO / "var" / "wrap001-test"
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True)
        try:
            for name in ("demo-julius", "demo-director"):
                source = REPO / "fixtures" / name
                target = workspace / name
                shutil.copytree(source, target)
                subprocess.run(["git", "init", "-q", str(target)], check=True)
                subprocess.run(["git", "-C", str(target), "config", "user.email", "demo@example.invalid"], check=True)
                subprocess.run(["git", "-C", str(target), "config", "user.name", "Chief Demo"], check=True)
                subprocess.run(["git", "-C", str(target), "add", "docs"], check=True)
                subprocess.run(["git", "-C", str(target), "commit", "-qm", "demo fixture"], check=True)
            config_path = workspace / "projects.json"
            config_path.write_text(json.dumps({
                "database": str(workspace / "control-plane.sqlite3"),
                "projects": [
                    {
                        "id": "julius",
                        "name": "Julius",
                        "kind": "business_agent",
                        "owner": "Owner",
                        "root": str(workspace / "demo-julius"),
                        "ledger": "docs/TASK_QUEUE.md",
                        "status": "docs/PROJECT_STATUS.json",
                        "knowledge_roots": ["docs"],
                        "enabled_agents": ["knowledge", "observer", "release"],
                    },
                    {
                        "id": "director-agent",
                        "name": "Director Agent",
                        "kind": "business_agent",
                        "owner": "Owner",
                        "root": str(workspace / "demo-director"),
                        "ledger": "docs/TASK_QUEUE.md",
                        "status": "docs/PROJECT_STATUS.json",
                        "knowledge_roots": ["docs"],
                        "enabled_agents": ["knowledge", "observer", "release"],
                    },
                ],
            }), encoding="utf-8")
            settings = load_settings(str(config_path))
            repository = Repository(settings.database)
            repository.migrate()
            orchestrator = Orchestrator(repository)
            for project in settings.projects:
                repository.upsert_project(project)
                for agent in project.enabled_agents:
                    orchestrator.run(project, agent, "manual")
            projects = {row["id"] for row in repository.list_rows("project")}
            self.assertEqual(projects, {"julius", "director-agent"})
            runs = repository.list_rows("agent_run", 100)
            self.assertEqual({row["project_id"] for row in runs}, {"julius", "director-agent"})
            checks = repository.list_rows("check_result", 100)
            git_checks = [row for row in checks if row["check_name"] == "repository-readable"]
            self.assertEqual(len(git_checks), 2)
            self.assertTrue(all(row["status"] == "healthy" for row in git_checks))
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def test_git_at_root_rejects_parent_repository(self):
        fixture = REPO / "fixtures" / "demo-julius"
        parent = subprocess.check_output(
            ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], text=True,
        ).strip()
        walked = git(fixture, "rev-parse", "--short", "HEAD")
        owned = git_at_root(fixture, "rev-parse", "--short", "HEAD")
        if (fixture / ".git").exists():
            self.assertIsNotNone(owned)
            self.assertNotEqual(owned, parent)
        else:
            self.assertEqual(walked, parent)
            self.assertIsNone(owned)

    def test_in_tree_example_observer_does_not_report_parent_head(self):
        parent = subprocess.check_output(
            ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], text=True,
        ).strip()
        settings = load_settings(str(REPO / "config" / "projects.example.json"))
        database = REPO / "var" / "wrap001-intree.sqlite3"
        database.parent.mkdir(parents=True, exist_ok=True)
        if database.exists():
            database.unlink()
        try:
            repository = Repository(database)
            repository.migrate()
            orchestrator = Orchestrator(repository)
            for project in settings.projects:
                repository.upsert_project(project)
                orchestrator.run(project, AgentType.OBSERVER, "manual")
            runs = {row["id"]: row["project_id"] for row in repository.list_rows("agent_run", 100)}
            checks = [
                row for row in repository.list_rows("check_result", 100)
                if row["check_name"] == "repository-readable"
            ]
            self.assertEqual(len(checks), 2)
            self.assertEqual(set(runs.values()), {"julius", "director-agent"})
            for row in checks:
                evidence = json.loads(row["evidence_json"])
                self.assertNotEqual(evidence.get("head"), parent)
                project_id = runs[row["run_id"]]
                fixture = REPO / "fixtures" / ("demo-julius" if project_id == "julius" else "demo-director")
                if (fixture / ".git").exists():
                    self.assertEqual(row["status"], "healthy")
                    self.assertTrue(evidence.get("head"))
                else:
                    self.assertEqual(row["status"], "unknown")
                    self.assertIsNone(evidence.get("head"))
        finally:
            if database.exists():
                database.unlink()


if __name__ == "__main__":
    unittest.main()
