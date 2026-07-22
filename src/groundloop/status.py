from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from .gitops import repo_snapshot
from .state import load_state
from .util import now_utc, parse_time


def build_status(root: Path, project: dict[str, Any]) -> dict[str, Any]:
    state = load_state(root)
    snapshots = [repo_snapshot(repo) for repo in project.get("repositories", [])]
    available = sum(1 for repo in snapshots if repo["available"])
    partial = any(not repo["available"] or repo.get("error") for repo in snapshots)
    dirty = sum(1 for repo in snapshots if repo.get("dirty"))
    cutoffs = (state.get("last_reflection") or {}).get("repo_cutoffs", {})
    changed = sum(1 for repo in snapshots if repo.get("head") and cutoffs.get(repo["name"]) != repo["head"])

    freshness = project.get("freshness", {})
    reflection_hours = int(freshness.get("reflection_hours", 36))
    remote_hours = int(freshness.get("remote_hours", 24))
    reflection_at = parse_time((state.get("last_reflection") or {}).get("completed_at"))
    remote_at = parse_time(state.get("last_remote_refresh"))
    reflection_current = bool(reflection_at and now_utc() - reflection_at <= timedelta(hours=reflection_hours))
    remote_current = bool(remote_at and now_utc() - remote_at <= timedelta(hours=remote_hours))

    active_lessons = [item for item in state.get("operational_lessons", []) if item.get("status") == "active"]
    watched_lessons = [item for item in state.get("operational_lessons", []) if item.get("status") == "watch"]
    proposals = [item for item in state.get("proposals", []) if item.get("status") in {"proposed", "verify"}]

    if partial:
        status = "PARTIAL"
    elif not reflection_current or not remote_current:
        status = "STALE"
    elif changed or dirty:
        status = "CHANGES"
    else:
        status = "CURRENT"

    return {
        "status": status,
        "workspace": project.get("name", root.name),
        "workspace_path": str(root),
        "repositories": snapshots,
        "repo_count": len(snapshots),
        "available_count": available,
        "changed_count": changed,
        "dirty_count": dirty,
        "reflection": {"current": reflection_current, "completed_at": reflection_at.isoformat() if reflection_at else None},
        "remote": {"current": remote_current, "refreshed_at": remote_at.isoformat() if remote_at else None},
        "lessons": {"active": len(active_lessons), "watch": len(watched_lessons)},
        "proposals_due": len(proposals),
        "scout": state.get("scout", {}),
    }


def _short_time(value: str | None) -> str:
    parsed = parse_time(value)
    return parsed.astimezone().strftime("%d %b %H:%M") if parsed else "never"


def format_status(status: dict[str, Any]) -> str:
    dirty = f" | {status['dirty_count']} dirty" if status["dirty_count"] else ""
    changed = f"{status['changed_count']} changed"
    reflection = "current" if status["reflection"]["current"] else "stale"
    remote = "current" if status["remote"]["current"] else "stale"
    scout_due = status.get("scout", {}).get("next_due_at") or "not scheduled"
    lines = [
        f"GROUNDLOOP - {status['status']}",
        f"Workspace: {status['workspace']} | {status['available_count']}/{status['repo_count']} repos known",
        f"Drift: {changed}{dirty}",
        f"Freshness: refs {remote} ({_short_time(status['remote']['refreshed_at'])}) | reflection {reflection} ({_short_time(status['reflection']['completed_at'])})",
        f"Operational lessons: {status['lessons']['active']} active | {status['lessons']['watch']} watching",
        f"Improvement: {status['proposals_due']} awaiting verification",
        f"Scout: next due {scout_due}",
    ]
    return "\n".join(lines)
