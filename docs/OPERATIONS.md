# Operations

## Runtime model

Director Control Plane v0.1 runs as a local read-only collector plus a local HTTP API.
The collector reads only configured project paths and writes only to its own SQLite database.

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
python3 -m control_plane.main --config config/projects.local.json task render --project control-panel --output var/TASKS.md
python3 -m control_plane.main --config config/projects.local.json serve
```

Task mutations are CLI-only in TASK-015. Every transition requires the expected version, actor and reason;
dependency gates and the allowed state graph are enforced before a new version is committed. The HTTP API
only exposes task records and transition history for inspection. `config/task.schema.json` is the portable
contract; `config/task.example.json` is a ready-to-copy example.

The API binds to `127.0.0.1:8765` by default. Do not bind it to a public interface until authentication,
request auditing and deployment security have been implemented and reviewed.

## Scheduling

Use `scripts/run_daily_check.sh` from a system scheduler. The script is deterministic and does not
open a Codex task. On macOS, the scheduler process needs read permission for every configured project
path. A failed run remains visible as a failed `AgentRun`; do not retry indefinitely.

## Backup and recovery

The only mutable runtime asset is `var/control-plane.sqlite3`. Stop the API before copying it.
Restoring the database does not modify any connected project. Deleting the database removes Control
Plane history only and must still be treated as an audited operator action.
