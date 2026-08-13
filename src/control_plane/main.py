import argparse
import json
import os
from pathlib import Path
from typing import Iterable

from .api.server import serve
from .config import ProjectConfig, load_settings
from .domain.models import AgentType, DecisionOutcome, TaskState
from .services import (
    DecisionPolicyEngine, LeaseDispatcher, Orchestrator, ReviewGate, TaskRegistry,
    completion_evidence_from_dict, decision_facts_from_dict, task_from_dict,
)
from .storage import Repository


def selected_projects(projects: Iterable[ProjectConfig], project_id: str):
    selected = [project for project in projects if not project_id or project.id == project_id]
    if project_id and not selected:
        raise SystemExit("Unknown project: {}".format(project_id))
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Director Control Plane v0.1")
    parser.add_argument("--config", help="Path to non-secret project configuration")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init-db")
    run = subcommands.add_parser("run")
    run.add_argument("agent", choices=[value.value for value in AgentType])
    run.add_argument("--project")
    run.add_argument("--trigger", default="manual", choices=["manual", "schedule", "webhook"])
    run_all = subcommands.add_parser("run-all")
    run_all.add_argument("--project")
    run_all.add_argument("--trigger", default="schedule", choices=["manual", "schedule", "webhook"])
    list_command = subcommands.add_parser("list")
    list_command.add_argument("resource", choices=["project", "agent_run", "finding", "review_item", "check_result", "release_report"])
    list_command.add_argument("--limit", type=int, default=50)
    serve_command = subcommands.add_parser("serve")
    serve_command.add_argument("--host", default=os.environ.get("CONTROL_PLANE_HOST", "127.0.0.1"))
    serve_command.add_argument("--port", type=int, default=int(os.environ.get("CONTROL_PLANE_PORT", "8765")))
    task_command = subcommands.add_parser("task")
    task_commands = task_command.add_subparsers(dest="task_command", required=True)
    task_register = task_commands.add_parser("register")
    task_register.add_argument("--file", required=True)
    task_register.add_argument("--actor", required=True)
    task_register.add_argument("--request-id")
    task_transition = task_commands.add_parser("transition")
    task_transition.add_argument("task_id")
    task_transition.add_argument("--to", required=True, choices=[value.value for value in TaskState])
    task_transition.add_argument("--expected-version", required=True, type=int)
    task_transition.add_argument("--actor", required=True)
    task_transition.add_argument("--reason", required=True)
    task_transition.add_argument("--request-id")
    task_list = task_commands.add_parser("list")
    task_list.add_argument("--project")
    task_list.add_argument("--state", choices=[value.value for value in TaskState])
    task_list.add_argument("--limit", type=int, default=100)
    task_history = task_commands.add_parser("history")
    task_history.add_argument("task_id")
    task_decide = task_commands.add_parser("decide")
    task_decide.add_argument("task_id")
    task_decide.add_argument("--facts", required=True)
    task_decide.add_argument("--expected-version", required=True, type=int)
    task_decide.add_argument("--actor", required=True)
    task_decide.add_argument("--request-id")
    task_decisions = task_commands.add_parser("decisions")
    task_decisions.add_argument("task_id")
    task_decisions.add_argument("--outcome", choices=[value.value for value in DecisionOutcome])
    task_decisions.add_argument("--limit", type=int, default=100)
    task_review = task_commands.add_parser("review")
    task_review.add_argument("task_id")
    task_review.add_argument("--evidence", required=True)
    task_review.add_argument("--actor", required=True)
    task_review.add_argument("--request-id")
    task_review.add_argument("--dry-run", action="store_true")
    task_reviews = task_commands.add_parser("reviews")
    task_reviews.add_argument("task_id")
    task_reviews.add_argument("--outcome", choices=["DONE", "NEEDS_FIX", "OWNER_CONFIRMATION_REQUIRED"])
    task_reviews.add_argument("--limit", type=int, default=100)
    task_render = task_commands.add_parser("render")
    task_render.add_argument("--project")
    task_render.add_argument("--output", required=True)
    dispatch = subcommands.add_parser("dispatch")
    dispatch_commands = dispatch.add_subparsers(dest="dispatch_command", required=True)
    executor = dispatch_commands.add_parser("register-executor")
    executor.add_argument("executor_id")
    executor.add_argument("--projects", required=True)
    executor.add_argument("--max-risk", required=True, choices=["low", "medium", "high", "critical"])
    baseline = dispatch_commands.add_parser("set-baseline")
    baseline.add_argument("project_id")
    baseline.add_argument("baseline_ref")
    baseline.add_argument("--actor", required=True)
    get_baseline = dispatch_commands.add_parser("get-baseline")
    get_baseline.add_argument("project_id")
    next_task = dispatch_commands.add_parser("next")
    next_task.add_argument("executor_id")
    next_task.add_argument("--project")
    claim = dispatch_commands.add_parser("claim")
    claim.add_argument("task_id")
    claim.add_argument("executor_id")
    claim.add_argument("baseline_ref")
    claim.add_argument("--expected-version", required=True, type=int)
    claim.add_argument("--request-id", required=True)
    claim.add_argument("--ttl", type=int)
    heartbeat = dispatch_commands.add_parser("heartbeat")
    heartbeat.add_argument("lease_id")
    heartbeat.add_argument("executor_id")
    heartbeat.add_argument("--request-id", required=True)
    heartbeat.add_argument("--ttl", type=int)
    for command_name in ("complete", "fail"):
        finish = dispatch_commands.add_parser(command_name)
        finish.add_argument("lease_id")
        finish.add_argument("executor_id")
        finish.add_argument("baseline_ref")
        finish.add_argument("--request-id", required=True)
        if command_name == "fail":
            finish.add_argument("--reason", required=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    repository = Repository(settings.database)
    repository.migrate()
    for project in settings.projects:
        repository.upsert_project(project)
    if args.command == "init-db":
        print(settings.database)
        return 0
    if args.command == "list":
        print(json.dumps(repository.list_rows(args.resource, args.limit), ensure_ascii=False, indent=2))
        return 0
    if args.command == "serve":
        print("Serving on http://{}:{}".format(args.host, args.port))
        serve(repository, (args.host, args.port))
        return 0
    if args.command == "task":
        registry = TaskRegistry(repository)
        if args.task_command == "register":
            payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
            print(json.dumps(registry.register(task_from_dict(payload), args.actor, args.request_id), ensure_ascii=False, indent=2))
            return 0
        if args.task_command == "transition":
            result = registry.transition(
                args.task_id, TaskState(args.to), args.expected_version, args.actor, args.reason, args.request_id
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.task_command == "list":
            print(json.dumps(repository.list_tasks(args.project, args.state, args.limit), ensure_ascii=False, indent=2))
            return 0
        if args.task_command == "history":
            print(json.dumps(repository.list_task_transitions(args.task_id), ensure_ascii=False, indent=2))
            return 0
        if args.task_command == "decide":
            payload = json.loads(Path(args.facts).read_text(encoding="utf-8"))
            result = DecisionPolicyEngine(repository).decide(
                args.task_id, decision_facts_from_dict(payload), args.expected_version, args.actor, args.request_id
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.task_command == "decisions":
            print(json.dumps(
                repository.list_task_decisions(args.task_id, args.outcome, args.limit),
                ensure_ascii=False, indent=2,
            ))
            return 0
        if args.task_command == "review":
            payload = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
            evidence = completion_evidence_from_dict(payload)
            if evidence["taskId"] != args.task_id:
                raise ValueError("evidence taskId does not match the requested task")
            gate = ReviewGate(repository)
            if args.dry_run:
                result = gate.evaluate(evidence)
            else:
                result = gate.review(evidence, args.actor, args.request_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.task_command == "reviews":
            print(json.dumps(
                repository.list_task_reviews(args.task_id, args.outcome, args.limit),
                ensure_ascii=False, indent=2,
            ))
            return 0
        registry.render_to_file(Path(args.output), args.project)
        print(args.output)
        return 0
    if args.command == "dispatch":
        dispatcher = LeaseDispatcher(repository)
        if args.dispatch_command == "register-executor":
            result = dispatcher.register_executor(args.executor_id, args.projects.split(","), args.max_risk)
        elif args.dispatch_command == "set-baseline":
            result = dispatcher.set_project_baseline(args.project_id, args.baseline_ref, args.actor)
        elif args.dispatch_command == "get-baseline":
            result = dispatcher.get_project_baseline(args.project_id)
        elif args.dispatch_command == "next":
            result = dispatcher.next(args.executor_id, args.project)
        elif args.dispatch_command == "claim":
            result = dispatcher.claim(args.task_id, args.executor_id, args.baseline_ref,
                                      args.expected_version, args.request_id, args.ttl)
        elif args.dispatch_command == "heartbeat":
            result = dispatcher.heartbeat(args.lease_id, args.executor_id, args.request_id, args.ttl)
        elif args.dispatch_command == "complete":
            result = dispatcher.complete(args.lease_id, args.executor_id, args.baseline_ref, args.request_id)
        else:
            result = dispatcher.fail(args.lease_id, args.executor_id, args.baseline_ref,
                                     args.request_id, args.reason)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    orchestrator = Orchestrator(repository)
    projects = selected_projects(settings.projects, args.project)
    if args.command == "run":
        agent_type = AgentType(args.agent)
        for project in projects:
            if agent_type in project.enabled_agents:
                print(orchestrator.run(project, agent_type, args.trigger))
        return 0
    for project in projects:
        for agent_type in project.enabled_agents:
            print(orchestrator.run(project, agent_type, args.trigger))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
