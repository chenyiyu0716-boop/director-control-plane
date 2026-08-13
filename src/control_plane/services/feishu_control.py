from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from ..storage import Repository


ALLOWED_REQUIREMENT_KINDS = {
    "new_requirement", "direction_change", "priority_change", "pause", "resume", "replan",
}
ALLOWED_PRIORITIES = {"P0", "P1", "P2", "P3"}


class FeishuControlError(Exception):
    pass


class FeishuControlInbox:
    """Fast, transport-neutral inbox for sanitized Feishu card actions."""

    def __init__(self, repository: Repository, owner_open_ids: Iterable[str], escalation_handler: Any = None):
        self.repository = repository
        self.owner_open_ids = frozenset(value for value in owner_open_ids if value)
        if not self.owner_open_ids:
            raise FeishuControlError("at least one owner open_id is required")
        self.escalation_handler = escalation_handler

    def ingest(self, event: Dict[str, Any]) -> Dict[str, Any]:
        required = {"event_id", "event_type", "operator_id", "nonce", "expires_at", "payload"}
        missing = sorted(required - set(event))
        if missing:
            raise FeishuControlError("missing event fields: {}".format(", ".join(missing)))
        for field in ("event_id", "event_type", "operator_id", "nonce", "expires_at"):
            if not isinstance(event[field], str) or not event[field].strip():
                raise FeishuControlError("{} must be a non-empty string".format(field))
        if event["operator_id"] not in self.owner_open_ids:
            return {"accepted": False, "duplicate": False, "status": "unauthorized"}
        if not isinstance(event["payload"], dict):
            raise FeishuControlError("payload must be an object")
        sanitized = dict(event)
        sanitized["payload"] = self._sanitize_payload(event["payload"])
        return self.repository.receive_feishu_event(sanitized)

    def process_pending(self, limit: int = 100, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        current = now or datetime.now(timezone.utc)
        results = []
        for event in self.repository.list_pending_feishu_events(limit):
            try:
                if self._parse_time(event["expires_at"]) <= current:
                    raise FeishuControlError("event expired")
                result = self._apply(event)
                self.repository.finish_feishu_event(event["event_id"], "processed", result)
                results.append({"event_id": event["event_id"], "status": "processed", "result": result})
            except Exception as error:
                result = {"error": str(error), "error_type": type(error).__name__}
                self.repository.finish_feishu_event(event["event_id"], "rejected", result)
                results.append({"event_id": event["event_id"], "status": "rejected", "result": result})
        return results

    def _apply(self, event: Dict[str, Any]) -> Dict[str, Any]:
        payload = event["payload"]
        command = payload.get("command")
        if command == "task_decision":
            existing = self.repository.get_owner_decision_by_event(event["event_id"])
            if existing:
                return {"decision_id": existing["id"], "task": self.repository.get_task(existing["task_id"])}
            return self.repository.apply_owner_decision(
                event["event_id"], self._text(payload, "task_id"), self._integer(payload, "task_version"),
                self._text(payload, "action"), event["operator_id"], self._text(payload, "reason"),
            )
        if command == "escalation_decision":
            if self.escalation_handler is None:
                raise FeishuControlError("escalation decision handler is not configured")
            return self.escalation_handler.apply_decision(
                self._text(payload, "escalation_id"), self._text(payload, "action"),
                event["operator_id"], str(payload.get("reason") or ""),
                str(payload.get("parameters") or ""),
            )
        if command == "requirement_intake":
            existing = self.repository.get_requirement_intake_by_event(event["event_id"])
            if existing:
                return {"intake": existing, "next_action": "owner_confirmation_required"}
            project_id = self._text(payload, "project_id")
            kind = self._text(payload, "kind")
            objective = self._text(payload, "objective")
            priority = payload.get("requested_priority") or None
            if kind not in ALLOWED_REQUIREMENT_KINDS:
                raise FeishuControlError("unsupported requirement kind")
            if priority and priority not in ALLOWED_PRIORITIES:
                raise FeishuControlError("unsupported priority")
            preview = self._build_preview(project_id, kind, objective, priority)
            intake = self.repository.create_requirement_intake(
                event["event_id"], project_id, kind, objective, priority, event["operator_id"], preview,
            )
            return {"intake": intake, "next_action": "owner_confirmation_required"}
        if command == "confirm_intake":
            existing = self.repository.get_requirement_intake_by_event(event["event_id"])
            if existing:
                return {
                    "intake": existing,
                    "next_action": "planner_review" if existing["status"] == "CONFIRMED" else "none",
                }
            intake = self.repository.confirm_requirement_intake(
                event["event_id"], self._text(payload, "intake_id"),
                self._integer(payload, "intake_version"), event["operator_id"],
                self._boolean(payload, "confirm"),
            )
            return {
                "intake": intake,
                "next_action": "planner_review" if intake["status"] == "CONFIRMED" else "none",
            }
        raise FeishuControlError("unsupported command")

    def _build_preview(self, project_id: str, kind: str, objective: str,
                       priority: Optional[str]) -> Dict[str, Any]:
        active_states = {"DRAFT", "NEEDS_DECISION", "READY", "CLAIMED", "RUNNING", "REVIEW", "BLOCKED"}
        affected = [
            {"task_id": task["id"], "state": task["state"], "version": task["version"],
             "title": task["title"], "objective": task["objective"], "priority": task["priority"]}
            for task in self.repository.list_tasks(project_id=project_id, limit=500)
            if task["state"] in active_states
        ]
        pause_candidates = [item["task_id"] for item in affected if item["state"] in {"CLAIMED", "RUNNING"}]
        replacement_candidates = [item["task_id"] for item in affected] if kind in {"direction_change", "replan"} else []
        return {
            "project_id": project_id,
            "kind": kind,
            "objective": objective,
            "requested_priority": priority,
            "affected_active_tasks": affected,
            "affected_goals": [item["objective"] for item in affected],
            "relationships": {
                "pause_candidates": pause_candidates,
                "replacement_candidates": replacement_candidates,
                "requested_priority": priority,
            },
            "proposed_effect": "planner_replan_required",
            "automatic_task_changes": [],
            "recommended_plan": {
                "summary": "Planner 校准目标与现有任务，形成 DRAFT 变更方案后再次走决策策略。",
                "steps": [
                    "Review confirmed requirement intake",
                    "Reconcile affected goals, priorities, pause and replacement relationships",
                    "Create or revise DRAFT tasks",
                    "Run deterministic decision policy before READY",
                ],
            },
            "warnings": [
                "Confirmation does not modify DOING/RUNNING tasks.",
                "Confirmation does not create a READY task; Planner must create a DRAFT task first.",
            ],
        }

    @staticmethod
    def _sanitize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "command", "task_id", "task_version", "action", "reason", "project_id", "kind",
            "objective", "requested_priority", "intake_id", "intake_version", "confirm",
            "escalation_id", "parameters",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise FeishuControlError("unsupported payload fields: {}".format(", ".join(unknown)))
        return {key: payload[key] for key in payload if key in allowed}

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError) as error:
            raise FeishuControlError("expires_at must be ISO-8601") from error
        if parsed.tzinfo is None:
            raise FeishuControlError("expires_at must include a timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _text(payload: Dict[str, Any], name: str) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise FeishuControlError("{} must be a non-empty string".format(name))
        return value.strip()

    @staticmethod
    def _integer(payload: Dict[str, Any], name: str) -> int:
        value = payload.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise FeishuControlError("{} must be a positive integer".format(name))
        return value

    @staticmethod
    def _boolean(payload: Dict[str, Any], name: str) -> bool:
        value = payload.get(name)
        if not isinstance(value, bool):
            raise FeishuControlError("{} must be a boolean".format(name))
        return value
