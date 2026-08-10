import argparse
import json
import os
from typing import Iterable

from .api.server import serve
from .config import ProjectConfig, load_settings
from .domain.models import AgentType
from .services import Orchestrator
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
