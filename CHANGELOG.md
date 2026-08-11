# Changelog

## Unreleased

### TASK-017 — Feishu Owner Control Channel

- Added an owner-only Feishu `card.action.trigger` inbox using the official long-connection SDK.
- Added event/nonce idempotency, expiry checks, structured payload allowlisting and audit evidence.
- Added atomic owner decisions for `NEEDS_DECISION` tasks with optimistic task-version checks.
- Added requirement and direction intake previews plus separate confirmation; confirmation leaves active tasks unchanged and routes the record to Planner review.
- Added read-only APIs for owner decisions and requirement intakes, operations documentation, and security tests.
