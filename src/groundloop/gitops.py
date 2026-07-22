from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import run_process


def repo_snapshot(repo_config: dict[str, Any]) -> dict[str, Any]:
    path = Path(repo_config["path"])
    result: dict[str, Any] = {
        "name": repo_config["name"],
        "path": str(path),
        "baseline_ref": repo_config.get("baseline_ref", "origin/develop"),
        "available": False,
        "head": None,
        "baseline_head": None,
        "branch": None,
        "dirty": False,
        "ahead": None,
        "behind": None,
        "error": None,
    }
    if not path.exists():
        result["error"] = "repository path is missing"
        return result
    head = run_process(["git", "rev-parse", "HEAD"], cwd=path, timeout=4)
    if head.returncode != 0:
        result["error"] = "not a readable Git repository"
        return result
    result["available"] = True
    result["head"] = head.stdout.strip()
    branch = run_process(["git", "branch", "--show-current"], cwd=path, timeout=4)
    result["branch"] = branch.stdout.strip() or "detached"
    dirty = run_process(["git", "status", "--porcelain", "--untracked-files=no"], cwd=path, timeout=5)
    result["dirty"] = bool(dirty.stdout.strip()) if dirty.returncode == 0 else False
    baseline_ref = result["baseline_ref"]
    baseline = run_process(["git", "rev-parse", "--verify", baseline_ref], cwd=path, timeout=4)
    if baseline.returncode == 0:
        result["baseline_head"] = baseline.stdout.strip()
        counts = run_process(["git", "rev-list", "--left-right", "--count", f"{baseline_ref}...HEAD"], cwd=path, timeout=5)
        if counts.returncode == 0:
            parts = counts.stdout.strip().split()
            if len(parts) == 2:
                result["behind"], result["ahead"] = int(parts[0]), int(parts[1])
    else:
        result["error"] = f"baseline ref unavailable: {baseline_ref}"
    return result


def fetch_baseline(repo_config: dict[str, Any], timeout: int = 30) -> tuple[bool, str]:
    path = Path(repo_config["path"])
    ref = repo_config.get("baseline_ref", "origin/develop")
    if "/" not in ref:
        return True, "local baseline; fetch skipped"
    remote, branch = ref.split("/", 1)
    result = run_process(["git", "fetch", remote, branch, "--prune"], cwd=path, timeout=timeout)
    message = (result.stderr or result.stdout).strip()
    return result.returncode == 0, message[-500:]
