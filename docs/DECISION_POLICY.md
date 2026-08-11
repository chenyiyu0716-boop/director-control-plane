# Deterministic Task Decision Policy

Policy version: `decision-policy/1.0.0`

The policy classifies a registered `DRAFT` or `BLOCKED` task as `READY`, `NEEDS_DECISION`, or `BLOCKED`.
It never executes a task and it does not approve a human-gated action.

## Precedence

1. Blocking facts win. Missing safety evidence, an unknown baseline, an unauthorized workspace, incomplete acceptance criteria, or unfinished dependencies produce `BLOCKED`.
2. Owner gates run next. Medium/high/critical risk or any architecture, production, permission, external communication, paid, destructive, release, or scope-expansion action produces `NEEDS_DECISION`.
3. Only a low-risk task that passes every deterministic gate produces `READY`.

## Model boundary

`modelAdvisory` may contain a recommendation and explanation for display. It is stored separately, excluded from the input-fact fingerprint, and never changes the deterministic result.

## Evidence

Every applied decision stores:

- task input and result versions;
- policy version;
- outcome, matched rule IDs, and reasons;
- normalized facts and dependency snapshot;
- SHA-256 input-fact fingerprint;
- actor, request ID, and timestamp;
- optional model advisory.

The decision, state transition, and audit event are committed atomically. Stale task versions and reused request IDs are rejected without a partial state change.

## Scope boundary

TASK-016 does not implement Feishu approval, requirement intake, task leases, executor claiming, or evidence-based completion. Those remain TASK-017, TASK-018, and TASK-021.
