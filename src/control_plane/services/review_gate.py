import hashlib
import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ..domain.models import TaskRisk, TaskState
from ..storage.repository import Repository, TaskNotFoundError, TaskVersionConflictError
from .task_registry import TaskValidationError, _required_text


GATE_VERSION = "review-gate/1.0.0"

OUTCOME_DONE = "DONE"
OUTCOME_NEEDS_FIX = "NEEDS_FIX"
OUTCOME_OWNER_CONFIRMATION_REQUIRED = "OWNER_CONFIRMATION_REQUIRED"

EVIDENCE_FIELDS = (
    "taskId", "taskVersion", "baselineRef", "commitRef", "executorId", "leaseId",
    "acceptanceChecks", "testResults", "changedFiles", "riskFindings",
)

CHECK_STATUSES = ("pass", "fail")

# Every finding below maps to an owner-confirmation category in docs/SECURITY.md.
RISK_FINDING_RULES = (
    ("architecture_change", "Architecture changes require owner confirmation."),
    ("production_change", "Production changes require owner confirmation."),
    ("permission_change", "Permission changes require owner confirmation."),
    ("external_communication", "External communication requires owner confirmation."),
    ("paid_action", "Paid actions require owner confirmation."),
    ("destructive_action", "Destructive actions require owner confirmation."),
    ("release_action", "Release or merge actions require owner confirmation."),
    ("scope_expansion", "Task scope expansion requires owner confirmation."),
    ("high_risk", "High-risk findings require owner confirmation."),
)

RISK_FINDING_CATEGORIES = tuple(category for category, _ in RISK_FINDING_RULES)
COMMIT_REF_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")


def _bounded_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TaskValidationError("{} must be an integer".format(field_name))
    if value < 1:
        raise TaskValidationError("{} must be positive".format(field_name))
    return value


def _object_list(value: Any, field_name: str, allowed_keys: set, required_keys: set) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise TaskValidationError("{} must be a non-empty list".format(field_name))
    items = []
    for index, item in enumerate(value):
        label = "{}[{}]".format(field_name, index)
        if not isinstance(item, dict):
            raise TaskValidationError("{} must be an object".format(label))
        unknown = sorted(set(item) - allowed_keys)
        if unknown:
            raise TaskValidationError("unknown {} fields: {}".format(label, ", ".join(unknown)))
        missing = sorted(required_keys - set(item))
        if missing:
            raise TaskValidationError("{} is missing: {}".format(label, ", ".join(missing)))
        record = {key: _required_text(item[key], "{}.{}".format(label, key)) for key in sorted(item)}
        if record["status"] not in CHECK_STATUSES:
            raise TaskValidationError("{}.status must be one of {}".format(label, ", ".join(CHECK_STATUSES)))
        items.append(record)
    return items


def _path_list(value: Any, field_name: str) -> List[str]:
    if not isinstance(value, list):
        raise TaskValidationError("{} must be a list".format(field_name))
    paths = []
    for item in value:
        path = _required_text(item, field_name)
        if not os.path.isabs(path):
            raise TaskValidationError("{} must contain absolute paths: {}".format(field_name, path))
        paths.append(os.path.normpath(path))
    if len(paths) != len(set(paths)):
        raise TaskValidationError("{} contains duplicates".format(field_name))
    return sorted(paths)


def _category_list(value: Any, field_name: str) -> List[str]:
    if not isinstance(value, list):
        raise TaskValidationError("{} must be a list".format(field_name))
    categories = [_required_text(item, field_name) for item in value]
    unknown = sorted(set(categories) - set(RISK_FINDING_CATEGORIES))
    if unknown:
        raise TaskValidationError("unknown {} categories: {}".format(field_name, ", ".join(unknown)))
    if len(categories) != len(set(categories)):
        raise TaskValidationError("{} contains duplicates".format(field_name))
    return sorted(categories)


