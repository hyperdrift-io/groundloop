from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

from .config import (
    create_project_config,
    discover_repositories,
    find_project,
    groundloop_home,
    project_config_path,
    register_project,
    reports_dir,
    state_path,
)
from .gitops import fetch_baseline, repo_snapshot
from .hooks import handle_hook
from .installer import doctor, install, uninstall
from .sessions import create_digest
from .state import change_lesson, default_state, load_state, mutate_state, observe_lesson, save_state
from .status import build_status, format_status
from .util import iso_now, now_utc, parse_time


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=True))


def _project_or_exit(cwd: Path) -> tuple[Path, dict[str, Any]]:
    found = find_project(cwd)
    if not found:
        raise SystemExit("Groundloop is not initialised for this workspace. Run groundloop init.")
    return found


def command_init(args: argparse.Namespace) -> int:
    root = Path(args.cwd or os.getcwd()).resolve()
    explicit = [Path(value).resolve() for value in args.repo] if args.repo else None
    repos = discover_repositories(root, explicit)
    if not repos:
        print("No bounded Git repositories found. Use --repo PATH to specify one.", file=sys.stderr)
        return 2
    project = create_project_config(root, repos, args.baseline_ref)
    destination = register_project(project, shared=args.shared)
    if not state_path(root).exists():
        save_state(root, default_state(root))
    reports_dir(root).mkdir(parents=True, exist_ok=True)
    result = {"workspace": str(root), "project_config": str(destination), "repositories": project["repositories"], "shared": args.shared}
    _json(result) if args.json else print(f"Initialised Groundloop for {root}\nConfig: {destination}\nRepositories: {len(repos)}")
    return 0


def command_status(args: argparse.Namespace) -> int:
    root, project = _project_or_exit(Path(args.cwd or os.getcwd()))
    result = build_status(root, project)
    _json(result) if args.json else print(format_status(result))
    return 0 if result["status"] != "PARTIAL" else 1


def command_refresh(args: argparse.Namespace) -> int:
    root, project = _project_or_exit(Path(args.cwd or os.getcwd()))
    results = []
    for repo in project.get("repositories", []):
        ok, message = fetch_baseline(repo, timeout=args.timeout)
        results.append({"name": repo["name"], "ok": ok, "message": message})
    all_ok = all(item["ok"] for item in results)
    if all_ok:
        mutate_state(root, lambda state: state.__setitem__("last_remote_refresh", iso_now()))
    _json({"ok": all_ok, "repositories": results}) if args.json else [print(f"{item['name']}: {'ok' if item['ok'] else 'failed'} {item['message']}") for item in results]
    return 0 if all_ok else 1


def command_digest(args: argparse.Namespace) -> int:
    root, _ = _project_or_exit(Path(args.cwd or os.getcwd()))
    path, document = create_digest(root, args.since_hours, args.max_sessions)
    result = {"path": str(path), "reports_dir": str(reports_dir(root)), "counts": document["counts"], "cutoff": document["cutoff"]}
    _json(result) if args.json else print(f"Digest: {path}\nCodex: {document['counts']['codex_sessions']} | Claude: {document['counts']['claude_sessions']}")
    return 0


def command_compare(args: argparse.Namespace) -> int:
    root, _ = _project_or_exit(Path(args.cwd or os.getcwd()))
    path, document = create_digest(root, args.since_hours, args.max_sessions)
    cutoff = now_utc().timestamp() - (args.since_hours * 3600)
    state = load_state(root)
    treatment_ids = {
        item.get("session_id")
        for item in state.get("sessions", [])
        if item.get("variant") == "treatment" and (parse_time(item.get("started_at")) or now_utc()).timestamp() >= cutoff
    }

    def cohort(items: list[dict[str, Any]]) -> dict[str, Any]:
        turns = [len(item.get("user_messages", [])) for item in items]
        failures = [len(item.get("tool_failures", [])) for item in items]
        return {
            "sessions": len(items),
            "median_user_turns": statistics.median(turns) if turns else None,
            "sessions_with_failures": sum(1 for count in failures if count),
            "failure_excerpts": sum(failures),
            "providers": {
                provider: sum(1 for item in items if item.get("provider") == provider)
                for provider in ("codex", "claude")
            },
        }

    treatment = [item for item in document["sessions"] if item.get("session_id") in treatment_ids]
    baseline = [item for item in document["sessions"] if item.get("session_id") not in treatment_ids]
    result = {
        "window_hours": args.since_hours,
        "digest": str(path),
        "baseline": cohort(baseline),
        "treatment": cohort(treatment),
        "interpretation": "Directional evidence only. Baseline means an eligible workspace session without a Groundloop treatment marker; task mix and profile equivalence are not guaranteed.",
    }
    _json(result)
    return 0


