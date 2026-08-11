import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..domain.models import ControlTask, TaskPriority, TaskRisk, TaskState
from ..storage.repository import Repository, TaskNotFoundError


TASK_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
EXECUTOR_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")

ALLOWED_TRANSITIONS = {
    TaskState.DRAFT: {TaskState.NEEDS_DECISION, TaskState.READY, TaskState.BLOCKED},
    TaskState.NEEDS_DECISION: {TaskState.READY, TaskState.BLOCKED},
    TaskState.READY: {TaskState.CLAIMED, TaskState.BLOCKED},
    TaskState.CLAIMED: {TaskState.RUNNING, TaskState.READY, TaskState.BLOCKED, TaskState.FAILED},
    TaskState.RUNNING: {TaskState.REVIEW, TaskState.READY, TaskState.BLOCKED, TaskState.FAILED},
    TaskState.REVIEW: {TaskState.DONE, TaskState.READY, TaskState.BLOCKED, TaskState.FAILED},
    TaskState.BLOCKED: {TaskState.DRAFT, TaskState.NEEDS_DECISION, TaskState.READY},
    TaskState.FAILED: {TaskState.READY, TaskState.BLOCKED},
    TaskState.DONE: set(),
}


class TaskValidationError(ValueError):
    pass


class IllegalTaskTransitionError(ValueError):
    pass


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise TaskValidationError("{} is required".format(field_name))
    return text


def _string_list(value: Any, field_name: str, allow_empty: bool = False) -> List[str]:
    if not isinstance(value, list):
        raise TaskValidationError("{} must be a list".format(field_name))
    result = [_required_text(item, field_name) for item in value]
    if not allow_empty and not result:
        raise TaskValidationError("{} must not be empty".format(field_name))
    if len(result) != len(set(result)):
        raise TaskValidationError("{} contains duplicates".format(field_name))
    return result


def task_from_dict(payload: Dict[str, Any]) -> ControlTask:
    if not isinstance(payload, dict):
        raise TaskValidationError("task payload must be an object")
    task_id = _required_text(payload.get("id"), "id")
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise TaskValidationError("id must be an uppercase hyphenated task id")
    try:
        priority = TaskPriority(_required_text(payload.get("priority"), "priority"))
        risk_level = TaskRisk(_required_text(payload.get("riskLevel"), "riskLevel"))
    except ValueError as error:
        raise TaskValidationError(str(error)) from error
    allowed_executors = _string_list(payload.get("allowedExecutors"), "allowedExecutors")
    if any(not EXECUTOR_PATTERN.fullmatch(item) for item in allowed_executors):
        raise TaskValidationError("allowedExecutors must use lowercase executor ids")
    dependencies = _string_list(payload.get("dependencies", []), "dependencies", allow_empty=True)
    if task_id in dependencies:
        raise TaskValidationError("task cannot depend on itself")
    return ControlTask(
        id=task_id,
        project_id=_required_text(payload.get("projectId"), "projectId"),
        title=_required_text(payload.get("title"), "title"),
        objective=_required_text(payload.get("objective"), "objective"),
        scope=_string_list(payload.get("scope"), "scope"),
        acceptance=_string_list(payload.get("acceptance"), "acceptance"),
        priority=priority,
        risk_level=risk_level,
        allowed_executors=allowed_executors,
        workspace_roots=_string_list(payload.get("workspaceRoots"), "workspaceRoots"),
        dependencies=dependencies,
        source_uri=str(payload["sourceUri"]).strip() if payload.get("sourceUri") else None,
    )


