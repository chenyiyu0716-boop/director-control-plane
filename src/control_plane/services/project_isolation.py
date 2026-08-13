from typing import FrozenSet


# This Chief runtime does not own the Julius execution lineage. Julius is governed by
# its independent control worktree and task-state root; records may remain readable
# here for audit, but they must never become dispatchable from this runtime.
# JULIUS_PROJECT_ID is the single source of truth for the Julius project id; both the
# Chief-side isolation gate and the Julius-side onboarding runtime import it from here.
JULIUS_PROJECT_ID = "julius"
ISOLATED_PROJECT_IDS: FrozenSet[str] = frozenset({JULIUS_PROJECT_ID})


def project_is_isolated(project_id: str) -> bool:
    return project_id in ISOLATED_PROJECT_IDS
