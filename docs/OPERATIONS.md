# Operations

## Runtime model

Chief v0.1 runs as a local read-only collector plus a local HTTP API.
The collector reads only configured project paths and writes only to its own SQLite database.

Chief is the user-facing product name. The `control_plane` package, `control-panel` project IDs,
`var/control-plane.sqlite3` and the `CONTROL_PLANE_*` environment variables remain compatibility-only
internal identifiers and are not renamed.

## Commands

```bash
export PYTHONPATH="$PWD/src"
python3 -m control_plane.main --config config/projects.local.json init-db
python3 -m control_plane.main --config config/projects.local.json run observer --project director-agent
python3 -m control_plane.main --config config/projects.local.json run-all --trigger schedule
python3 -m control_plane.main --config config/projects.local.json task register --file config/task.example.json --actor codex
python3 -m control_plane.main --config config/projects.local.json task transition TASK-015 --to READY --expected-version 1 --actor codex --reason "planning approved"
python3 -m control_plane.main --config config/projects.local.json task list --project control-panel
python3 -m control_plane.main --config config/projects.local.json task history TASK-015
python3 -m control_plane.main --config config/projects.local.json task decide TASK-015 --facts config/decision-facts.example.json --expected-version 1 --actor codex
python3 -m control_plane.main --config config/projects.local.json task decisions TASK-015
python3 -m control_plane.main --config config/projects.local.json task render --project control-panel --output var/TASKS.md
python3 -m control_plane.main --config config/projects.local.json dispatch register-executor workbuddy-hy3 --projects control-panel --max-risk low
python3 -m control_plane.main --config config/projects.local.json dispatch set-baseline control-panel COMMIT_SHA --actor planner
python3 -m control_plane.main --config config/projects.local.json dispatch next workbuddy-hy3
python3 -m control_plane.main --config config/projects.local.json serve
```

Task mutations are CLI-only in TASK-015. Every transition requires the expected version, actor and reason;
dependency gates and the allowed state graph are enforced before a new version is committed. The HTTP API
only exposes task records and transition history for inspection. `config/task.schema.json` is the portable
contract; `config/task.example.json` is a ready-to-copy example.

Decision facts use `config/decision-facts.schema.json`. A decision is applied only to a `DRAFT` or `BLOCKED`
task and requires its expected version. `modelAdvisory` is optional, stored separately, and cannot alter the
outcome. Read-only evidence is available at `/api/decisions` and `/api/tasks/{id}/decisions`.

Dispatcher mutations are local CLI/service operations. Register a project baseline before claiming. Executors
must use a fresh request ID per semantic operation and reuse it only to retry that same operation. Read-only
state is available at `/api/executors`, `/api/baselines` and `/api/leases`. Completion enters REVIEW, never DONE.

The API binds to `127.0.0.1:8765` by default. Do not bind it to a public interface until authentication,
request auditing and deployment security have been implemented and reviewed.

## Feishu Owner channel

Install `requirements-feishu.txt` in the runtime environment, copy the example owner allowlist to
`config/feishu-control.local.json`, and export `FEISHU_APP_ID`, `FEISHU_APP_SECRET`,
`CONTROL_PLANE_CONFIG` and `CONTROL_PLANE_FEISHU_CONFIG`. Start `scripts/run_feishu_control.py` as a
separate service. The `.local.json` file and environment credentials must never be committed.

The process uses the official SDK long connection and only handles `card.action.trigger`. It acknowledges
after a small structured inbox write and processes the event on a background worker. Inspect decisions at
`/api/owner-decisions` and direction inputs at `/api/requirement-intakes`. If the worker stops after a
business write but before updating the inbox status, restarting it safely recognizes the applied event.

Before enabling formal cards, verify all of these with a non-production task: an allowlisted approval,
an unauthorized operator, the same callback twice, an expired callback, a direction preview, and a
direction confirmation. Never send App Secret or tokens through chat.

## Scheduling

Use `scripts/run_daily_check.sh` from a system scheduler. The script is deterministic and does not
open a Codex task. On macOS, the scheduler process needs read permission for every configured project
path. A failed run remains visible as a failed `AgentRun`; do not retry indefinitely.

## Backup and recovery

The only mutable runtime asset is `var/control-plane.sqlite3`. Stop the API and Feishu worker before copying it.

## Deployment source consistency

Projects with a `deployment` configuration are checked against live Docker Compose metadata and bind mounts.
The observer resolves each required service to the Git worktree that actually supplies its source, then compares
that worktree HEAD with the configured project main HEAD. Missing Docker metadata, an unknown source, mixed
worktrees, or a diverged history fails closed and creates a deployment-drift finding. A non-healthy result blocks
release and automatic baseline synchronization; the observer never switches worktrees, restarts containers, or
changes services.
Restoring the database does not modify any connected project. Deleting the database removes Chief
history only and must still be treated as an audited operator action.
