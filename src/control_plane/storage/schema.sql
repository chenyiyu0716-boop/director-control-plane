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

CREATE INDEX IF NOT EXISTS idx_run_project_started ON agent_run(project_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_project_status ON review_item(project_id, status);
CREATE INDEX IF NOT EXISTS idx_finding_fingerprint ON finding(fingerprint);
CREATE INDEX IF NOT EXISTS idx_control_task_project_state_priority ON control_task(project_id, state, priority, updated_at);
CREATE INDEX IF NOT EXISTS idx_control_task_dependency_target ON control_task_dependency(depends_on_task_id);
CREATE INDEX IF NOT EXISTS idx_control_task_transition_task_created ON control_task_transition(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_decision_task_created ON task_decision(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_decision_outcome_created ON task_decision(outcome, created_at DESC);
