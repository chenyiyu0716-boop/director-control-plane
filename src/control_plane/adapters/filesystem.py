import hashlib
from pathlib import Path
from typing import Iterator, Tuple


SUPPORTED_TEXT = {".md", ".txt"}
REGISTER_ONLY = {".pdf"}


def iter_knowledge_files(project_root: Path, roots, max_files: int = 5000) -> Iterator[Path]:
    emitted = 0
    for configured in roots:
        candidate_root = configured.resolve()
        if not candidate_root.is_dir():
            continue
        for path in sorted(candidate_root.rglob("*")):
            if emitted >= max_files:
                return
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in SUPPORTED_TEXT | REGISTER_ONLY:
                continue
            resolved = path.resolve()
            if not _within(resolved, candidate_root):
                continue
            emitted += 1
            yield resolved


def fingerprint(path: Path, max_bytes: int = 2_000_000) -> Tuple[str, bytes]:
    size = path.stat().st_size
    if size > max_bytes:
        payload = "{}:{}:{}".format(path.name, size, path.stat().st_mtime_ns).encode("utf-8")
        return hashlib.sha256(payload).hexdigest(), b""
    payload = path.read_bytes()
    return hashlib.sha256(payload).hexdigest(), payload


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
