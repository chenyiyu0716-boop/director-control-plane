import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .git import git
from ..config import DeploymentConfig


def _docker(*args: str, timeout: int = 10) -> Optional[str]:
    try:
        result = subprocess.run(
            ["docker", *args], check=True, capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def inspect_compose_runtime(config: DeploymentConfig) -> Dict[str, Any]:
    ids = _docker("ps", "--filter", "label=com.docker.compose.project={}".format(config.compose_project), "-q")
    if ids is None:
        return {"available": False, "error": "docker-unavailable", "containers": []}
    container_ids = [value for value in ids.splitlines() if value]
    containers = []
    for container_id in container_ids:
        raw = _docker("inspect", container_id)
        if raw is None:
            return {"available": False, "error": "docker-inspect-failed", "containers": containers}
        payload = json.loads(raw)[0]
        labels = payload.get("Config", {}).get("Labels") or {}
        containers.append({
            "name": str(payload.get("Name", "")).lstrip("/"),
            "service": labels.get("com.docker.compose.service"),
            "config_files": labels.get("com.docker.compose.project.config_files"),
            "mounts": [
                {"type": mount.get("Type"), "source": mount.get("Source"), "destination": mount.get("Destination")}
                for mount in payload.get("Mounts", [])
            ],
        })
    return {"available": True, "error": None, "containers": containers}


def _common_git_dir(root: Path) -> Optional[Path]:
    value = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(value).resolve() if value else None


def _source_worktree(source: str, expected_common_dir: Path) -> Optional[Path]:
    path = Path(source)
    probe = path if path.is_dir() else path.parent
    top = git(probe, "rev-parse", "--show-toplevel")
    if not top:
        return None
    root = Path(top).resolve()
    return root if _common_git_dir(root) == expected_common_dir else None


def evaluate_deployment(project_root: Path, config: DeploymentConfig, runtime: Dict[str, Any]) -> Dict[str, Any]:
    main_root = project_root.resolve()
    main_head = git(main_root, "rev-parse", "HEAD")
    common_dir = _common_git_dir(main_root)
    evidence: Dict[str, Any] = {
        "compose_project": config.compose_project,
        "main_root": str(main_root),
        "main_head": main_head,
        "services": [],
    }
    if not runtime.get("available") or not main_head or not common_dir:
        evidence["error"] = runtime.get("error") or "main-git-unavailable"
        return {"status": "critical", "reason": "runtime-source-unknown", "evidence": evidence}

    containers = runtime.get("containers") or []
    by_service = {item.get("service"): item for item in containers if item.get("service")}
    missing = [service for service in config.required_services if service not in by_service]
    if missing:
        evidence["missing_services"] = missing

    roots: Dict[str, Path] = {}
    unknown_services = []
    for service in config.required_services:
        container = by_service.get(service)
        if not container:
            continue
        candidates = []
        config_files = container.get("config_files")
        if config_files:
            candidates.extend(value.strip() for value in str(config_files).split(",") if value.strip())
        candidates.extend(
            str(item["source"]) for item in container.get("mounts", [])
            if item.get("type") == "bind" and item.get("source")
        )
        root = next((_source_worktree(value, common_dir) for value in candidates if value), None)
        if root:
            roots[service] = root
        else:
            unknown_services.append(service)

    for service in config.required_services:
        root = roots.get(service)
        if not root:
            evidence["services"].append({"service": service, "source_root": None, "head": None})
            continue
        runtime_head = git(root, "rev-parse", "HEAD")
        merge_base = git(main_root, "merge-base", main_head, runtime_head) if runtime_head else None
        relationship = "unknown"
        if runtime_head == main_head:
            relationship = "equal"
        elif merge_base == main_head:
            relationship = "runtime-ahead"
        elif merge_base == runtime_head:
            relationship = "runtime-behind"
        elif merge_base:
            relationship = "diverged"
        evidence["services"].append({
            "service": service,
            "source_root": str(root),
            "head": runtime_head,
            "merge_base": merge_base,
            "relationship": relationship,
        })

    if missing or unknown_services:
        evidence["unknown_services"] = unknown_services
        return {"status": "critical", "reason": "runtime-source-unknown", "evidence": evidence}
    unique_roots = sorted({str(value) for value in roots.values()})
    if len(unique_roots) != 1:
        evidence["runtime_roots"] = unique_roots
        return {"status": "critical", "reason": "mixed-runtime-sources", "evidence": evidence}
    relationships = {item["relationship"] for item in evidence["services"]}
    if "diverged" in relationships:
        return {"status": "critical", "reason": "runtime-main-diverged", "evidence": evidence}
    if relationships != {"equal"}:
        return {"status": "warning", "reason": "runtime-main-not-equal", "evidence": evidence}
    return {"status": "healthy", "reason": "runtime-main-equal", "evidence": evidence}
