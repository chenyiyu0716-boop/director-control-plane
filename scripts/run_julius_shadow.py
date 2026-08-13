#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from control_plane.config import ProjectConfig  # noqa: E402
from control_plane.services import (  # noqa: E402
    BaselineConflictError, DecisionPolicyEngine, ExecutorUnauthorizedError,
    JULIUS_CORRECTION_EXECUTOR_ID, JULIUS_EXECUTOR_ID, JULIUS_PROJECT_ID,
    JuliusIdleGuard, JuliusStatePaths, LeaseDispatcher, TaskRegistry,
    agent_ops_records, decision_facts_from_dict, parse_episode_ledger,
    review_shadow_task, run_read_only_shadow, shadow_baseline, task_from_dict,
)
from control_plane.storage import Repository  # noqa: E402


SAFE_FACTS = {
    "architectureChange": False, "productionChange": False,
    "permissionChange": False, "externalCommunication": False,
    "paidAction": False, "destructiveAction": False,
    "releaseAction": False, "scopeExpansion": False,
    "safetyEvidenceComplete": True, "baselineKnown": True,
    "workspaceAuthorized": True, "acceptanceComplete": True,
}


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run isolated Julius read-only shadow verification")
    parser.add_argument("--julius-root", required=True)
    parser.add_argument("--state-root", default=str(ROOT / "var" / "julius"))
    parser.add_argument("--ledger", help="Episode ledger markdown (default: <julius-root>/../10期实验总表.md)")
    parser.add_argument("--status", help="Production status json (default: <julius-root>/workflow/episode-001/production.json)")
    parser.add_argument("--readme", help="Readme markdown (default: <julius-root>/README.md)")
    args = parser.parse_args(argv)
    julius_root = Path(args.julius_root).resolve()
    state = JuliusStatePaths(Path(args.state_root).resolve())
    state.ensure()
    database = state.root / "control-plane.sqlite3"
    if database.exists():
        raise SystemExit("Refusing to overwrite an existing Julius shadow database: {}".format(database))

    # 允许覆盖验证输入路径；默认沿用 Julius 约定布局（其他项目复用此脚本时可覆盖）。
    ledger = Path(args.ledger).resolve() if args.ledger else (julius_root / "../10期实验总表.md").resolve()
    production = Path(args.status).resolve() if args.status else julius_root / "workflow/episode-001/production.json"
    readme = Path(args.readme).resolve() if args.readme else julius_root / "README.md"

    def _scope_of(path):
        # 尽量给出相对 julius_root 的可读路径；不在其内则用绝对路径。
        try:
            return str(Path(path).resolve().relative_to(julius_root))
        except ValueError:
            return str(Path(path).resolve())
    git_head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=julius_root,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    baseline = shadow_baseline(git_head, [ledger, production])
    write_json(state.planner / "baseline.json", baseline)
    write_json(state.planner / "ledger-candidates.json", parse_episode_ledger(ledger))
    write_json(state.review / "status.json", {"project_id": JULIUS_PROJECT_ID, "status": "PENDING"})
    write_json(state.escalation / "status.json", {
        "project_id": JULIUS_PROJECT_ID, "delivery": "DISABLED",
        "reason": "Fresh Owner approval is required before a real Feishu card.",
    })

    repository = Repository(database)
    repository.migrate()
    repository.upsert_project(ProjectConfig(
        id=JULIUS_PROJECT_ID, name="尤里乌斯", kind="business_agent", owner="Project Owner",
        root=julius_root, ledger=ledger, status=production,
        knowledge_roots=[julius_root / "workflow"], enabled_agents=[],
    ))
    registry = TaskRegistry(repository)
    policy = DecisionPolicyEngine(repository)
    # 这是 Julius 的独立 runtime（独立 sqlite + state-root）。轻量方案对 Chief runtime
    # 的隔离不应作用于此：本 dispatcher 显式传空隔离集，允许派发 Julius 自己的 shadow 任务。
    dispatcher = LeaseDispatcher(repository, isolated_project_ids=frozenset())
    dispatcher.register_executor(JULIUS_EXECUTOR_ID, [JULIUS_PROJECT_ID], "low")
    dispatcher.register_executor(JULIUS_CORRECTION_EXECUTOR_ID, [JULIUS_PROJECT_ID], "low")
    dispatcher.set_project_baseline(JULIUS_PROJECT_ID, baseline["baseline_ref"], "julius-planner")

    draft = registry.register(task_from_dict({
        "id": "JUL-SHADOW-001", "projectId": JULIUS_PROJECT_ID,
        "title": "Read-only Julius document fingerprint shadow",
        "objective": "Report allowlisted paths, line counts and SHA-256 without source writes.",
        "scope": [_scope_of(readme), _scope_of(ledger), _scope_of(production)],
        "acceptance": ["Exact paths", "Line counts", "SHA-256", "No source writes"],
        "priority": "P0", "riskLevel": "low", "allowedExecutors": [JULIUS_EXECUTOR_ID],
        "workspaceRoots": [str(julius_root), str(state.root)], "dependencies": [],
        "sourceUri": "julius://onboarding/shadow-001",
    }), "julius-planner", request_id="julius:register:JUL-SHADOW-001")
    ready = policy.decide(
        draft["id"], decision_facts_from_dict(SAFE_FACTS), draft["version"], "julius-planner",
        request_id="julius:decide:JUL-SHADOW-001",
    )["task"]

    rejected = {}
    try:
        dispatcher.next(JULIUS_EXECUTOR_ID, "control-panel")
    except ExecutorUnauthorizedError as error:
        rejected["wrong_project_id"] = str(error)
    try:
        dispatcher.claim(
            ready["id"], JULIUS_EXECUTOR_ID, "shadow:git:old0000", ready["version"],
            "julius:probe:old-baseline",
        )
    except BaselineConflictError as error:
        rejected["old_baseline"] = str(error)
    try:
        dispatcher.claim(
            ready["id"], JULIUS_CORRECTION_EXECUTOR_ID, baseline["baseline_ref"], ready["version"],
            "julius:probe:unlisted-executor",
        )
    except ExecutorUnauthorizedError as error:
        rejected["non_allowlisted_executor"] = str(error)
    write_json(state.evidence / "rejection-probes.json", rejected)

    lease = dispatcher.claim(
        ready["id"], JULIUS_EXECUTOR_ID, baseline["baseline_ref"], ready["version"],
        "julius:claim:JUL-SHADOW-001",
    )
    dispatcher.heartbeat(
        lease["lease_id"], JULIUS_EXECUTOR_ID, "julius:heartbeat:JUL-SHADOW-001",
    )
    evidence_path = state.evidence / "JUL-SHADOW-001.json"
    evidence = run_read_only_shadow([readme, ledger, production], evidence_path)
    dispatcher.complete(
        lease["lease_id"], JULIUS_EXECUTOR_ID, baseline["baseline_ref"],
        "julius:complete:JUL-SHADOW-001",
    )
    review = review_shadow_task(repository, ready["id"], evidence_path, [readme, ledger, production])
    write_json(state.review / "JUL-SHADOW-001.json", review)
    agent_ops_records(ready["id"], evidence, state.root / "agent-ops")

    idle = JuliusIdleGuard(state.planner / "idle.json")
    idle_results = [idle.observe(repository, dispatcher) for _ in range(3)]
    write_json(state.planner / "idle-test.json", idle_results)
    summary = {
        "project_id": JULIUS_PROJECT_ID,
        "database": str(database),
        "baseline": baseline,
        "task": repository.get_task(ready["id"]),
        "transitions": repository.list_task_transitions(ready["id"]),
        "lease": repository.list_rows("task_lease"),
        "executors": repository.list_rows("executor_profile"),
        "rejections": rejected,
        "idle_test": idle_results,
        "feishu_delivery": "DISABLED",
    }
    write_json(state.root / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
