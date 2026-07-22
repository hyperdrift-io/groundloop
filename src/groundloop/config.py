from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from .util import atomic_write_json, is_within, read_json, run_process


SCHEMA_VERSION = 1


def groundloop_home() -> Path:
    override = os.environ.get("GROUNDLOOP_HOME")
    return Path(override).expanduser().resolve() if override else (Path.home() / ".groundloop").resolve()


def global_config_path() -> Path:
    return groundloop_home() / "config.json"


def load_global_config() -> dict[str, Any]:
    return read_json(global_config_path(), {"schema_version": SCHEMA_VERSION, "codex_profile": "work"})


def save_global_config(config: dict[str, Any]) -> None:
    config["schema_version"] = SCHEMA_VERSION
    atomic_write_json(global_config_path(), config)


def workspace_id(root: Path) -> str:
    normalized = os.path.normcase(str(root.resolve())).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()[:16]


def project_dir(root: Path) -> Path:
    return groundloop_home() / "projects" / workspace_id(root)


def project_config_path(root: Path) -> Path:
    return project_dir(root) / "project.json"


def state_path(root: Path) -> Path:
    return project_dir(root) / "state.json"


def reports_dir(root: Path) -> Path:
    return project_dir(root) / "reports"


def _git_root(path: Path) -> Path | None:
    result = run_process(["git", "rev-parse", "--show-toplevel"], cwd=path, timeout=3)
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return None


def _baseline_ref(repo: Path, preferred: str) -> str:
    candidates = [preferred, "origin/develop", "develop", "origin/main", "main", "origin/master", "master"]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        result = run_process(["git", "rev-parse", "--verify", candidate], cwd=repo, timeout=3)
        if result.returncode == 0:
            return candidate
    return preferred


def discover_repositories(root: Path, explicit: list[Path] | None = None) -> list[Path]:
    if explicit:
        candidates = [path.resolve() for path in explicit]
    else:
        own_root = _git_root(root)
        if own_root and own_root == root.resolve():
            candidates = [own_root]
        else:
            candidates = []
            for child in sorted(root.iterdir(), key=lambda value: value.name.lower()):
                if not child.is_dir() or child.name.startswith(".") or child.name in {"artifacts", "env", "logs", "run"}:
                    continue
                if (child / ".git").exists():
                    candidates.append(child.resolve())
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.exists() or not (candidate / ".git").exists():
            continue
        key = os.path.normcase(str(candidate))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def create_project_config(root: Path, repos: list[Path], baseline_ref: str = "origin/develop") -> dict[str, Any]:
    resolved = root.resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "workspace": str(resolved),
        "name": resolved.name,
        "freshness": {"reflection_hours": 36, "remote_hours": 24},
        "repositories": [
            {
                "name": repo.name,
                "path": str(repo),
                "baseline_ref": _baseline_ref(repo, baseline_ref),
            }
            for repo in repos
        ],
        "guidance": [
            str(path)
            for path in (resolved / "AGENTS.md", resolved / "CLAUDE.md", resolved / ".claude" / "CLAUDE.md")
            if path.exists()
        ],
    }


def register_project(config: dict[str, Any], shared: bool = False) -> Path:
    root = Path(config["workspace"]).resolve()
    destination = project_config_path(root)
    atomic_write_json(destination, config)
    index_path = groundloop_home() / "projects.json"
    index = read_json(index_path, {"schema_version": SCHEMA_VERSION, "projects": {}})
    index.setdefault("projects", {})[str(root)] = workspace_id(root)
    atomic_write_json(index_path, index)
    if shared:
        shared_path = root / ".groundloop" / "project.json"
        shared_config = dict(config)
        shared_config["workspace"] = "."
        shared_config["repositories"] = [
            {
                **repo,
                "path": os.path.relpath(repo["path"], root).replace("\\", "/"),
            }
            for repo in config.get("repositories", [])
        ]
        shared_config["guidance"] = [os.path.relpath(path, root).replace("\\", "/") for path in config.get("guidance", [])]
        atomic_write_json(shared_path, shared_config)
    return destination


def find_project(cwd: Path) -> tuple[Path, dict[str, Any]] | None:
    resolved = cwd.resolve()
    for parent in (resolved, *resolved.parents):
        shared = parent / ".groundloop" / "project.json"
        if shared.exists():
            config = read_json(shared, {})
            if config.get("workspace"):
                root = (parent / config["workspace"]).resolve() if not Path(config["workspace"]).is_absolute() else Path(config["workspace"]).resolve()
                expanded = dict(config)
                expanded["workspace"] = str(root)
                expanded["repositories"] = [
                    {
                        **repo,
                        "path": str((root / repo["path"]).resolve()) if not Path(repo["path"]).is_absolute() else repo["path"],
                    }
                    for repo in config.get("repositories", [])
                ]
                expanded["guidance"] = [str((root / path).resolve()) if not Path(path).is_absolute() else path for path in config.get("guidance", [])]
                return root, expanded

    index = read_json(groundloop_home() / "projects.json", {"projects": {}})
    matches: list[tuple[Path, str]] = []
    for root_text, identifier in index.get("projects", {}).items():
        root = Path(root_text)
        if root.exists() and is_within(resolved, root):
            matches.append((root.resolve(), identifier))
    if not matches:
        return None
    root, identifier = max(matches, key=lambda item: len(str(item[0])))
    config = read_json(groundloop_home() / "projects" / identifier / "project.json", {})
    return (root, config) if config else None
