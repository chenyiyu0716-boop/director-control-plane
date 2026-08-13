# Julius Control Plane Onboarding

Julius is onboarded as a separate control domain, not as a Panel task source.

- Project: `julius`
- New task IDs: `JUL-*`; historical IDs remain immutable references.
- Executor: `workbuddy-hy3-julius`
- Correction executor: `workbuddy-hy3-julius-correction`
- Reviewer: `julius-reviewer`
- Runtime database and state: `var/julius/`
- Source repository: read-only while its working tree is dirty.
- Baseline: `shadow:git:<HEAD>` plus SHA-256 evidence for explicitly allowlisted files.
- Feishu: protocol code may be reused, but events, IDs, files and runtime state are Julius-only. No real card may be sent without fresh Owner approval.

The onboarding round may execute only deterministic read-only shadow tasks. It must not render, publish, push,
merge, delete, pay, clean the source repository, or move existing business tasks.
