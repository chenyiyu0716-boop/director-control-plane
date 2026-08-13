CREATE TABLE IF NOT EXISTS project (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  owner TEXT NOT NULL,
  config_json TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_run (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES project(id),
  agent_type TEXT NOT NULL,
  trigger TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  summary TEXT,
  error_json TEXT
);

CREATE TABLE IF NOT EXISTS finding (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES agent_run(id),
  category TEXT NOT NULL,
  severity TEXT NOT NULL,
  title TEXT NOT NULL,
  detail TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  recommendation TEXT,
  fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_candidate (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES agent_run(id),
  project_id TEXT NOT NULL REFERENCES project(id),
  source_uri TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  knowledge_type TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  proposed_action TEXT NOT NULL,
  target_uri TEXT,
  confidence REAL NOT NULL,
  diff TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(project_id, content_hash)
);

CREATE TABLE IF NOT EXISTS review_item (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES project(id),
  item_type TEXT NOT NULL,
  payload_ref TEXT NOT NULL,
  status TEXT NOT NULL,
  reviewer TEXT,
  reviewed_at TEXT,
  comment TEXT,
  expires_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS check_result (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES agent_run(id),
  component TEXT NOT NULL,
  check_name TEXT NOT NULL,
  status TEXT NOT NULL,
  latency_ms INTEGER,
  evidence_json TEXT NOT NULL,
  observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS release_report (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES agent_run(id),
  repo_ref TEXT NOT NULL,
  branch TEXT,
  head_ref TEXT,
  commit_count INTEGER NOT NULL DEFAULT 0,
  dirty INTEGER NOT NULL DEFAULT 0,
  risk_items_json TEXT NOT NULL,
  notes_draft TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_event (
  id TEXT PRIMARY KEY,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  before_json TEXT,
  after_json TEXT,
  request_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS control_task (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES project(id),
  title TEXT NOT NULL,
  objective TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  acceptance_json TEXT NOT NULL,
  priority TEXT NOT NULL CHECK(priority IN ('P0', 'P1', 'P2', 'P3')),
  state TEXT NOT NULL CHECK(state IN ('DRAFT', 'NEEDS_DECISION', 'READY', 'CLAIMED', 'RUNNING', 'REVIEW', 'DONE', 'BLOCKED', 'FAILED')),
  version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
  risk_level TEXT NOT NULL CHECK(risk_level IN ('low', 'medium', 'high', 'critical')),
  allowed_executors_json TEXT NOT NULL,
  workspace_roots_json TEXT NOT NULL,
  source_uri TEXT,
  source_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, source_fingerprint)
);

CREATE TABLE IF NOT EXISTS control_task_dependency (
  task_id TEXT NOT NULL REFERENCES control_task(id) ON DELETE CASCADE,
  depends_on_task_id TEXT NOT NULL REFERENCES control_task(id),
  created_at TEXT NOT NULL,
  PRIMARY KEY(task_id, depends_on_task_id),
  CHECK(task_id <> depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS control_task_transition (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES control_task(id) ON DELETE CASCADE,
  from_state TEXT,
  to_state TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT NOT NULL,
  previous_version INTEGER NOT NULL,
  result_version INTEGER NOT NULL,
  request_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_decision (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES control_task(id) ON DELETE CASCADE,
  task_version INTEGER NOT NULL,
  result_version INTEGER NOT NULL,
  policy_version TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK(outcome IN ('READY', 'NEEDS_DECISION', 'BLOCKED')),
  reasons_json TEXT NOT NULL,
  matched_rules_json TEXT NOT NULL,
  facts_json TEXT NOT NULL,
  dependency_snapshot_json TEXT NOT NULL,
  advisory_json TEXT,
  input_fingerprint TEXT NOT NULL,
  actor TEXT NOT NULL,
  request_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_review (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES control_task(id) ON DELETE CASCADE,
  task_version INTEGER NOT NULL,
  result_version INTEGER,
  gate_version TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK(outcome IN ('DONE', 'NEEDS_FIX', 'OWNER_CONFIRMATION_REQUIRED')),
  reasons_json TEXT NOT NULL,
  matched_rules_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  evidence_fingerprint TEXT NOT NULL,
  executor_id TEXT NOT NULL,
  lease_id TEXT NOT NULL,
  baseline_ref TEXT NOT NULL,
  commit_ref TEXT,
  actor TEXT NOT NULL,
  request_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS executor_report (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES control_task(id) ON DELETE CASCADE,
  task_version INTEGER NOT NULL,
  report_version TEXT NOT NULL,
  executor_id TEXT NOT NULL,
  lease_id TEXT NOT NULL REFERENCES task_lease(id),
  baseline_ref TEXT NOT NULL,
  report_json TEXT NOT NULL,
  report_fingerprint TEXT NOT NULL,
  request_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS content_intake (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES project(id),
  task_id TEXT NOT NULL UNIQUE REFERENCES control_task(id),
  intake_version TEXT NOT NULL,
  intake_type TEXT NOT NULL CHECK(intake_type IN ('knowledge_ingest', 'candidate_research')),
  subject_name TEXT NOT NULL,
  subject_key TEXT NOT NULL,
  objective TEXT NOT NULL,
  target_stage TEXT NOT NULL CHECK(target_stage IN ('candidate', 'knowledge', 'story_ready', 'brief')),
  sources_json TEXT NOT NULL,
  assertions_json TEXT NOT NULL,
  intake_fingerprint TEXT NOT NULL,
  actor TEXT NOT NULL,
  request_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  UNIQUE(project_id, intake_fingerprint)
);

CREATE TABLE IF NOT EXISTS feishu_inbox_event (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  operator_id TEXT NOT NULL,
  nonce TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  message_ref TEXT,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending', 'processed', 'rejected')),
  result_json TEXT,
  received_at TEXT NOT NULL,
  processed_at TEXT
);

CREATE TABLE IF NOT EXISTS owner_decision (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES control_task(id),
  task_version INTEGER NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('approve', 'reject', 'request_changes')),
  operator_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  event_id TEXT NOT NULL UNIQUE REFERENCES feishu_inbox_event(event_id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requirement_intake (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES project(id),
  kind TEXT NOT NULL CHECK(kind IN ('new_requirement', 'direction_change', 'priority_change', 'pause', 'resume', 'replan')),
  objective TEXT NOT NULL,
  requested_priority TEXT CHECK(requested_priority IS NULL OR requested_priority IN ('P0', 'P1', 'P2', 'P3')),
  operator_id TEXT NOT NULL,
  source_event_id TEXT NOT NULL UNIQUE REFERENCES feishu_inbox_event(event_id),
  status TEXT NOT NULL CHECK(status IN ('PREVIEW_PENDING', 'CONFIRMED', 'REJECTED')),
  preview_json TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
  confirmed_event_id TEXT UNIQUE REFERENCES feishu_inbox_event(event_id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS executor_profile (
  id TEXT PRIMARY KEY,
  project_ids_json TEXT NOT NULL,
  max_risk TEXT NOT NULL CHECK(max_risk IN ('low', 'medium', 'high', 'critical')),
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_baseline (
  project_id TEXT PRIMARY KEY REFERENCES project(id),
  baseline_ref TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_lease (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES control_task(id),
  executor_id TEXT NOT NULL REFERENCES executor_profile(id),
  baseline_ref TEXT NOT NULL,
  claimed_version INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active', 'completed', 'failed', 'expired')),
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatcher_operation (
  request_id TEXT PRIMARY KEY,
  operation TEXT NOT NULL,
  executor_id TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_project_started ON agent_run(project_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_project_status ON review_item(project_id, status);
CREATE INDEX IF NOT EXISTS idx_finding_fingerprint ON finding(fingerprint);
CREATE INDEX IF NOT EXISTS idx_control_task_project_state_priority ON control_task(project_id, state, priority, updated_at);
CREATE INDEX IF NOT EXISTS idx_control_task_dependency_target ON control_task_dependency(depends_on_task_id);
CREATE INDEX IF NOT EXISTS idx_control_task_transition_task_created ON control_task_transition(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_decision_task_created ON task_decision(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_decision_outcome_created ON task_decision(outcome, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_review_task_created ON task_review(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_review_outcome_created ON task_review(outcome, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_executor_report_task_created ON executor_report(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_intake_project_created ON content_intake(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feishu_inbox_status_received ON feishu_inbox_event(status, received_at);
CREATE INDEX IF NOT EXISTS idx_owner_decision_task_created ON owner_decision(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_requirement_intake_project_status ON requirement_intake(project_id, status, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_lease_one_active ON task_lease(task_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_task_lease_executor_status ON task_lease(executor_id, status, expires_at);
