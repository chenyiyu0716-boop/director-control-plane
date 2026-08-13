from typing import FrozenSet


# This Chief runtime does not own the Julius execution lineage. Julius is governed by
# its independent control worktree and task-state root; records may remain readable
# here for audit, but they must never become dispatchable from this runtime.
ISOLATED_PROJECT_IDS: FrozenSet[str] = frozenset({"julius"})


def project_is_isolated(project_id: str) -> bool:
    return project_id in ISOLATED_PROJECT_IDS
