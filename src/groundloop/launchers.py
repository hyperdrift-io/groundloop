from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from .config import has_unrestricted_alias_consent, load_global_config
from .util import command_path, record_runtime_failure


CODEX_BYPASS_OPTIONS = (
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
)
CLAUDE_BYPASS_OPTION = "--dangerously-skip-permissions"


def _require_unrestricted_alias_consent(config: dict[str, object]) -> None:
    if not has_unrestricted_alias_consent(config):
        raise SystemExit(
            "Groundloop unrestricted aliases are not enabled. Run groundloop install and explicitly confirm the bypass modes."
        )


def _print_startup_summary() -> None:
    try:
        from .config import find_project
        from .status import build_status, format_status

        found = find_project(Path.cwd())
        if found:
            root, project = found
            print(format_status(build_status(root, project)), file=sys.stderr)
        else:
            print("GROUNDLOOP - PARTIAL\nWorkspace is not initialised; run groundloop init.", file=sys.stderr)
    except Exception as error:  # A launcher summary must never prevent the agent from starting.
        print(f"GROUNDLOOP - PARTIAL\nStartup status failed: {type(error).__name__}", file=sys.stderr)


def _run(command: list[str], env: dict[str, str]) -> int:
    try:
        return subprocess.call(command, env=env)
    except FileNotFoundError:
        print(f"Groundloop could not find {command[0]} on PATH.", file=sys.stderr)
        return 127


def _has_option(args: list[str], names: set[str]) -> bool:
    return any(arg in names or any(arg.startswith(f"{name}=") for name in names if name.startswith("--")) for arg in args)


def codex_command(args: list[str], profile: str) -> list[str]:
    command = ["codex"]
    command.extend(option for option in CODEX_BYPASS_OPTIONS if option not in args)
    if not _has_option(args, {"-p", "--profile"}):
        command.extend(["--profile", profile])
    command.extend(args)
    return command


def claude_command(args: list[str], plugin_dir: str | None) -> list[str]:
    command = ["claude"]
    if CLAUDE_BYPASS_OPTION not in args:
        command.append(CLAUDE_BYPASS_OPTION)
    if plugin_dir and not _has_option(args, {"--plugin-dir"}):
        command.extend(["--plugin-dir", plugin_dir])
    command.extend(args)
    return command


def treatment_environment(launch_id: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["GROUNDLOOP_ENABLED"] = "1"
    env["GROUNDLOOP_VARIANT"] = "treatment"
    env["GROUNDLOOP_LAUNCH_ID"] = launch_id or uuid.uuid4().hex
    return env


def _run_groundloop_session(command: list[str], host: str) -> int:
    launch_id = uuid.uuid4().hex
    try:
        return _run(command, treatment_environment(launch_id))
    finally:
        try:
            from .config import find_project
            from .sessions import finalize_session

            found = find_project(Path.cwd())
            if found:
                finalize_session(found[0], host, launch_id)
        except Exception as error:
            # The agent has already exited; learning must not alter its exit status.
            record_runtime_failure("session-finalize", type(error).__name__)


def codex_main() -> None:
    config = load_global_config()
    _require_unrestricted_alias_consent(config)
    if not command_path("codex"):
        raise SystemExit("Codex is not installed or is not on PATH.")
    _print_startup_summary()
    raise SystemExit(_run_groundloop_session(codex_command(sys.argv[1:], config.get("codex_profile", "work")), "codex"))


def claude_main() -> None:
    config = load_global_config()
    _require_unrestricted_alias_consent(config)
    if not command_path("claude"):
        raise SystemExit("Claude Code is not installed or is not on PATH.")
    plugin_dir = config.get("plugin_dir")
    if plugin_dir and not Path(plugin_dir).exists():
        raise SystemExit("Groundloop plugin cache is missing; run groundloop install.")
    _print_startup_summary()
    raise SystemExit(_run_groundloop_session(claude_command(sys.argv[1:], plugin_dir), "claude"))


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in {"codex", "claude"}:
        raise SystemExit("Usage: python -m groundloop.launchers codex|claude [arguments]")
    host = sys.argv.pop(1)
    codex_main() if host == "codex" else claude_main()


if __name__ == "__main__":
    main()
