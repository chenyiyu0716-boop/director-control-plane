# Review Evidence Gate

TASK-021 adds a deterministic boundary between executor completion and task closure.

An executor still stops at `REVIEW`. A separate reviewer submits structured evidence tied to the exact task version, completed lease, executor and execution baseline. Code-changing work uses `evidenceType=commit` and must include the resulting commit. Read-only validation uses `evidenceType=no_change`, an empty `commitRef` and an empty `changedFiles` list; it is bound to the completed lease baseline and never invents a Git object. The gate checks every registered acceptance criterion, test status, changed-file scope and risk finding.

Outcomes:

- `DONE`: low-risk evidence is complete; the task moves once from `REVIEW` to `DONE`.
- `NEEDS_FIX`: evidence is missing, stale, failed, out of scope or inconsistent; the task remains `REVIEW`.
- `OWNER_CONFIRMATION_REQUIRED`: the task or reported finding crosses an Owner gate; the task remains `REVIEW`.

Every evaluation is immutable and idempotent by request ID. Reusing a request ID with different evidence is rejected. A completed commit can close at most one task, preventing the same change from being used as proof for unrelated work. Multiple legitimate no-change tasks may close without a commit, but each still requires its own completed lease, exact acceptance evidence and request ID.

The interface is available through `task review`, `task reviews`, `/api/task-reviews` and `/api/tasks/{id}/reviews`. It does not merge, deploy, publish, modify business projects or approve Owner-gated risk.