def source_fingerprint(task: ControlTask) -> str:
    normalized = json.dumps({
        "project_id": task.project_id,
        "title": task.title,
        "objective": task.objective,
        "scope": task.scope,
        "acceptance": task.acceptance,
        "priority": task.priority.value,
        "risk_level": task.risk_level.value,
        "allowed_executors": task.allowed_executors,
        "workspace_roots": task.workspace_roots,
        "dependencies": task.dependencies,
        "source_uri": task.source_uri,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class TaskRegistry:
    def __init__(self, repository: Repository):
        self.repository = repository

    def register(self, task: ControlTask, actor: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        self._validate_task(task)
        return self.repository.register_task(
            task, source_fingerprint(task), _required_text(actor, "actor"), request_id or str(uuid.uuid4())
        )

    def transition(self, task_id: str, to_state: TaskState, expected_version: int, actor: str,
                   reason: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        task = self.repository.get_task(task_id)
        if task is None:
            raise TaskNotFoundError("task is not registered: {}".format(task_id))
        from_state = TaskState(task["state"])
        if to_state not in ALLOWED_TRANSITIONS[from_state]:
            raise IllegalTaskTransitionError("{} cannot transition to {}".format(from_state.value, to_state.value))
        if expected_version < 1:
            raise TaskValidationError("expected_version must be positive")
        return self.repository.transition_task(
            task_id, from_state, to_state, expected_version, _required_text(actor, "actor"),
            _required_text(reason, "reason"), request_id or str(uuid.uuid4()),
        )

    def reconstruct_state(self, task_id: str) -> Tuple[TaskState, int]:
        transitions = self.repository.list_task_transitions(task_id)
        if not transitions:
            raise TaskNotFoundError("task has no transition history: {}".format(task_id))
        state: Optional[TaskState] = None
        version = 0
        for transition in transitions:
            if transition["previous_version"] != version:
                raise TaskValidationError("transition history has a version gap")
            expected_from = state.value if state else None
            if transition["from_state"] != expected_from:
                raise TaskValidationError("transition history has a state gap")
            state = TaskState(transition["to_state"])
            version = int(transition["result_version"])
        if state is None:
            raise TaskValidationError("transition history is empty")
        return state, version

    def render_markdown(self, project_id: Optional[str] = None) -> str:
        tasks = self.repository.list_tasks(project_id=project_id, limit=10000)
        lines = [
            "# Control Plane Task Registry",
            "",
            "> Generated from Control Plane state. Do not edit this projection by hand.",
            "",
        ]
        for state in TaskState:
            items = [task for task in tasks if task["state"] == state.value]
            lines.extend(["## {}".format(state.value), ""])
            if not items:
                lines.extend(["No tasks.", ""])
                continue
            for task in items:
                lines.extend([
                    "### {} — {}".format(task["id"], task["title"]),
                    "",
                    "- Project: `{}`".format(task["project_id"]),
                    "- Priority: `{}`".format(task["priority"]),
                    "- Risk: `{}`".format(task["risk_level"]),
                    "- Version: `{}`".format(task["version"]),
                    "- Allowed executors: {}".format(", ".join("`{}`".format(item) for item in task["allowed_executors"])),
                    "- Dependencies: {}".format(", ".join("`{}`".format(item) for item in task["dependencies"]) or "none"),
                    "",
                    "Objective: {}".format(task["objective"]),
                    "",
                    "Scope:",
                    "",
                ])
                lines.extend("- {}".format(item) for item in task["scope"])
                lines.extend(["", "Acceptance:", ""])
                lines.extend("- {}".format(item) for item in task["acceptance"])
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def render_to_file(self, output: Path, project_id: Optional[str] = None) -> None:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(self.render_markdown(project_id), encoding="utf-8")

    def _validate_task(self, task: ControlTask) -> None:
        task_from_dict({
            "id": task.id,
            "projectId": task.project_id,
            "title": task.title,
            "objective": task.objective,
            "scope": task.scope,
            "acceptance": task.acceptance,
            "priority": task.priority.value,
            "riskLevel": task.risk_level.value,
            "allowedExecutors": task.allowed_executors,
            "workspaceRoots": task.workspace_roots,
            "dependencies": task.dependencies,
            "sourceUri": task.source_uri,
        })
