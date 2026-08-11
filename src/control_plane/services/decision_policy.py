import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ..domain.models import DecisionFacts, DecisionOutcome, TaskRisk, TaskState
from ..storage.repository import Repository, TaskNotFoundError, TaskVersionConflictError
from .task_registry import TaskValidationError, _required_text


POLICY_VERSION = "decision-policy/1.0.0"

FACT_FIELDS = {
    "architectureChange": "architecture_change",
    "productionChange": "production_change",
    "permissionChange": "permission_change",
    "externalCommunication": "external_communication",
    "paidAction": "paid_action",
    "destructiveAction": "destructive_action",
    "releaseAction": "release_action",
    "scopeExpansion": "scope_expansion",
    "safetyEvidenceComplete": "safety_evidence_complete",
    "baselineKnown": "baseline_known",
    "workspaceAuthorized": "workspace_authorized",
    "acceptanceComplete": "acceptance_complete",
}

BLOCKING_RULES = (
    ("block.safety_evidence_incomplete", "safety_evidence_complete", "Required safety evidence is incomplete."),
    ("block.baseline_unknown", "baseline_known", "The target baseline is unknown."),
    ("block.workspace_not_authorized", "workspace_authorized", "The requested workspace is outside the authorized boundary."),
    ("block.acceptance_incomplete", "acceptance_complete", "Acceptance criteria are incomplete."),
)

DECISION_RULES = (
    ("decision.architecture_change", "architecture_change", "Architecture changes require owner confirmation."),
    ("decision.production_change", "production_change", "Production changes require owner confirmation."),
    ("decision.permission_change", "permission_change", "Permission changes require owner confirmation."),
    ("decision.external_communication", "external_communication", "External communication requires owner confirmation."),
    ("decision.paid_action", "paid_action", "Paid actions require owner confirmation."),
    ("decision.destructive_action", "destructive_action", "Destructive actions require owner confirmation."),
    ("decision.release_action", "release_action", "Release or merge actions require owner confirmation."),
    ("decision.scope_expansion", "scope_expansion", "Task scope expansion requires owner confirmation."),
)


