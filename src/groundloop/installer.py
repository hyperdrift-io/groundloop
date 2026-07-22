from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import groundloop_home, load_global_config, save_global_config
from .util import atomic_write_json, command_path, is_within, read_json, run_process


PLUGIN_PARTS = (".codex-plugin", ".claude-plugin", "hooks", "skills")
MARKETPLACE_NAME = "groundloop-local"
SHIM_MARKER = "Groundloop managed launcher"


def source_plugin_root() -> Path:
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / ".codex-plugin" / "plugin.json").exists():
        return source_root
    installed = Path(sys.prefix) / "share" / "groundloop" / "plugin"
    if (installed / ".codex-plugin" / "plugin.json").exists():
        return installed
    raise FileNotFoundError("Groundloop plugin resources are unavailable")


def marketplace_root() -> Path:
    return groundloop_home() / "marketplace"


def installed_plugin_dir() -> Path:
    return marketplace_root() / "plugins" / "groundloop"


def _sync_plugin(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in PLUGIN_PARTS:
        source_part = source / name
        destination_part = destination / name
        if destination_part.exists():
            shutil.rmtree(destination_part)
        shutil.copytree(source_part, destination_part)


def _apply_codex_cachebuster(destination: Path) -> str:
    manifest_path = destination / ".codex-plugin" / "plugin.json"
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    base = str(manifest.get("version") or __version__).split("+", 1)[0]
    version = f"{base}+codex.local-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    manifest["version"] = version
    atomic_write_json(manifest_path, manifest)
    return version


def _marketplace_document() -> dict[str, Any]:
    return {
        "name": MARKETPLACE_NAME,
        "interface": {"displayName": "Groundloop local"},
        "plugins": [
            {
                "name": "groundloop",
                "source": {"source": "local", "path": "./plugins/groundloop"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }
        ],
    }


def _command_ok(args: list[str], timeout: int = 30) -> tuple[bool, str]:
    result = run_process(args, timeout=timeout)
    output = (result.stdout + "\n" + result.stderr).strip()
    return result.returncode == 0, output


def _shim_directory() -> Path:
    override = os.environ.get("GROUNDLOOP_BIN")
    if override:
        return Path(override).expanduser().resolve()
    path_entries = {os.path.normcase(str(Path(item).resolve())) for item in os.environ.get("PATH", "").split(os.pathsep) if item}
    preferred = Path.home() / ".local" / "bin"
    if os.path.normcase(str(preferred.resolve())) in path_entries:
        return preferred.resolve()
    if os.name == "nt":
        windows_apps = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Microsoft" / "WindowsApps"
        if os.path.normcase(str(windows_apps.resolve())) in path_entries:
            return windows_apps.resolve()
    return preferred.resolve()


def _write_shims() -> tuple[Path, bool]:
    destination = _shim_directory()
    destination.mkdir(parents=True, exist_ok=True)
    python = str(Path(sys.executable).resolve())
    if os.name == "nt":
        legacy_noop = 'if /I "%~1"=="hook" if /I "%~5"=="post-tool" exit /b 0\r\nif /I "%~1"=="hook" if /I "%~5"=="user-prompt" exit /b 0\r\n'
        shell_legacy_noop = 'if [ "$1" = "hook" ] && { [ "$5" = "post-tool" ] || [ "$5" = "user-prompt" ]; }; then exit 0; fi\n'
        commands = {
            "groundloop.cmd": f'@echo off\r\nrem {SHIM_MARKER}\r\n{legacy_noop}"{python}" -m groundloop.cli %*\r\n',
            "codex-ground-loop.cmd": f'@echo off\r\nrem {SHIM_MARKER}\r\n"{python}" -m groundloop.launchers codex %*\r\n',
            "claude-ground-loop.cmd": f'@echo off\r\nrem {SHIM_MARKER}\r\n"{python}" -m groundloop.launchers claude %*\r\n',
            "groundloop": f'#!/bin/sh\n# {SHIM_MARKER}\n{shell_legacy_noop}exec "{python.replace(chr(92), "/")}" -m groundloop.cli "$@"\n',
            "codex-ground-loop": f'#!/bin/sh\n# {SHIM_MARKER}\nexec "{python.replace(chr(92), "/")}" -m groundloop.launchers codex "$@"\n',
            "claude-ground-loop": f'#!/bin/sh\n# {SHIM_MARKER}\nexec "{python.replace(chr(92), "/")}" -m groundloop.launchers claude "$@"\n',
        }
    else:
        shell_legacy_noop = 'if [ "$1" = "hook" ] && { [ "$5" = "post-tool" ] || [ "$5" = "user-prompt" ]; }; then exit 0; fi\n'
        commands = {
            "groundloop": f'#!/bin/sh\n# {SHIM_MARKER}\n{shell_legacy_noop}exec "{python}" -m groundloop.cli "$@"\n',
            "codex-ground-loop": f'#!/bin/sh\n# {SHIM_MARKER}\nexec "{python}" -m groundloop.launchers codex "$@"\n',
            "claude-ground-loop": f'#!/bin/sh\n# {SHIM_MARKER}\nexec "{python}" -m groundloop.launchers claude "$@"\n',
        }
    for name, content in commands.items():
        path = destination / name
        path.write_text(content, encoding="utf-8", newline="")
        if os.name != "nt":
            path.chmod(0o755)
    on_path = any(os.path.normcase(str(destination)) == os.path.normcase(str(Path(item).resolve())) for item in os.environ.get("PATH", "").split(os.pathsep) if item)
    return destination, on_path


def install(hosts: list[str], codex_profile: str, dry_run: bool = False) -> dict[str, Any]:
    source = source_plugin_root()
    destination = installed_plugin_dir()
    actions = [f"sync plugin to {destination}"]
    if "codex" in hosts:
        actions.extend([f"register Codex marketplace {marketplace_root()}", "install groundloop@groundloop-local"])
    if "claude" in hosts:
        actions.append("validate Claude plugin")
    if dry_run:
        return {"ok": True, "dry_run": True, "actions": actions}

    _sync_plugin(source, destination)
    installed_version = _apply_codex_cachebuster(destination)
    shim_dir, shim_on_path = _write_shims()
    marketplace_file = marketplace_root() / ".agents" / "plugins" / "marketplace.json"
    atomic_write_json(marketplace_file, _marketplace_document())
    results: dict[str, Any] = {
        "ok": True,
        "plugin_dir": str(destination),
        "plugin_version": installed_version,
        "shim_dir": str(shim_dir),
        "shim_dir_on_path": shim_on_path,
        "actions": actions,
        "hosts": {},
    }

    if "codex" in hosts:
        if not command_path("codex"):
            results["hosts"]["codex"] = {"ok": False, "message": "codex not found"}
            results["ok"] = False
        else:
            listed = run_process(["codex", "plugin", "marketplace", "list"], timeout=45)
            if MARKETPLACE_NAME not in listed.stdout:
                registered, register_output = _command_ok(["codex", "plugin", "marketplace", "add", str(marketplace_root())])
            else:
                registered, register_output = True, "marketplace already registered"
            installed = False
            install_output = "skipped because marketplace registration failed"
            if registered:
                installed, install_output = _command_ok(["codex", "plugin", "add", f"groundloop@{MARKETPLACE_NAME}"])
            results["hosts"]["codex"] = {
                "ok": registered and installed,
                "marketplace": register_output[-800:],
                "plugin": install_output[-800:],
            }
            results["ok"] = results["ok"] and registered and installed

    if "claude" in hosts:
        if not command_path("claude"):
            results["hosts"]["claude"] = {"ok": False, "message": "claude not found"}
            results["ok"] = False
        else:
            valid, output = _command_ok(["claude", "plugin", "validate", str(destination)])
            results["hosts"]["claude"] = {"ok": valid, "message": output[-800:]}
            results["ok"] = results["ok"] and valid

    config = load_global_config()
    config.update(
        {
            "version": __version__,
            "codex_profile": codex_profile,
            "hosts": hosts,
            "plugin_dir": str(destination),
            "plugin_version": installed_version,
            "marketplace_root": str(marketplace_root()),
            "shim_dir": str(shim_dir),
        }
    )
    save_global_config(config)
    return results


def doctor(cwd: Path) -> dict[str, Any]:
    from .config import find_project
    from .status import build_status

    config = load_global_config()
    plugin = Path(config.get("plugin_dir", installed_plugin_dir()))
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    add("groundloop home", groundloop_home().exists(), str(groundloop_home()))
    add("plugin cache", (plugin / ".codex-plugin" / "plugin.json").exists(), str(plugin))
    hooks = read_json(plugin / "hooks" / "hooks.json", {})
    configured_events = set(hooks.get("hooks", {}))
    add(
        "session non-interference",
        configured_events == {"SessionStart"},
        f"configured lifecycle events: {', '.join(sorted(configured_events)) or 'none'}",
    )
    shim_dir = Path(config.get("shim_dir", _shim_directory()))
    shim_name = "groundloop.cmd" if os.name == "nt" else "groundloop"
    add("launch shims", (shim_dir / shim_name).exists(), str(shim_dir))
    add("shim PATH", bool(command_path("groundloop")), command_path("groundloop") or f"add {shim_dir} to PATH")
    add("codex CLI", bool(command_path("codex")), command_path("codex") or "not found")
    add("claude CLI", bool(command_path("claude")), command_path("claude") or "not found")
    if command_path("codex"):
        add("Codex Groundloop bypass", True, "alias bypasses approvals, sandboxing, and hook trust")
    if command_path("claude"):
        add("Claude Groundloop bypass", True, "alias bypasses permission checks")
    found = find_project(cwd)
    if found:
        root, project = found
        status = build_status(root, project)
        add("project configuration", True, str(root))
        add("repository discovery", status["available_count"] == status["repo_count"], f"{status['available_count']}/{status['repo_count']}")
    else:
        add("project configuration", False, "run groundloop init")
    return {"ok": all(item["ok"] for item in checks), "checks": checks}


def uninstall(purge_state: bool = False) -> dict[str, Any]:
    results: dict[str, Any] = {"ok": True, "actions": []}
    if command_path("codex"):
        _command_ok(["codex", "plugin", "remove", f"groundloop@{MARKETPLACE_NAME}"])
        _command_ok(["codex", "plugin", "marketplace", "remove", MARKETPLACE_NAME])
        results["actions"].append("removed Codex plugin registration")
    target = marketplace_root().resolve()
    home = groundloop_home().resolve()
    if target.exists() and is_within(target, home) and target != home:
        shutil.rmtree(target)
        results["actions"].append(f"removed {target}")
    config = load_global_config()
    shim_dir = Path(config.get("shim_dir", _shim_directory())).resolve()
    shim_names = ["groundloop.cmd", "codex-ground-loop.cmd", "claude-ground-loop.cmd", "groundloop", "codex-ground-loop", "claude-ground-loop"] if os.name == "nt" else ["groundloop", "codex-ground-loop", "claude-ground-loop"]
    for name in shim_names:
        path = shim_dir / name
        if path.exists():
            try:
                if SHIM_MARKER in path.read_text(encoding="utf-8", errors="ignore"):
                    path.unlink()
                    results["actions"].append(f"removed {path}")
            except OSError:
                results["ok"] = False
    if purge_state:
        projects = (home / "projects").resolve()
        if projects.exists() and is_within(projects, home) and projects != home:
            shutil.rmtree(projects)
            results["actions"].append(f"removed {projects}")
    return results
