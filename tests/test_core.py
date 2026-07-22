from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from groundloop.cli import _read_hook_input, command_hook
from groundloop.config import create_project_config, find_project, register_project, state_path
from groundloop.hooks import failure_excerpt, handle_hook
from groundloop.installer import _apply_codex_cachebuster, _write_shims, install
from groundloop.launchers import _run_groundloop_session, claude_command, codex_command
from groundloop.sessions import finalize_session
from groundloop.state import default_state, load_state, observe_lesson, save_state
from groundloop.status import build_status, format_status
from groundloop.util import iso_now, run_process


class GroundloopTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.codex_home = self.base / "codex"
        self.workspace = self.base / "workspace"
        self.repo = self.workspace / "service"
        self.repo.mkdir(parents=True)
        self.env = patch.dict(
            os.environ,
            {"GROUNDLOOP_HOME": str(self.home), "CODEX_HOME": str(self.codex_home)},
            clear=False,
        )
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
        with patch.dict(
            os.environ,
            {"GROUNDLOOP_ENABLED": "1", "GROUNDLOOP_VARIANT": "treatment", "GROUNDLOOP_LAUNCH_ID": "launch-1"},
            clear=False,
        ):
            output = handle_hook("codex", "session-start", payload)
        self.assertIn("GROUNDLOOP", output)
        sessions = load_state(self.workspace)["sessions"]
        self.assertEqual(sessions[0]["variant"], "treatment")
        self.assertEqual(sessions[0]["launch_id"], "launch-1")

    def test_learning_runs_only_after_the_agent_session_closes(self) -> None:
        payload = {"cwd": str(self.workspace), "session_id": "session-closed"}
        with patch.dict(
            os.environ,
            {
                "GROUNDLOOP_ENABLED": "1",
                "GROUNDLOOP_VARIANT": "treatment",
                "GROUNDLOOP_LAUNCH_ID": "launch-closed",
            },
            clear=False,
        ):
            handle_hook("codex", "session-start", payload)
            self.assertEqual(handle_hook("codex", "post-tool", {**payload, "tool_response": "Script failed"}), "")
        self.assertEqual(load_state(self.workspace)["operational_lessons"], [])

        transcript = self.codex_home / "sessions" / "2026" / "07" / "22" / "rollout-session-closed.jsonl"
        transcript.parent.mkdir(parents=True)
        records = [
            {
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "call_id": "call-1", "name": "exec"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-1",
                    "output": "Script failed. Exit code: 1 ParserError: invalid escaping in PowerShell",
                },
            },
        ]
        transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

        result = finalize_session(self.workspace, "codex", "launch-closed")
        self.assertEqual(result["status"], "processed")
        self.assertEqual(result["observations"], 1)
        state = load_state(self.workspace)
        self.assertEqual(len(state["operational_lessons"]), 1)
        self.assertEqual(state["sessions"][0]["learning_status"], "processed")

    def test_successful_deprecation_documentation_is_not_a_failure(self) -> None:
        payload = {"tool_response": "Exit code: 0\n.. Note:: Parameter experiment_ids is deprecated. Use locations instead."}
        self.assertIsNone(failure_excerpt(payload))
        warning = {"tool_response": "Exit code: 0\nFutureWarning: Parameter experiment_ids is deprecated."}
        self.assertIn("FutureWarning", failure_excerpt(warning) or "")

    def test_structured_failure_excerpt_omits_response_wrapper(self) -> None:
        payload = {"tool_response": {"success": False, "output": "command stopped without an exit-code marker"}}
        excerpt = failure_excerpt(payload) or ""
        self.assertIn("command stopped", excerpt)
        self.assertNotIn('"success"', excerpt)

    def test_hook_failures_fail_open_without_recording_sensitive_details(self) -> None:
        args = SimpleNamespace(host="codex", event="post-tool")
        with patch("sys.stdin", io.StringIO("{}")), patch(
            "groundloop.cli.handle_hook", side_effect=RuntimeError("sensitive payload")
        ):
            self.assertEqual(command_hook(args), 0)
        diagnostic = (self.home / "hook-errors.jsonl").read_text(encoding="utf-8")
        self.assertIn("RuntimeError", diagnostic)
        self.assertNotIn("sensitive payload", diagnostic)

    def test_hook_input_is_drained_and_rejects_oversized_payloads(self) -> None:
        stream = io.StringIO("0123456789abcdef")
        with patch("sys.stdin", stream):
            self.assertIsNone(_read_hook_input(limit=8))
        self.assertEqual(stream.tell(), 16)

    def test_launchers_preserve_arguments(self) -> None:
        codex_bypass = ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"]
        self.assertEqual(
            codex_command(["--search", "hello"], "work"),
            ["codex", *codex_bypass, "--profile", "work", "--search", "hello"],
        )
        self.assertEqual(codex_command(["-p", "review"], "work"), ["codex", *codex_bypass, "-p", "review"])
        self.assertEqual(
            claude_command(["--model", "sonnet"], "C:/plugin"),
            ["claude", "--dangerously-skip-permissions", "--plugin-dir", "C:/plugin", "--model", "sonnet"],
        )

    def test_launchers_do_not_duplicate_bypass_arguments(self) -> None:
        codex_args = ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"]
        self.assertEqual(codex_command(codex_args, "work").count("--dangerously-bypass-hook-trust"), 1)
        claude_args = ["--dangerously-skip-permissions"]
        self.assertEqual(claude_command(claude_args, None).count("--dangerously-skip-permissions"), 1)

    def test_launcher_finalizes_only_after_child_returns(self) -> None:
        events: list[str] = []

        def run_child(command, env):
            events.append("child")
            return 7

        def finalize(workspace, host, launch_id):
            events.append("finalize")
            return {"status": "processed", "observations": 0}

        with patch("groundloop.launchers._run", side_effect=run_child), patch(
            "groundloop.config.find_project", return_value=(self.workspace, self.project)
        ), patch("groundloop.sessions.finalize_session", side_effect=finalize):
            self.assertEqual(_run_groundloop_session(["codex"], "codex"), 7)
        self.assertEqual(events, ["child", "finalize"])

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
        self.assertIn("post-tool", (destination / "groundloop").read_text(encoding="utf-8"))
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
