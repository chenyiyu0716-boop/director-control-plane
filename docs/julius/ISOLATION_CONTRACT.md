# Julius Isolation Contract

1. Julius uses a separate SQLite database, baseline row, executor profiles and task namespace.
2. Planner, review, escalation, evidence and idle state live below `var/julius/`.
3. Request IDs use the `julius:` domain; Feishu event/task IDs cannot be reused from Panel.
4. Only `workbuddy-hy3-julius` may claim normal Julius tasks; only the correction executor may claim corrections.
5. Completion stops at REVIEW. Only `julius-reviewer` may accept deterministic evidence into DONE.
6. Ledger parsing creates candidates only. It never mass-registers tasks or marks them READY.
7. After two empty polls the guard emits an Owner Gate; a third silent `null` is forbidden.
8. The dirty Julius repository is read-only evidence. No cleanup, commit or write is permitted.
9. Real Feishu delivery, production, publish, payment, deletion, merge and push require new Owner approval.
