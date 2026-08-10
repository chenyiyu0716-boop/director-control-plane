import time

from ..adapters.git import git
from ..config import ProjectConfig
from ..domain.models import AgentResult, Check, Finding, RunStatus


class ObserverAgent:
    def run(self, project: ProjectConfig) -> AgentResult:
        checks = []
        findings = []
        self._path_check(checks, findings, "project", "project-root", project.root)
        self._path_check(checks, findings, "ledger", "project-ledger", project.ledger)
        self._path_check(checks, findings, "status", "project-status", project.status)
        started = time.monotonic()
        head = git(project.root, "rev-parse", "--short", "HEAD")
        latency = int((time.monotonic() - started) * 1000)
        git_status = "healthy" if head else "unknown"
        checks.append(Check("git", "repository-readable", git_status, {"head": head}, latency))
        if not head:
            findings.append(Finding(
                category="git-unavailable", severity="warning", title="Git 仓库状态未知",
                detail="无法只读获取当前提交。", evidence={"root": str(project.root)},
                recommendation="检查仓库路径与读取权限。",
            ))
        states = [item.status for item in checks]
        overall = "critical" if "critical" in states else "partial" if "unknown" in states or "warning" in states else "healthy"
        return AgentResult(
            status=RunStatus.PARTIAL if overall != "healthy" else RunStatus.SUCCEEDED,
            summary="Observer 完成 {} 项只读检查，总体 {}。".format(len(checks), overall),
            findings=findings,
            checks=checks,
        )

    def _path_check(self, checks, findings, component, name, path):
        started = time.monotonic()
        exists = path.exists()
        latency = int((time.monotonic() - started) * 1000)
        status = "healthy" if exists else "critical"
        checks.append(Check(component, name, status, {"exists": exists, "path": str(path)}, latency))
        if not exists:
            findings.append(Finding(
                category="source-unavailable", severity="critical", title="数据源不可访问",
                detail="{} 不存在或不可读取。".format(name), evidence={"path": str(path)},
                recommendation="恢复只读路径或修正项目配置。",
            ))
