# Julius isolated onboarding progress

- Goal: isolated Julius planning, dispatch, review and Owner-gate shadow loop.
- Prefix selected: `JUL-*`; historical records remain unchanged.
- Source tree: read-only and dirty; no business file is writable in this round.
- Baseline mode: `shadow:git:213574b` plus allowlisted file hashes.
- Runtime boundary: dedicated `var/julius/` database and state directories.
- Executors: `workbuddy-hy3-julius` and `workbuddy-hy3-julius-correction`.
- Feishu: protocol reuse only; real delivery remains disabled.
- Shadow result: `JUL-SHADOW-001` reached DONE through all six required states.
- Negative probes: wrong project, stale baseline and non-allowlisted executor rejected.
- Idle guard: second empty round emitted Owner Gate; third remained explicit.
- Verification: Julius-specific and full repository tests pending final handoff check.
- Largest risk: Git HEAD alone does not freeze the dirty working-tree content.
