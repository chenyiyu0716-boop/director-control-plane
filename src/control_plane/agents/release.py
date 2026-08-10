from ..adapters.git import git, recent_commits
from ..config import ProjectConfig
from ..domain.models import AgentResult, Finding, RunStatus


class ReleaseAgent:
    def run(self, project: ProjectConfig) -> AgentResult:
        branch = git(project.root, "branch", "--show-current")
        head = git(project.root, "rev-parse", "HEAD")
        status = git(project.root, "status", "--short")
        commits = recent_commits(project.root)
        findings = []
        dirty = status is None or bool(status)
        if dirty:
            findings.append(Finding(
                category="dirty-worktree", severity="warning", title="工作区不是干净发布基线",
                detail="存在未提交变化或无法读取状态。", evidence={"changed_entries": len(status.splitlines()) if status else None},
                recommendation="在独立工作树中拆分、评审并合入，不直接发布当前目录。",
            ))
        report = {
            "repo_ref": str(project.root),
            "branch": branch,
            "head_ref": head,
            "commit_count": len(commits),
            "dirty": dirty,
            "risk_items": [item.title for item in findings],
            "notes_draft": "\n".join("- {}".format(item) for item in commits) or "本周期无新提交。",
        }
        status_value = RunStatus.PARTIAL if not head else RunStatus.SUCCEEDED
        return AgentResult(
            status=status_value,
            summary="Release 检查完成：分支 {}，近 7 天 {} 个提交，工作区{}。".format(branch or "unknown", len(commits), "有变化" if dirty else "干净"),
            findings=findings,
            release_report=report,
        )
