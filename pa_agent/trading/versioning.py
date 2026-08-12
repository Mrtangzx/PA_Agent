"""Reproducibility snapshots for every analysis."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Iterable

STRATEGY_VERSION = "pa-baseline-1.0.0"
FEATURE_VERSION = "market-features-1.0.0"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prompt_snapshot(paths: Iterable[str | Path]) -> list[dict[str, str]]:
    snapshot: list[dict[str, str]] = []
    for value in paths:
        path = Path(value)
        if not path.exists() or not path.is_file():
            snapshot.append({"path": str(value), "sha256": "missing"})
            continue
        snapshot.append({"path": str(value), "sha256": sha256_file(path)})
    return snapshot


def git_revision(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
