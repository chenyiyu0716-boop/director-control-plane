from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AgentType(str, Enum):
    KNOWLEDGE = "knowledge"
    OBSERVER = "observer"
    RELEASE = "release"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"


class TaskState(str, Enum):
    DRAFT = "DRAFT"
    NEEDS_DECISION = "NEEDS_DECISION"
    READY = "READY"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    REVIEW = "REVIEW"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class TaskPriority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class TaskRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DecisionOutcome(str, Enum):
    READY = "READY"
    NEEDS_DECISION = "NEEDS_DECISION"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class DecisionFacts:
    architecture_change: bool
    production_change: bool
    permission_change: bool
    external_communication: bool
    paid_action: bool
    destructive_action: bool
    release_action: bool
    scope_expansion: bool
    safety_evidence_complete: bool
    baseline_known: bool
    workspace_authorized: bool
    acceptance_complete: bool
    model_advisory: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class ControlTask:
    id: str
    project_id: str
    title: str
    objective: str
    scope: List[str]
    acceptance: List[str]
    priority: TaskPriority
    risk_level: TaskRisk
    allowed_executors: List[str]
    workspace_roots: List[str]
    dependencies: List[str] = field(default_factory=list)
    source_uri: Optional[str] = None


@dataclass(frozen=True)
class Finding:
    category: str
    severity: str
    title: str
    detail: str
    evidence: Dict[str, Any]
    recommendation: Optional[str] = None


@dataclass(frozen=True)
class Check:
    component: str
    name: str
    status: str
    evidence: Dict[str, Any]
    latency_ms: Optional[int] = None


@dataclass(frozen=True)
class KnowledgeCandidate:
    source_uri: str
    content_hash: str
    title: str
    summary: str
    knowledge_type: str
    proposed_action: str = "create"
    confidence: float = 0.7
    tags: List[str] = field(default_factory=list)


@dataclass
class AgentResult:
    status: RunStatus
    summary: str
    findings: List[Finding] = field(default_factory=list)
    checks: List[Check] = field(default_factory=list)
    candidates: List[KnowledgeCandidate] = field(default_factory=list)
    release_report: Optional[Dict[str, Any]] = None