def command_reflection_complete(args: argparse.Namespace) -> int:
    root, project = _project_or_exit(Path(args.cwd or os.getcwd()))
    report = Path(args.report).resolve()
    if not report.is_file():
        print(f"Reflection report does not exist: {report}", file=sys.stderr)
        return 2
    cutoffs = {snapshot["name"]: snapshot["head"] for snapshot in (repo_snapshot(repo) for repo in project["repositories"]) if snapshot["head"]}

    def update(state: dict[str, Any]) -> None:
        state["last_reflection"] = {"completed_at": iso_now(), "report": str(report), "repo_cutoffs": cutoffs}

    mutate_state(root, update)
    print(f"Reflection checkpoint recorded for {len(cutoffs)} repositories.")
    return 0


def command_lesson(args: argparse.Namespace) -> int:
    root, _ = _project_or_exit(Path(args.cwd or os.getcwd()))
    if args.lesson_command == "list":
        lessons = load_state(root).get("operational_lessons", [])
        if args.status:
            lessons = [item for item in lessons if item.get("status") == args.status]
        if args.json:
            _json(lessons)
        else:
            for item in lessons:
                print(f"{item['id']} [{item['status']}] x{item['occurrences']} {item['symptom']}")
        return 0
    if args.lesson_command == "observe":
        lesson = observe_lesson(
            root,
            symptom=args.symptom,
            category=args.category,
            resolution=args.resolution,
            severity=args.severity,
            session_id=args.session_id,
            tags=args.tag,
        )
        _json(lesson) if args.json else print(f"{lesson['id']} [{lesson['status']}] x{lesson['occurrences']}")
        return 0
    status = "resolved" if args.lesson_command == "resolve" else "retired"
    found = change_lesson(root, args.id, status, getattr(args, "destination", None))
    print(f"Lesson {args.id} marked {status}." if found else f"Lesson not found: {args.id}")
    return 0 if found else 1


def command_hook(args: argparse.Namespace) -> int:
    raw = sys.stdin.read(1_000_000)
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    output = handle_hook(args.host, args.event, payload)
    if output:
        sys.stdout.write(output)
    return 0


def command_install(args: argparse.Namespace) -> int:
    hosts = [item.strip() for item in args.hosts.split(",") if item.strip()]
    invalid = set(hosts) - {"codex", "claude"}
    if invalid:
        print(f"Unsupported hosts: {', '.join(sorted(invalid))}", file=sys.stderr)
        return 2
    result = install(hosts, args.codex_profile, args.dry_run)
    _json(result)
    return 0 if result["ok"] else 1


def command_update(args: argparse.Namespace) -> int:
    from .config import load_global_config

    config = load_global_config()
    hosts = config.get("hosts") or ["codex", "claude"]
    result = install(hosts, config.get("codex_profile", "work"), False)
    _json(result)
    return 0 if result["ok"] else 1


def command_doctor(args: argparse.Namespace) -> int:
    result = doctor(Path(args.cwd or os.getcwd()))
    if args.json:
        _json(result)
    else:
        for check in result["checks"]:
            print(f"{'PASS' if check['ok'] else 'FAIL'} {check['name']}: {check['detail']}")
    return 0 if result["ok"] else 1