def completion_evidence_from_dict(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize an executor completion-evidence document."""
    if not isinstance(payload, dict):
        raise TaskValidationError("completion evidence must be an object")
    unknown = sorted(set(payload) - set(EVIDENCE_FIELDS))
    if unknown:
        raise TaskValidationError("unknown evidence fields: {}".format(", ".join(unknown)))
    missing = sorted(set(EVIDENCE_FIELDS) - set(payload))
    if missing:
        raise TaskValidationError("evidence is missing: {}".format(", ".join(missing)))
    commit_ref = _required_text(payload["commitRef"], "commitRef").lower()
    if not COMMIT_REF_PATTERN.fullmatch(commit_ref):
        raise TaskValidationError("commitRef must be a 7-64 character hexadecimal Git object id")
    return {
        "taskId": _required_text(payload["taskId"], "taskId"),
        "taskVersion": _bounded_int(payload["taskVersion"], "taskVersion"),
        "baselineRef": _required_text(payload["baselineRef"], "baselineRef"),
        "commitRef": commit_ref,
        "executorId": _required_text(payload["executorId"], "executorId"),
        "leaseId": _required_text(payload["leaseId"], "leaseId"),
        "acceptanceChecks": _object_list(
            payload["acceptanceChecks"], "acceptanceChecks",
            {"criterion", "status", "evidence"}, {"criterion", "status", "evidence"},
        ),
        "testResults": _object_list(
            payload["testResults"], "testResults",
            {"name", "status", "command"}, {"name", "status"},
        ),
        "changedFiles": _path_list(payload["changedFiles"], "changedFiles"),
        "riskFindings": _category_list(payload["riskFindings"], "riskFindings"),
    }


def evidence_fingerprint(evidence: Dict[str, Any], gate_version: str = GATE_VERSION) -> str:
    normalized = json.dumps(
        {"gateVersion": gate_version, "evidence": evidence},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _in_scope(path: str, roots: List[str]) -> bool:
    for root in roots:
        normalized_root = os.path.normpath(root)
        if path == normalized_root or path.startswith(normalized_root.rstrip(os.sep) + os.sep):
            return True
    return False


class ReviewGate:
    """Deterministic REVIEW to DONE evidence gate.

    The gate never claims leases, never touches business projects and never
    publishes. It only reads registered task state plus a submitted evidence
    document, and it can move exactly one REVIEW task to DONE on a full pass.
    """

    def __init__(self, repository: Repository, gate_version: str = GATE_VERSION):
        self.repository = repository
        self.gate_version = gate_version

    def evaluate(self, evidence: Dict[str, Any]) -> Dict[str, Any]:
        task = self.repository.get_task(evidence["taskId"])
        if task is None:
            raise TaskNotFoundError("task is not registered: {}".format(evidence["taskId"]))
        fix_rules, fix_reasons = self._blocking_rules(task, evidence)
        if fix_rules:
            outcome, rules, reasons = OUTCOME_NEEDS_FIX, fix_rules, fix_reasons
        else:
            owner_rules, owner_reasons = self._owner_rules(task, evidence)
            if owner_rules:
                outcome, rules, reasons = OUTCOME_OWNER_CONFIRMATION_REQUIRED, owner_rules, owner_reasons
            else:
                outcome = OUTCOME_DONE
                rules = ["auto.evidence_complete"]
                reasons = ["Low-risk evidence satisfied every registered acceptance criterion."]
        return {
            "taskId": task["id"],
            "taskVersion": task["version"],
            "taskState": task["state"],
            "gateVersion": self.gate_version,
            "outcome": outcome,
            "matchedRules": rules,
            "reasons": reasons,
            "evidenceFingerprint": evidence_fingerprint(evidence, self.gate_version),
        }

    def review(self, evidence: Dict[str, Any], actor: str,
               request_id: Optional[str] = None) -> Dict[str, Any]:
        actor = _required_text(actor, "actor")
        request_id = request_id or str(uuid.uuid4())
        replay = self.repository.get_task_review_by_request(request_id)
        if replay is not None:
            fingerprint = evidence_fingerprint(evidence, self.gate_version)
            if replay["task_id"] != evidence["taskId"] or replay["evidence_fingerprint"] != fingerprint:
                raise TaskVersionConflictError("review request id was used for different evidence")
            return {"review": replay, "task": self.repository.get_task(replay["task_id"])}
        evaluation = self.evaluate(evidence)
        return self.repository.apply_review_result(
            task_id=evaluation["taskId"],
            task_version=int(evidence["taskVersion"]),
            outcome=evaluation["outcome"],
            gate_version=self.gate_version,
            reasons=evaluation["reasons"],
            matched_rules=evaluation["matchedRules"],
            evidence=evidence,
            evidence_fingerprint=evaluation["evidenceFingerprint"],
            executor_id=evidence["executorId"],
            lease_id=evidence["leaseId"],
            baseline_ref=evidence["baselineRef"],
            commit_ref=evidence["commitRef"],
            actor=actor,
            request_id=request_id,
        )

    def _blocking_rules(self, task: Dict[str, Any],
                        evidence: Dict[str, Any]) -> Tuple[List[str], List[str]]:
        rules: List[str] = []
        reasons: List[str] = []

        def add(rule_id: str, reason: str) -> None:
            rules.append(rule_id)
            reasons.append(reason)

        if task["state"] != TaskState.REVIEW.value:
            add("fix.task_not_in_review", "Task is {} and not REVIEW.".format(task["state"]))
        if int(evidence["taskVersion"]) != int(task["version"]):
            add("fix.task_version_stale", "Evidence targets version {} but the task is at version {}.".format(
                evidence["taskVersion"], task["version"]))

        baseline = self.repository.get_project_baseline(task["project_id"])
        if baseline is None:
            add("fix.baseline_unknown", "Project baseline is not registered.")
        elif baseline["baseline_ref"] != evidence["baselineRef"]:
            add("fix.baseline_mismatch", "Evidence baseline does not match the registered project baseline.")

        lease = self.repository.get_task_lease(evidence["leaseId"])
        if lease is None or lease["task_id"] != task["id"] or lease["executor_id"] != evidence["executorId"]:
            add("fix.lease_mismatch", "Evidence lease does not belong to this task and executor.")
        elif lease["baseline_ref"] != evidence["baselineRef"]:
            add("fix.lease_baseline_mismatch", "Evidence baseline does not match the lease baseline.")
        elif lease["status"] != "completed":
            add("fix.lease_not_completed", "Evidence lease is {} and not completed.".format(lease["status"]))

        if evidence["executorId"] not in task["allowed_executors"]:
            add("fix.executor_not_allowed", "Executor is not on the task allowlist.")

        registered = list(task["acceptance"])
        failed = [item["criterion"] for item in evidence["acceptanceChecks"] if item["status"] != "pass"]
        if failed:
            add("fix.acceptance_failed", "Acceptance checks reported {} failure(s).".format(len(failed)))
        passed = {item["criterion"] for item in evidence["acceptanceChecks"] if item["status"] == "pass"}
        claimed = {item["criterion"] for item in evidence["acceptanceChecks"]}
        missing = [item for item in registered if item not in passed]
        if missing:
            add("fix.acceptance_incomplete", "{} registered acceptance criterion/criteria lack passing evidence.".format(
                len(missing)))
        unknown = sorted(claimed - set(registered))
        if unknown:
            add("fix.acceptance_unknown_criterion",
                "Evidence claims {} criterion/criteria that are not registered on the task.".format(len(unknown)))

        failed_tests = [item["name"] for item in evidence["testResults"] if item["status"] != "pass"]
        if failed_tests:
            add("fix.tests_failed", "Test results reported {} failure(s).".format(len(failed_tests)))

        out_of_scope = [path for path in evidence["changedFiles"]
                        if not _in_scope(path, task["workspace_roots"])]
        if out_of_scope:
            add("fix.changed_file_out_of_scope",
                "{} changed file(s) fall outside the authorized workspace roots.".format(len(out_of_scope)))
        return rules, reasons

    @staticmethod
    def _owner_rules(task: Dict[str, Any], evidence: Dict[str, Any]) -> Tuple[List[str], List[str]]:
        rules: List[str] = []
        reasons: List[str] = []
        risk = TaskRisk(task["risk_level"])
        if risk is not TaskRisk.LOW:
            rules.append("owner.risk_requires_owner")
            reasons.append("{} risk tasks require owner confirmation.".format(risk.value.capitalize()))
        findings = set(evidence["riskFindings"])
        for category, reason in RISK_FINDING_RULES:
            if category in findings:
                rules.append("owner.{}".format(category))
                reasons.append(reason)
        return rules, reasons
