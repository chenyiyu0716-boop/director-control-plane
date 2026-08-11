# Changelog

## Unreleased

### TASK-018 — Lease-based Executor Dispatcher

- Added executor profiles with project and risk ceilings, registered project baselines, and exclusive task leases.
- Added idempotent next/claim/heartbeat/complete/fail service and CLI operations with atomic concurrency control.
- Added expired-lease recovery, stale-baseline rejection and read-only executor/baseline/lease APIs.
- Completion stops at REVIEW so TASK-021 evidence validation remains authoritative for DONE.

### TASK-017 — Feishu Owner Control Channel

- Added an owner-only Feishu `card.action.trigger` inbox using the official long-connection SDK.
- Added event/nonce idempotency, expiry checks, structured payload allowlisting and audit evidence.
- Added atomic owner decisions for `NEEDS_DECISION` tasks with optimistic task-version checks.
- Added requirement and direction intake previews plus separate confirmation; confirmation leaves active tasks unchanged and routes the record to Planner review.
- Added read-only APIs for owner decisions and requirement intakes, operations documentation, and security tests.
