"""Find Claude Code session transcripts on disk."""

from __future__ import annotations

import os
from pathlib import Path


def projects_root(override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config).expanduser() if config else Path.home() / ".claude"
    return base / "projects"


def session_files(root: Path, project: str | None = None) -> list[Path]:
    """Top-level session transcripts, newest first. Subagent files load with their parent."""
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    files = [p for p in root.glob("*/*.jsonl") if p.is_file()]
    if not files:  # pointed straight at one project directory
        files = [p for p in root.glob("*.jsonl") if p.is_file()]
    if project:
        needle = project.lower()
        files = [p for p in files if needle in p.parent.name.lower()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def resolve(target: str, root: Path) -> Path:
    """A path, ``last``, or a (prefix of a) session id."""
    candidate = Path(target).expanduser()
    if candidate.is_file():
        return candidate
    files = session_files(root)
    if target in ("last", "latest", "-"):
        if not files:
            raise LookupError(f"no sessions under {root}")
        return files[0]
    matches = [p for p in files if p.stem.startswith(target)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise LookupError(f"no session matching {target!r} under {root}")
    raise LookupError(f"{target!r} is ambiguous: {len(matches)} sessions match")