def decision_facts_from_dict(payload: Dict[str, Any]) -> DecisionFacts:
    if not isinstance(payload, dict):
        raise TaskValidationError("decision facts must be an object")
    allowed = set(FACT_FIELDS) | {"modelAdvisory"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise TaskValidationError("unknown decision fact fields: {}".format(", ".join(unknown)))
    values: Dict[str, Any] = {}
    for external_name, internal_name in FACT_FIELDS.items():
        if external_name not in payload:
            raise TaskValidationError("{} is required".format(external_name))
        if not isinstance(payload[external_name], bool):
            raise TaskValidationError("{} must be a boolean".format(external_name))
        values[internal_name] = payload[external_name]
    advisory = payload.get("modelAdvisory")
    if advisory is not None:
        if not isinstance(advisory, dict):
            raise TaskValidationError("modelAdvisory must be an object")
        unknown_advisory = sorted(set(advisory) - {"recommendation", "explanation"})
        if unknown_advisory:
            raise TaskValidationError("unknown modelAdvisory fields: {}".format(", ".join(unknown_advisory)))
        advisory = {
            key: _required_text(value, "modelAdvisory.{}".format(key))
            for key, value in advisory.items()
        }
    values["model_advisory"] = advisory
    return DecisionFacts(**values)


def normalized_facts(facts: DecisionFacts) -> Dict[str, bool]:
    return {
        external_name: bool(getattr(facts, internal_name))
        for external_name, internal_name in FACT_FIELDS.items()
    }


class DecisionPolicyEngine:
    def __init__(self, repository: Repository, policy_version: str = POLICY_VERSION):
        self.repository = repository
        self.policy_version = policy_version

    def evaluate(self, task_id: str, facts: DecisionFacts) -> Dict[str, Any]:
        task = self.repository.get_task(task_id)
        if task is None:
            raise TaskNotFoundError("task is not registered: {}".format(task_id))
        state = TaskState(task["state"])
        if state not in {TaskState.DRAFT, TaskState.BLOCKED}:
            raise TaskValidationError("policy evaluation requires a DRAFT or BLOCKED task")
        dependencies = self.repository.dependency_snapshot(task_id)
        outcome, rules, reasons = self._classify(task, facts, dependencies)
        facts_payload = normalized_facts(facts)
        fingerprint_payload = {
            "policyVersion": self.policy_version,
            "task": {
                "id": task["id"],
                "projectId": task["project_id"],
                "version": task["version"],
                "riskLevel": task["risk_level"],
                "allowedExecutors": task["allowed_executors"],
                "workspaceRoots": task["workspace_roots"],
            },
            "facts": facts_payload,
            "dependencies": dependencies,
        }
        fingerprint = hashlib.sha256(json.dumps(
            fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        return {
            "taskId": task_id,
            "taskVersion": task["version"],
            "fromState": state.value,
            "outcome": outcome.value,
            "policyVersion": self.policy_version,
            "matchedRules": rules,
            "reasons": reasons,
            "facts": facts_payload,
            "dependencySnapshot": dependencies,
            "modelAdvisory": facts.model_advisory,
            "inputFingerprint": fingerprint,
        }

    def decide(self, task_id: str, facts: DecisionFacts, expected_version: int, actor: str,
               request_id: Optional[str] = None) -> Dict[str, Any]:
        evaluation = self.evaluate(task_id, facts)
        if int(evaluation["taskVersion"]) != expected_version:
            raise TaskVersionConflictError(
                "expected task version {}, found {}".format(expected_version, evaluation["taskVersion"])
            )
        return self.repository.apply_task_decision(
            task_id=task_id,
            from_state=TaskState(evaluation["fromState"]),
            outcome=DecisionOutcome(evaluation["outcome"]),
            expected_version=expected_version,
            policy_version=self.policy_version,
            reasons=evaluation["reasons"],
            matched_rules=evaluation["matchedRules"],
            facts=evaluation["facts"],
            dependency_snapshot=evaluation["dependencySnapshot"],
            advisory=evaluation["modelAdvisory"],
            input_fingerprint=evaluation["inputFingerprint"],
            actor=_required_text(actor, "actor"),
            request_id=request_id or str(uuid.uuid4()),
        )

    def _classify(self, task: Dict[str, Any], facts: DecisionFacts,
                  dependencies: List[Dict[str, Any]]) -> Tuple[DecisionOutcome, List[str], List[str]]:
        blocked_rules = []
        blocked_reasons = []
        for rule_id, field_name, reason in BLOCKING_RULES:
            if not getattr(facts, field_name):
                blocked_rules.append(rule_id)
                blocked_reasons.append(reason)
        pending_dependencies = [item for item in dependencies if item["state"] != TaskState.DONE.value]
        if pending_dependencies:
            blocked_rules.append("block.dependencies_incomplete")
            blocked_reasons.append("Dependencies are not DONE: {}.".format(
                ", ".join("{}={}".format(item["id"], item["state"]) for item in pending_dependencies)
            ))
        if blocked_rules:
            return DecisionOutcome.BLOCKED, blocked_rules, blocked_reasons

        decision_rules = []
        decision_reasons = []
        risk = TaskRisk(task["risk_level"])
        if risk is not TaskRisk.LOW:
            decision_rules.append("decision.risk_requires_owner")
            decision_reasons.append("{} risk tasks require owner confirmation.".format(risk.value.capitalize()))
        for rule_id, field_name, reason in DECISION_RULES:
            if getattr(facts, field_name):
                decision_rules.append(rule_id)
                decision_reasons.append(reason)
        if decision_rules:
            return DecisionOutcome.NEEDS_DECISION, decision_rules, decision_reasons
        return DecisionOutcome.READY, ["auto.low_risk_all_clear"], [
            "Low-risk task passed all deterministic safety gates."
        ]
