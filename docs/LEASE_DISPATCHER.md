# Lease Dispatcher

TASK-018 provides the exclusive claim boundary between READY tasks and external executors. Executors use the local `dispatch` interface; they do not edit the Ledger or SQLite database directly.

## Contract

- `next` returns the first eligible READY task after project, task allowlist and maximum-risk checks.
- `claim` atomically creates one active lease and moves READY to CLAIMED. A project baseline must already be registered and match the claim.
- The first `heartbeat` moves CLAIMED to RUNNING and extends the lease.
- `complete` requires an active RUNNING lease and an unchanged baseline, then moves the task to REVIEW. TASK-021 decides whether REVIEW may become DONE.
- `fail` moves CLAIMED or RUNNING to FAILED and preserves the reason in transition history.
- Two consecutive executor-reported `fail` terminal operations disable that executor profile. A successful
  `complete` resets the consecutive sequence; a Planner must explicitly re-register a disabled executor.
- Expired active leases become `expired`; CLAIMED/RUNNING tasks return to READY at a new version and can be claimed again.

Every mutation uses a request ID. Replaying the same operation returns the stored result; reusing its ID for a different operation is rejected. SQLite `BEGIN IMMEDIATE` plus a partial unique index ensures one active lease per task.

## Security boundary

An executor profile contains an explicit project allowlist, maximum risk and enabled flag. Eligibility also requires the executor ID to appear on the task itself. A free model or external automation never receives broader permission because of price or availability.

The current interface is local CLI/service code. TASK-019 and TASK-020 may wrap it for Cursor or WorkBuddy/Hy3, but must not bypass leases, baseline checks or task versions.
