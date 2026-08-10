from pathlib import Path

from ..adapters.filesystem import REGISTER_ONLY, fingerprint, iter_knowledge_files
from ..config import ProjectConfig
from ..domain.models import AgentResult, Finding, KnowledgeCandidate, RunStatus


class KnowledgeAgent:
    def run(self, project: ProjectConfig) -> AgentResult:
        candidates = []
        findings = []
        project_root = project.root.resolve()
        for path in iter_knowledge_files(project.root, project.knowledge_roots):
            content_hash, payload = fingerprint(path)
            try:
                relative = str(path.relative_to(project_root))
            except ValueError:
                source_root = next((root.resolve() for root in project.knowledge_roots if _within(path, root.resolve())), None)
                relative = "external/{}/{}".format(source_root.name, path.relative_to(source_root)) if source_root else "external/{}".format(path.name)
            kind = "pdf" if path.suffix.lower() in REGISTER_ONLY else "document"
            if payload:
                text = payload.decode("utf-8", errors="replace").strip()
                summary = "{} 文档，{} 字节，{} 行；正文未写入控制面。".format(
                    path.suffix.lower().lstrip(".").upper(), len(payload), len(text.splitlines())
                )
                if not text:
                    findings.append(Finding(
                        category="empty-content", severity="warning", title="知识文件为空",
                        detail=relative, evidence={"path": relative, "content_hash": content_hash},
                        recommendation="确认文件是否应删除或补充内容。",
                    ))
            else:
                summary = "文件已登记；当前格式或大小不进入正文解析。"
            candidates.append(KnowledgeCandidate(
                source_uri=relative,
                content_hash=content_hash,
                title=path.stem,
                summary=summary,
                knowledge_type=kind,
                confidence=0.5 if kind == "pdf" else 0.75,
                tags=[path.suffix.lower().lstrip(".")],
            ))
        return AgentResult(
            status=RunStatus.SUCCEEDED,
            summary="发现 {} 个知识文件，生成 {} 个候选。".format(len(candidates), len(candidates)),
            findings=findings,
            candidates=candidates,
        )


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
