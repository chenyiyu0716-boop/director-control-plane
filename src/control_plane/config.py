import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .domain.models import AgentType


@dataclass(frozen=True)
class DeploymentConfig:
    compose_project: str
    required_services: List[str]


@dataclass(frozen=True)
class ProjectConfig:
    id: str
    name: str
    kind: str
    owner: str
    root: Path
    ledger: Path
    status: Path
    knowledge_roots: List[Path]
    enabled_agents: List[AgentType]
    deployment: Optional[DeploymentConfig] = None


@dataclass(frozen=True)
class Settings:
    database: Path
    projects: List[ProjectConfig]


def load_settings(config_path: str = None) -> Settings:
    path = Path(config_path or os.environ.get("CONTROL_PLANE_CONFIG", "config/projects.local.json")).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent.parent
    database_value = os.environ.get("CONTROL_PLANE_DB", payload.get("database", "var/control-plane.sqlite3"))
    database = Path(database_value).expanduser()
    if not database.is_absolute():
        database = (base / database).resolve()
    projects = []
    for item in payload.get("projects", []):
        root = Path(item["root"]).expanduser().resolve()
        resolve_project_path = lambda value: (Path(value).expanduser() if Path(value).expanduser().is_absolute() else root / value).resolve()
        deployment_payload = item.get("deployment")
        deployment = None
        if deployment_payload:
            deployment = DeploymentConfig(
                compose_project=str(deployment_payload["composeProject"]),
                required_services=[str(value) for value in deployment_payload.get("requiredServices", [])],
            )
        projects.append(ProjectConfig(
            id=item["id"],
            name=item["name"],
            kind=item.get("kind", "business_agent"),
            owner=item["owner"],
            root=root,
            ledger=resolve_project_path(item["ledger"]),
            status=resolve_project_path(item["status"]),
            knowledge_roots=[resolve_project_path(value) for value in item.get("knowledge_roots", [])],
            enabled_agents=[AgentType(value) for value in item.get("enabled_agents", [agent.value for agent in AgentType])],
            deployment=deployment,
        ))
    return Settings(database=database, projects=projects)
