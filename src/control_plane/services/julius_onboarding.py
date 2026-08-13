import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from ..domain.models import TaskState
from ..storage import Repository
from .dispatcher import LeaseDispatcher
from .project_isolation import JULIUS_PROJECT_ID
from .task_registry import TaskRegistry


JULIUS_EXECUTOR_ID = "workbuddy-hy3-julius"
JULIUS_CORRECTION_EXECUTOR_ID = "workbuddy-hy3-julius-correction"
JULIUS_REVIEWER_ID = "julius-reviewer"
JULIUS_TASK_PREFIX = "JUL-"
SHADOW_BASELINE_PREFIX = "shadow:git:"


class JuliusIsolationError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def shadow_baseline(git_head: str, paths: List[Path]) -> Dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{7,40}", git_head):
        raise JuliusIsolationError("invalid Julius git head")
    evidence = []
    for path in paths:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise JuliusIsolationError("baseline evidence is not a file: {}".format(resolved))
        evidence.append({"path": str(resolved), "sha256": sha256_file(resolved)})
    return {
        "project_id": JULIUS_PROJECT_ID,
        "baseline_ref": SHADOW_BASELINE_PREFIX + git_head,
        "mode": "read-only-shadow",
        "git_head": git_head,
        "evidence": evidence,
        "limitation": "Dirty working-tree content is evidence only and is not frozen by this baseline.",
    }


def parse_episode_ledger(path: Path) -> List[Dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8")
    candidates = []
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 5 or not re.fullmatch(r"\d{3}", cells[0]):
            continue
        episode, source, question, _, status = cells[:5]
        candidates.append({
            "candidate_id": "JUL-EP-{}".format(episode),
            "episode": episode,
            "source": source,
            "question": question,
            "ledger_status": status,
            "control_state": "CANDIDATE",
            "ready": False,
        })
    return candidates


@dataclass(frozen=True)
class JuliusStatePaths:
    root: Path

    @property
    def planner(self) -> Path:
        return self.root / "planner"

    @property
    def review(self) -> Path:
        return self.root / "review"

    @property
    def escalation(self) -> Path:
        return self.root / "escalation"

    @property
    def evidence(self) -> Path:
        return self.root / "evidence"

    def ensure(self) -> None:
        for target in (self.planner, self.review, self.escalation, self.evidence):
            target.mkdir(parents=True, exist_ok=True)


def run_read_only_shadow(paths: List[Path], output: Path) -> Dict[str, Any]:
    records = []
    for path in paths:
        resolved = Path(path).resolve()
        text = resolved.read_text(encoding="utf-8")
        records.append({
            "path": str(resolved),
            "line_count": len(text.splitlines()),
            "sha256": sha256_file(resolved),
        })
    result = {"project_id": JULIUS_PROJECT_ID, "mode": "read-only-shadow", "files": records}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def review_shadow_task(repository: Repository, task_id: str, evidence_path: Path,
                       expected_paths: List[Path]) -> Dict[str, Any]:
    task = repository.get_task(task_id)
    if not task or task["project_id"] != JULIUS_PROJECT_ID:
        raise JuliusIsolationError("review task is not Julius-owned")
    if task["state"] != TaskState.REVIEW.value:
        raise JuliusIsolationError("review requires REVIEW state")
    evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    expected = [str(Path(path).resolve()) for path in expected_paths]
    actual = [record.get("path") for record in evidence.get("files", [])]
    recomputed = []
    for path in expected_paths:
        resolved = Path(path).resolve()
        text = resolved.read_text(encoding="utf-8")
        recomputed.append({
            "path": str(resolved),
            "line_count": len(text.splitlines()),
            "sha256": sha256_file(resolved),
        })
    valid = (
        evidence.get("project_id") == JULIUS_PROJECT_ID
        and actual == expected
        and evidence.get("files") == recomputed
        and all(record.get("line_count", 0) >= 0 and re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", ""))
                for record in evidence.get("files", []))
    )
    if not valid:
        raise JuliusIsolationError("shadow evidence failed deterministic review")
    registry = TaskRegistry(repository)
    done = registry.transition(
        task_id, TaskState.DONE, task["version"], JULIUS_REVIEWER_ID,
        "deterministic read-only evidence accepted", "julius:review:{}".format(task_id),
    )
    return {"verdict": "PASS", "task": done, "evidence": evidence}


class JuliusIdleGuard:
    def __init__(self, state_path: Path):
        self.state_path = Path(state_path)

    def observe(self, repository: Repository, dispatcher: LeaseDispatcher) -> Dict[str, Any]:
        state = self._read()
        ready = repository.list_tasks(project_id=JULIUS_PROJECT_ID, state=TaskState.READY.value)
        if ready:
            state = {"empty_rounds": 0, "action": "READY_VISIBLE", "task_id": ready[0]["id"]}
        else:
            rounds = int(state.get("empty_rounds", 0)) + 1
            if rounds == 1:
                state = {"empty_rounds": rounds, "action": "PLAN_SAFE_SHADOW"}
            else:
                state = {
                    "empty_rounds": rounds,
                    "action": "OWNER_GATE",
                    "reason": "No Julius READY task after two consecutive polls.",
                }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        return state

    def _read(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {}
        return json.loads(self.state_path.read_text(encoding="utf-8"))


def agent_ops_records(task_id: str, evidence: Dict[str, Any], output_root: Path) -> List[Path]:
    if not task_id.startswith(JULIUS_TASK_PREFIX):
        raise JuliusIsolationError("Julius task id must use JUL-* prefix")
    output_root = Path(output_root)
    records = [
        ("sessions", "session-summary", {
            "title": "Julius isolated shadow verification", "agent": JULIUS_EXECUTOR_ID,
            "objective": "Verify the isolated read-only execution path.",
            "actions": ["Read allowlisted documents", "Record hashes", "Pass deterministic review"],
            "files": [item["path"] for item in evidence["files"]], "outcome": "Shadow task reached DONE.",
            "status": "completed",
        }),
        ("task-executions", "task-execution", {
            "headline": "Read-only Julius shadow completed", "outcome": "DONE",
            "changed": ["Control Plane Julius-only state"], "nextAction": "Owner approval before real tasks.",
        }),
        ("code-reviews", "code-review", {
            "title": "Julius shadow evidence review", "author": JULIUS_EXECUTOR_ID,
            "reviewedBy": JULIUS_REVIEWER_ID, "status": "accepted",
            "changed": "No Julius business files changed.", "purpose": "Verify isolation.",
            "risk": "Shadow baseline does not freeze dirty files.", "verification": "SHA-256 and line counts verified.",
            "filesChanged": 0,
        }),
        ("risks", "risk", {
            "title": "Julius working tree is not a writable baseline", "severity": "high", "status": "open",
            "owner": "Project Owner", "impact": "Dirty content cannot be attributed to an executor.",
            "mitigation": "Keep real execution disabled until Owner approves a clean or content-addressed baseline.",
            "linkedId": task_id,
        }),
    ]
    written = []
    for directory, record_type, body in records:
        target = output_root / directory / "{}-{}.json".format(task_id, record_type)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schemaVersion": "1.0", "projectId": JULIUS_PROJECT_ID,
            "id": "{}-{}".format(task_id, record_type), "recordType": record_type,
            "createdAt": "shadow-run", "source": {"producer": "Control Plane", "taskId": task_id},
            "taskId": task_id, **body,
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(target)
    return written
