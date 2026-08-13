import subprocess
import tempfile
import unittest
from pathlib import Path

from control_plane.adapters.deployment import evaluate_deployment
from control_plane.config import DeploymentConfig


class DeploymentObserverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.main = self.base / "main"
        self.runtime = self.base / "runtime"
        subprocess.run(["git", "init", "-q", str(self.main)], check=True)
        subprocess.run(["git", "-C", str(self.main), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.main), "config", "user.name", "Test"], check=True)
        (self.main / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.main), "add", "base.txt"], check=True)
        subprocess.run(["git", "-C", str(self.main), "commit", "-qm", "base"], check=True)
        subprocess.run(["git", "-C", str(self.main), "branch", "runtime"], check=True)
        subprocess.run(["git", "-C", str(self.main), "worktree", "add", "-q", str(self.runtime), "runtime"], check=True)
        self.config = DeploymentConfig("director", ["backend", "frontend", "worker"])

    def tearDown(self):
        self.temp.cleanup()

    def runtime_payload(self, roots):
        return {"available": True, "containers": [
            {"service": service, "config_files": str(root / "docker-compose.yml"), "mounts": [
                {"type": "bind", "source": str(root / service), "destination": "/app"},
            ]}
            for service, root in roots.items()
        ]}

    def test_equal_runtime_is_healthy(self):
        result = evaluate_deployment(self.main, self.config, self.runtime_payload({
            "backend": self.main, "frontend": self.main, "worker": self.main,
        }))
        self.assertEqual((result["status"], result["reason"]), ("healthy", "runtime-main-equal"))

    def test_mixed_runtime_sources_are_critical(self):
        result = evaluate_deployment(self.main, self.config, self.runtime_payload({
            "backend": self.runtime, "frontend": self.runtime, "worker": self.main,
        }))
        self.assertEqual((result["status"], result["reason"]), ("critical", "mixed-runtime-sources"))

    def test_diverged_runtime_is_critical(self):
        (self.main / "main.txt").write_text("main\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.main), "add", "main.txt"], check=True)
        subprocess.run(["git", "-C", str(self.main), "commit", "-qm", "main"], check=True)
        (self.runtime / "runtime.txt").write_text("runtime\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.runtime), "add", "runtime.txt"], check=True)
        subprocess.run(["git", "-C", str(self.runtime), "commit", "-qm", "runtime"], check=True)
        result = evaluate_deployment(self.main, self.config, self.runtime_payload({
            "backend": self.runtime, "frontend": self.runtime, "worker": self.runtime,
        }))
        self.assertEqual((result["status"], result["reason"]), ("critical", "runtime-main-diverged"))

    def test_unknown_runtime_fails_closed(self):
        result = evaluate_deployment(self.main, self.config, {
            "available": False, "error": "docker-unavailable", "containers": [],
        })
        self.assertEqual((result["status"], result["reason"]), ("critical", "runtime-source-unknown"))


if __name__ == "__main__":
    unittest.main()
