"""Git-aware code provenance for reproducible local artifacts."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CodeProvenance:
    git_commit: str
    dirty: bool
    source_tree_hash: str


def capture_code_provenance(repository: Path | None = None) -> CodeProvenance:
    root = repository or Path.cwd()
    commit = _git(root, "rev-parse", "HEAD").strip()
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    paths_raw = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    digest = hashlib.sha256()
    for encoded_path in sorted(item for item in paths_raw.split(b"\0") if item):
        relative = encoded_path.decode("utf-8", errors="surrogateescape")
        path = root / relative
        digest.update(encoded_path)
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return CodeProvenance(commit, bool(status.strip()), digest.hexdigest())


def _git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("dataset provenance requires an accessible Git repository") from exc
    return result.stdout
