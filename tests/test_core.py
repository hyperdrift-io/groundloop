from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from groundloop.config import create_project_config, find_project, register_project, state_path
from groundloop.hooks import handle_hook
from groundloop.installer import _apply_codex_cachebuster, _write_shims, install
from groundloop.launchers import claude_command, codex_command
from groundloop.state import default_state, load_state, observe_lesson, save_state
from groundloop.status import build_status, format_status
from groundloop.util import iso_now, run_process


class GroundloopTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.workspace = self.base / "workspace"
        self.repo = self.workspace / "service"
        self.repo.mkdir(parents=True)
        self.env = patch.dict(os.environ, {"GROUNDLOOP_HOME": str(self.home)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self._git("init", "-b", "develop")
        self._git("config", "user.email", "groundloop@example.invalid")
        self._git("config", "user.name", "Groundloop Tests")
        (self.repo / "app.txt").write_text("one\n", encoding="utf-8")
        self._git("add", "app.txt")
        self._git("commit", "-m", "initial")
        self.project = create_project_config(self.workspace, [self.repo], "develop")
        register_project(self.project)
        save_state(self.workspace, default_state(self.workspace))

    def _git(self, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=self.repo, text=True, capture_output=True, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def _mark_current(self) -> None:
        state = load_state(self.workspace)
        head = self._git("rev-parse", "HEAD")
        state["last_remote_refresh"] = iso_now()
        state["last_reflection"] = {"completed_at": iso_now(), "report": "test.html", "repo_cutoffs": {"service": head}}
        save_state(self.workspace, state)

    def test_status_moves_from_stale_to_current_to_changes(self) -> None:
        self.assertEqual(build_status(self.workspace, self.project)["status"], "STALE")
        self._mark_current()
        current = build_status(self.workspace, self.project)
        self.assertEqual(current["status"], "CURRENT")
        self.assertLessEqual(len(format_status(current).splitlines()), 8)
        (self.repo / "app.txt").write_text("two\n", encoding="utf-8")
        self._git("add", "app.txt")
        self._git("commit", "-m", "change")
        self.assertEqual(build_status(self.workspace, self.project)["status"], "CHANGES")

    def test_independent_occurrences_activate_lesson(self) -> None:
        first = observe_lesson(self.workspace, "PowerShell variable reference is not valid", session_id="one", tags=["powershell"])
        second = observe_lesson(self.workspace, "PowerShell variable reference is not valid", session_id="two", tags=["powershell"])
        self.assertEqual(first["status"], "watch")
        self.assertEqual(second["status"], "active")
        self.assertEqual(second["occurrences"], 2)

    def test_session_hook_is_opt_in_and_records_variant(self) -> None:
        payload = {"cwd": str(self.workspace), "session_id": "session-1"}
        with patch.dict(os.environ, {"GROUNDLOOP_ENABLED": "0"}, clear=False):
            self.assertEqual(handle_hook("codex", "session-start", payload), "")
        with patch.dict(os.environ, {"GROUNDLOOP_ENABLED": "1", "GROUNDLOOP_VARIANT": "treatment"}, clear=False):
            output = handle_hook("codex", "session-start", payload)
        self.assertIn("GROUNDLOOP", output)
        sessions = load_state(self.workspace)["sessions"]
        self.assertEqual(sessions[0]["variant"], "treatment")

    def test_failed_tool_is_fingerprinted_and_later_relevant(self) -> None:
        with patch.dict(os.environ, {"GROUNDLOOP_ENABLED": "1"}, clear=False):
            for session in ("a", "b"):
                handle_hook(
                    "codex",
                    "post-tool",
                    {
                        "cwd": str(self.workspace),
                        "session_id": session,
                        "tool_response": "Script failed. Exit code: 1 ParserError: invalid escaping in PowerShell",
                    },
                )
            guidance = handle_hook(
                "codex",
                "user-prompt",
                {"cwd": str(self.workspace), "session_id": "c", "prompt": "Write a PowerShell script for the workspace"},
            )
        self.assertIn("operational lessons", guidance)
        self.assertNotIn("Write a PowerShell script", json.dumps(load_state(self.workspace)))

    def test_launchers_preserve_arguments(self) -> None:
        self.assertEqual(codex_command(["--search", "hello"], "work"), ["codex", "--profile", "work", "--search", "hello"])
        self.assertEqual(codex_command(["-p", "review"], "work"), ["codex", "-p", "review"])
        self.assertEqual(claude_command(["--model", "sonnet"], "C:/plugin"), ["claude", "--plugin-dir", "C:/plugin", "--model", "sonnet"])

    def test_shared_config_is_relative_and_loadable(self) -> None:
        register_project(self.project, shared=True)
        shared = json.loads((self.workspace / ".groundloop" / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(shared["workspace"], ".")
        self.assertEqual(shared["repositories"][0]["path"], "service")
        found = find_project(self.repo)
        self.assertIsNotNone(found)
        self.assertEqual(Path(found[1]["repositories"][0]["path"]), self.repo.resolve())

    def test_install_dry_run_has_no_user_writes(self) -> None:
        marketplace = self.home / "marketplace"
        self.assertFalse(marketplace.exists())
        result = install(["codex", "claude"], "work", dry_run=True)
        self.assertTrue(result["ok"])
        self.assertFalse(marketplace.exists())

    def test_installer_writes_cross_shell_shims(self) -> None:
        destination = self.base / "bin"
        with patch("groundloop.installer._shim_directory", return_value=destination):
            _write_shims()
        self.assertTrue((destination / "groundloop").exists())
        if os.name == "nt":
            self.assertTrue((destination / "groundloop.cmd").exists())

    def test_cachebuster_replaces_existing_suffix(self) -> None:
        plugin = self.base / "plugin" / ".codex-plugin"
        plugin.mkdir(parents=True)
        manifest = plugin / "plugin.json"
        manifest.write_text('{"name":"groundloop","version":"0.1.0+codex.old"}', encoding="utf-8")
        first = _apply_codex_cachebuster(plugin.parent)
        self.assertTrue(first.startswith("0.1.0+codex.local-"))
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["version"], first)

    def test_subprocess_timeout_is_structured(self) -> None:
        result = run_process([os.sys.executable, "-c", "import time; time.sleep(1)"], timeout=0.01)
        self.assertEqual(result.returncode, 124)
        self.assertIn("timed out", result.stderr)


if __name__ == "__main__":
    unittest.main()
