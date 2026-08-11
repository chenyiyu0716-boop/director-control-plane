# Security Boundary

## v0.1 guarantees

- Project adapters are read-only and never write to connected repositories, knowledge bases or assets.
- Knowledge scanning follows only explicitly configured roots, skips symbolic links and caps each run.
- Source document bodies are not stored in SQLite; only hashes, metadata and structural summaries are kept.
- Git inspection uses read-only commands. Release Agent cannot commit, push, merge, tag or deploy.
- The HTTP API is GET-only and listens on loopback by default.
- Task registration and transitions are local CLI operations with optimistic version checks and append-only audit evidence.
- Task policy uses explicit boolean facts and versioned deterministic rules; incomplete safety facts block execution, and model advice cannot grant READY status.
- Medium, high and critical risk plus architecture, production, permission, external, paid, destructive, release and scope-expansion actions require owner confirmation.
- Executor identities in `allowedExecutors` are policy metadata only; TASK-015 does not implement task claiming or command execution.
- Secrets, tokens, cookies, environment dumps and database contents must not enter findings or audit events.

## Not yet approved for production exposure

- There is no remote authentication or Feishu callback verification yet.
- There is no write executor for approved review items.
- Docker, PostgreSQL, HTTP health and log adapters are not implemented in this first slice.
- The local API must not be exposed outside the host.

## Review requirements

Any future write path requires a separate process identity, explicit approval, version revalidation,
idempotency, rollback evidence and an AuditEvent. Adding a command to an adapter is not sufficient authorization.
