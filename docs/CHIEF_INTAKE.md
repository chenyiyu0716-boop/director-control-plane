# Chief Content Intake

Chief is the single editing entry point for Julius knowledge requests. The Owner submits people, source references, files, judgment and desired stage to Chief; individual requests do not create WorkBuddy windows.

`task intake` accepts `knowledge_ingest` and `candidate_research`. It stores source references and short structured assertions, never source bodies. Facts and quotes must point to a registered source; analysis remains visibly separate. Subject plus normalized source identities form the deduplication key.

Every accepted intake atomically creates exactly one Julius task in `DRAFT`. Even when `targetStage` is `story_ready` or `brief`, the task scope stops at staged extraction and provenance. Planner review is required before READY, and Codex Review is required before Story Ready, Brief, Outline or Draft.

WorkBuddy reads only READY tasks, writes only task-scoped staging/Agent Ops evidence, submits an Executor Report, and stops at REVIEW. The Julius project tree is not modified by intake creation.
