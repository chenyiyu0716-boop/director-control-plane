import hashlib
import json
import re
from typing import Any, Dict, List

from ..domain.models import ControlTask, TaskPriority, TaskRisk
from ..storage.repository import Repository
from .task_registry import TaskValidationError, _required_text, source_fingerprint


INTAKE_VERSION = "chief-intake/1.0.0"
INTAKE_TYPES = ("knowledge_ingest", "candidate_research")
TARGET_STAGES = ("candidate", "knowledge", "story_ready", "brief")
ASSERTION_KINDS = ("fact", "quote", "analysis")


def _normalize_subject(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _sources(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise TaskValidationError("sources must be a non-empty list")
    records = []
    identities = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise TaskValidationError("sources[{}] must be an object".format(index))
        unknown = sorted(set(item) - {"sourceUri", "title", "sha256"})
        if unknown:
            raise TaskValidationError("unknown source fields: {}".format(", ".join(unknown)))
        record = {"sourceUri": _required_text(item.get("sourceUri"), "sources.sourceUri")}
        if item.get("title") is not None:
            record["title"] = _required_text(item["title"], "sources.title")
        if item.get("sha256") is not None:
            digest = _required_text(item["sha256"], "sources.sha256").lower()
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise TaskValidationError("sources.sha256 must be a 64-character hexadecimal digest")
            record["sha256"] = digest
        identity = (record["sourceUri"], record.get("sha256"))
        if identity in identities:
            raise TaskValidationError("sources contains duplicates")
        identities.add(identity)
        records.append(record)
    return sorted(records, key=lambda item: (item["sourceUri"], item.get("sha256", "")))


def _assertions(value: Any, sources: List[Dict[str, str]]) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        raise TaskValidationError("assertions must be a list")
    source_uris = {item["sourceUri"] for item in sources}
    records = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise TaskValidationError("assertions[{}] must be an object".format(index))
        unknown = sorted(set(item) - {"kind", "statement", "sourceUri"})
        if unknown:
            raise TaskValidationError("unknown assertion fields: {}".format(", ".join(unknown)))
        kind = _required_text(item.get("kind"), "assertions.kind")
        if kind not in ASSERTION_KINDS:
            raise TaskValidationError("assertions.kind must be one of {}".format(", ".join(ASSERTION_KINDS)))
        statement = _required_text(item.get("statement"), "assertions.statement")
        if len(statement) > 1000:
            raise TaskValidationError("assertions.statement must not contain source bodies")
        record = {"kind": kind, "statement": statement}
        if kind in {"fact", "quote"}:
            source_uri = _required_text(item.get("sourceUri"), "assertions.sourceUri")
            if source_uri not in source_uris:
                raise TaskValidationError("fact and quote assertions must reference a registered source")
            record["sourceUri"] = source_uri
        elif item.get("sourceUri") is not None:
            record["sourceUri"] = _required_text(item["sourceUri"], "assertions.sourceUri")
        records.append(record)
    return records


def content_intake_from_dict(payload: Dict[str, Any]) -> Dict[str, Any]:
    fields = {"projectId", "intakeType", "subjectName", "objective", "targetStage", "sources", "assertions"}
    if not isinstance(payload, dict):
        raise TaskValidationError("content intake must be an object")
    unknown = sorted(set(payload) - fields)
    if unknown:
        raise TaskValidationError("unknown content intake fields: {}".format(", ".join(unknown)))
    missing = sorted(fields - set(payload))
    if missing:
        raise TaskValidationError("content intake is missing: {}".format(", ".join(missing)))
    intake_type = _required_text(payload["intakeType"], "intakeType")
    target_stage = _required_text(payload["targetStage"], "targetStage")
    if intake_type not in INTAKE_TYPES:
        raise TaskValidationError("intakeType must be one of {}".format(", ".join(INTAKE_TYPES)))
    if target_stage not in TARGET_STAGES:
        raise TaskValidationError("targetStage must be one of {}".format(", ".join(TARGET_STAGES)))
    subject_name = _required_text(payload["subjectName"], "subjectName")
    sources = _sources(payload["sources"])
    return {
        "intakeVersion": INTAKE_VERSION,
        "projectId": _required_text(payload["projectId"], "projectId"),
        "intakeType": intake_type,
        "subjectName": subject_name,
        "subjectKey": _normalize_subject(subject_name),
        "objective": _required_text(payload["objective"], "objective"),
        "targetStage": target_stage,
        "sources": sources,
        "assertions": _assertions(payload["assertions"], sources),
    }


def intake_fingerprint(intake: Dict[str, Any]) -> str:
    identity = {
        "projectId": intake["projectId"], "subjectKey": intake["subjectKey"],
        "sources": [{"sourceUri": item["sourceUri"], "sha256": item.get("sha256")} for item in intake["sources"]],
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ChiefIntakeService:
    def __init__(self, repository: Repository):
        self.repository = repository

    def submit(self, intake: Dict[str, Any], actor: str, request_id: str) -> Dict[str, Any]:
        fingerprint = intake_fingerprint(intake)
        existing = self.repository.get_content_intake_by_fingerprint(intake["projectId"], fingerprint)
        if existing is not None:
            return {"intake": existing, "task": self.repository.get_task(existing["task_id"]), "duplicate": True}
        task_prefix = "JUL-KNOWLEDGE" if intake["intakeType"] == "knowledge_ingest" else "JUL-RESEARCH"
        task_id = "{}-{}".format(task_prefix, fingerprint[:10].upper())
        project = self.repository.get_project(intake["projectId"])
        if project is None:
            raise TaskValidationError("project is not registered: {}".format(intake["projectId"]))
        project_root = project["config"]["root"]
        task = ControlTask(
            id=task_id, project_id=intake["projectId"],
            title="{}: {}".format(intake["intakeType"], intake["subjectName"]),
            objective=intake["objective"],
            scope=[
                "Read only the registered source references.",
                "Write extracted material to a task-scoped staging area with provenance.",
                "Do not produce Story Ready, Brief, Outline, or Draft before Codex review.",
            ],
            acceptance=[
                "Every factual or quoted claim is traceable to a registered source.",
                "Person attribution is explicit and conflicting identities are BLOCKED.",
                "Executor Report includes provenance and no unregistered source body.",
            ],
            priority=TaskPriority.P1, risk_level=TaskRisk.LOW,
            allowed_executors=["workbuddy-hy3"],
            workspace_roots=[project_root + "/.agent-ops"], dependencies=[],
            source_uri="chief-intake://{}".format(fingerprint),
        )
        return self.repository.create_content_intake_and_task(
            intake, fingerprint, task, source_fingerprint(task), _required_text(actor, "actor"), request_id,
        )
