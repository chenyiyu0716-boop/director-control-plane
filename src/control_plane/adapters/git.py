import subprocess
from pathlib import Path
from typing import List, Optional


def git(root: Path, *args: str, timeout: int = 10) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def recent_commits(root: Path, days: int = 7, limit: int = 100) -> List[str]:
    value = git(root, "log", "--format=%h %s", "--since={} days ago".format(days), "-n", str(limit))
    return value.splitlines() if value else []