def command_paths(args: argparse.Namespace) -> int:
    root, _ = _project_or_exit(Path(args.cwd or os.getcwd()))
    _json(
        {
            "home": str(groundloop_home()),
            "workspace": str(root),
            "project_config": str(project_config_path(root)),
            "state": str(state_path(root)),
            "reports": str(reports_dir(root)),
        }
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="groundloop", description="Ground AI development in current project reality.")
    sub = result.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialise private workspace configuration")
    init.add_argument("--cwd")
    init.add_argument("--repo", action="append", default=[])
    init.add_argument("--baseline-ref", default="origin/develop")
    init.add_argument("--shared", action="store_true")
    init.add_argument("--json", action="store_true")
    init.set_defaults(func=command_init)

    status = sub.add_parser("status", help="Show current Groundloop status")
    status.add_argument("--cwd")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=command_status)

    refresh = sub.add_parser("refresh", help="Fetch configured baseline refs")
    refresh.add_argument("--cwd")
    refresh.add_argument("--timeout", type=int, default=30)
    refresh.add_argument("--json", action="store_true")
    refresh.set_defaults(func=command_refresh)

    digest = sub.add_parser("digest", help="Create a privacy-filtered recent-thread digest")
    digest.add_argument("--cwd")
    digest.add_argument("--since-hours", type=int, default=36)
    digest.add_argument("--max-sessions", type=int, default=100)
    digest.add_argument("--json", action="store_true")
    digest.set_defaults(func=command_digest)

    compare = sub.add_parser("compare", help="Compare unmarked and Groundloop session cohorts")
    compare.add_argument("--cwd")
    compare.add_argument("--since-hours", type=int, default=168)
    compare.add_argument("--max-sessions", type=int, default=200)
    compare.set_defaults(func=command_compare)

    complete = sub.add_parser("reflection-complete", help="Checkpoint a completed reflection report")
    complete.add_argument("--cwd")
    complete.add_argument("--report", required=True)
    complete.set_defaults(func=command_reflection_complete)

    lesson = sub.add_parser("lesson", help="Manage operational lessons")
    lesson_sub = lesson.add_subparsers(dest="lesson_command", required=True)
    lesson_list = lesson_sub.add_parser("list")
    lesson_list.add_argument("--cwd")
    lesson_list.add_argument("--status", choices=["watch", "active", "resolved", "retired"])
    lesson_list.add_argument("--json", action="store_true")
    lesson_list.set_defaults(func=command_lesson)
    observe = lesson_sub.add_parser("observe")
    observe.add_argument("--cwd")
    observe.add_argument("--symptom", required=True)
    observe.add_argument("--category", default="scripts/tooling")
    observe.add_argument("--resolution")
    observe.add_argument("--severity", choices=["normal", "severe"], default="normal")
    observe.add_argument("--session-id")
    observe.add_argument("--tag", action="append", default=[])
    observe.add_argument("--json", action="store_true")
    observe.set_defaults(func=command_lesson)
    resolve = lesson_sub.add_parser("resolve")
    resolve.add_argument("--cwd")
    resolve.add_argument("--id", required=True)
    resolve.add_argument("--destination")
    resolve.set_defaults(func=command_lesson)
    retire = lesson_sub.add_parser("retire")
    retire.add_argument("--cwd")
    retire.add_argument("--id", required=True)
    retire.set_defaults(func=command_lesson)

    hook = sub.add_parser("hook", help="Run an agent lifecycle hook")
    hook.add_argument("--host", choices=["auto", "codex", "claude"], default="auto")
    hook.add_argument("--event", choices=["session-start", "post-tool", "user-prompt"], required=True)
    hook.set_defaults(func=command_hook)

    install_cmd = sub.add_parser("install", help="Install Groundloop host integration")
    install_cmd.add_argument("--hosts", default="codex,claude")
    install_cmd.add_argument("--codex-profile", default="work")
    install_cmd.add_argument("--dry-run", action="store_true")
    install_cmd.set_defaults(func=command_install)

    update_cmd = sub.add_parser("update", help="Refresh installed Groundloop host integration")
    update_cmd.set_defaults(func=command_update)

    doctor_cmd = sub.add_parser("doctor", help="Diagnose Groundloop installation")
    doctor_cmd.add_argument("--cwd")
    doctor_cmd.add_argument("--json", action="store_true")
    doctor_cmd.set_defaults(func=command_doctor)

    paths = sub.add_parser("paths", help="Show Groundloop-owned paths")
    paths.add_argument("--cwd")
    paths.set_defaults(func=command_paths)

    uninstall_cmd = sub.add_parser("uninstall", help="Remove host integration and optionally project state")
    uninstall_cmd.add_argument("--purge-state", action="store_true")
    uninstall_cmd.set_defaults(func=lambda args: (_json(uninstall(args.purge_state)) or 0))
    return result


def main() -> None:
    args = parser().parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
