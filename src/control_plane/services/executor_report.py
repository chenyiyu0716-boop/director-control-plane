import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional

from ..domain.models import TaskState
from ..storage.repository import Repository, TaskNotFoundError, TaskVersionConflictError
from .review_gate import completion_evidence_from_dict
from .task_registry import TaskValidationError, _required_text


REPORT_VERSION = "executor-report/1.0.0"
REPORT_FIELDS = {
    "taskId", "taskVersion", "baselineRef", "commitRef", "evidenceType", "executorId",
    "leaseId", "summary", "acceptanceChecks", "testResults", "changedFiles", "riskFindings",
    "provenance",
}


def _provenance_list(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        raise TaskValidationError("provenance must be a list")
    records: List[Dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise TaskValidationError("provenance[{}] must be an object".format(index))
        unknown = sorted(set(item) - {"sourceUri", "sha256", "note"})
        if unknown:
            raise TaskValidationError("unknown provenance fields: {}".format(", ".join(unknown)))
        record = {"sourceUri": _required_text(item.get("sourceUri"), "provenance.sourceUri")}
        if item.get("sha256") is not None:
            digest = _required_text(item["sha256"], "provenance.sha256").lower()
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise TaskValidationError("provenance.sha256 must be a 64-character hexadecimal digest")
            record["sha256"] = digest
        if item.get("note") is not None:
            record["note"] = _required_text(item["note"], "provenance.note")
        records.append(record)
    return records


def executor_report_from_dict(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise TaskValidationError("executor report must be an object")
    unknown = sorted(set(payload) - REPORT_FIELDS)
    if unknown:
        raise TaskValidationError("unknown executor report fields: {}".format(", ".join(unknown)))
    missing = sorted(REPORT_FIELDS - set(payload))
    if missing:
        raise TaskValidationError("executor report is missing: {}".format(", ".join(missing)))
    evidence = completion_evidence_from_dict({
        key: payload[key] for key in (
            "taskId", "taskVersion", "baselineRef", "commitRef", "evidenceType", "executorId",
            "leaseId", "acceptanceChecks", "testResults", "changedFiles", "riskFindings",
        )
    })
    return {
        "reportVersion": REPORT_VERSION,
        **evidence,
        "summary": _required_text(payload["summary"], "summary"),
        "provenance": _provenance_list(payload["provenance"]),
    }


def report_fingerprint(report: Dict[str, Any]) -> str:
    normalized = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def completion_evidence_from_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Project a validated report into the exact Review Gate evidence contract."""
    return completion_evidence_from_dict({
        key: report[key] for key in (
            "taskId", "taskVersion", "baselineRef", "commitRef", "evidenceType", "executorId",
            "leaseId", "acceptanceChecks", "testResults", "changedFiles", "riskFindings",
        )
    })


class ExecutorReportService:
    """Persist executor claims as evidence candidates without changing task state."""

    def __init__(self, repository: Repository):
        self.repository = repository

    def submit(self, report: Dict[str, Any], request_id: Optional[str] = None) -> Dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        fingerprint = report_fingerprint(report)
        replay = self.repository.get_executor_report_by_request(request_id)
        if replay is not None:
            if replay["report_fingerprint"] != fingerprint:
                raise TaskVersionConflictError("executor report request id was used for different content")
            return replay
        task = self.repository.get_task(report["taskId"])
        if task is None:
            raise TaskNotFoundError("task is not registered: {}".format(report["taskId"]))
        if task["state"] != TaskState.REVIEW.value or int(task["version"]) != int(report["taskVersion"]):
            raise TaskVersionConflictError(
                "executor report expected REVIEW v{}, found {} v{}".format(
                    report["taskVersion"], task["state"], task["version"]
                )
            )
        lease = self.repository.get_task_lease(report["leaseId"])
        if lease is None or lease["task_id"] != task["id"]:
            raise TaskValidationError("executor report lease does not belong to the task")
        if lease["executor_id"] != report["executorId"]:
            raise TaskValidationError("executor report identity does not match the lease")
        if lease["baseline_ref"] != report["baselineRef"]:
            raise TaskValidationError("executor report baseline does not match the lease")
        if lease["status"] != "completed":
            raise TaskValidationError("executor report requires a completed lease")
        if report["executorId"] not in task["allowed_executors"]:
            raise TaskValidationError("executor is not allowed for the task")
        roots = [root.rstrip("/") for root in task["workspace_roots"]]
        for path in report["changedFiles"]:
            if not any(path == root or path.startswith(root + "/") for root in roots):
                raise TaskValidationError("executor report changed file is outside task scope")
        return self.repository.create_executor_report(report, fingerprint, request_id)
