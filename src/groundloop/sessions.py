from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

from .config import reports_dir
from .hooks import failure_excerpt, lesson_tags
from .state import load_state, mutate_state, observe_lesson
from .util import atomic_write_json, iso_now, now_utc, redact


def _json_lines(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        yield value
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _limited(items: list[str], text: str, limit: int = 20, max_length: int = 2000) -> None:
    if len(items) < limit and text.strip():
        items.append(redact(text, max_length))


def _recent_files(root: Path, cutoff_timestamp: float, limit: int) -> list[Path]:
    if not root.exists():
        return []
    files = [path for path in root.rglob("*.jsonl") if path.is_file() and path.stat().st_mtime >= cutoff_timestamp]
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


def _codex_sessions(cutoff_timestamp: float, workspace: Path, limit: int) -> list[dict[str, Any]]:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    sessions: list[dict[str, Any]] = []
    for path in _recent_files(codex_home / "sessions", cutoff_timestamp, limit):
        metadata: dict[str, Any] = {}
        users: list[str] = []
        finals: list[str] = []
        failures: list[dict[str, str]] = []
        calls: dict[str, str] = {}
        for record in _json_lines(path):
            payload = record.get("payload") or {}
            if record.get("type") == "session_meta" and isinstance(payload, dict):
                metadata = payload
            elif record.get("type") == "event_msg" and payload.get("type") == "user_message":
                _limited(users, str(payload.get("message") or ""))
            elif record.get("type") == "event_msg" and payload.get("type") == "agent_message" and payload.get("phase") == "final_answer":
                _limited(finals, str(payload.get("message") or ""), limit=8)
            elif record.get("type") == "response_item" and payload.get("type") in {"function_call", "custom_tool_call"}:
                calls[str(payload.get("call_id") or "")] = str(payload.get("name") or "unknown")
            elif record.get("type") == "response_item" and payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
                excerpt = failure_excerpt({"output": payload.get("output")})
                if excerpt and len(failures) < 15:
                    failures.append({"tool": calls.get(str(payload.get("call_id") or ""), "unknown"), "excerpt": excerpt})
        cwd = Path(str(metadata.get("cwd") or "."))
        try:
            relevant = cwd.resolve().is_relative_to(workspace.resolve())
        except (OSError, ValueError):
            relevant = False
        if metadata and users and relevant:
            sessions.append(
                {
                    "provider": "codex",
                    "session_id": str(metadata.get("session_id") or path.stem),
                    "source_file": path.name,
                    "modified_at": path.stat().st_mtime,
                    "cwd": str(cwd),
                    "user_messages": users,
                    "final_response": finals[-1] if finals else "",
                    "tool_failures": failures,
                }
            )
    return sessions


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    return content if isinstance(content, list) else []


def _claude_sessions(cutoff_timestamp: float, workspace: Path, limit: int) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for path in _recent_files(Path.home() / ".claude" / "projects", cutoff_timestamp, limit):
        users: list[str] = []
        assistants: list[str] = []
        failures: list[dict[str, str]] = []
        calls: dict[str, str] = {}
        cwd_text = ""
        session_id = path.stem
        for record in _json_lines(path):
            cwd_text = str(record.get("cwd") or cwd_text)
            session_id = str(record.get("sessionId") or session_id)
            message = record.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            if record.get("type") == "assistant":
                for block in _content_blocks(content):
                    if block.get("type") == "tool_use":
                        calls[str(block.get("id") or "")] = str(block.get("name") or "unknown")
                    elif block.get("type") == "text":
                        _limited(assistants, str(block.get("text") or ""), limit=8)
                continue
            if record.get("type") != "user" or record.get("isMeta") or record.get("toolUseResult") is not None:
                continue
            if isinstance(content, str):
                if not content.lstrip().startswith(("<command-name", "<local-command", "<bash-input", "<bash-stdout")):
                    _limited(users, content)
                continue
            for block in _content_blocks(content):
                if block.get("type") == "text":
                    _limited(users, str(block.get("text") or ""))
                elif block.get("type") == "tool_result" and len(failures) < 15:
                    excerpt = None
                    if block.get("is_error"):
                        excerpt = redact(str(block.get("content") or ""), 700)
                    else:
                        excerpt = failure_excerpt({"output": block.get("content")})
                    if excerpt:
                        failures.append({"tool": calls.get(str(block.get("tool_use_id") or ""), "unknown"), "excerpt": excerpt})
        cwd = Path(cwd_text or ".")
        try:
            relevant = cwd.resolve().is_relative_to(workspace.resolve())
        except (OSError, ValueError):
            relevant = False
        if users and relevant:
            sessions.append(
                {
                    "provider": "claude",
                    "session_id": session_id,
                    "source_file": path.name,
                    "modified_at": path.stat().st_mtime,
                    "cwd": str(cwd),
                    "user_messages": users,
                    "final_response": assistants[-1] if assistants else "",
                    "tool_failures": failures,
                }
            )
    return sessions


def _session_file(host: str, session_id: str) -> Path | None:
    if host == "codex":
        root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
        pattern = f"*{session_id}.jsonl"
    elif host == "claude":
        root = Path.home() / ".claude" / "projects"
        pattern = f"{session_id}.jsonl"
    else:
        return None
    if not root.exists():
        return None
    return next((path for path in root.rglob(pattern) if path.is_file()), None)


def _closed_session_failures(host: str, path: Path, limit: int = 25) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    calls: dict[str, str] = {}
    if host == "codex":
        for record in _json_lines(path):
            payload = record.get("payload") or {}
            if record.get("type") == "response_item" and payload.get("type") in {"function_call", "custom_tool_call"}:
                calls[str(payload.get("call_id") or "")] = str(payload.get("name") or "unknown")
            elif record.get("type") == "response_item" and payload.get("type") in {
                "function_call_output",
                "custom_tool_call_output",
            }:
                excerpt = failure_excerpt({"output": payload.get("output")})
                if excerpt:
                    failures.append({"tool": calls.get(str(payload.get("call_id") or ""), "unknown"), "excerpt": excerpt})
                    if len(failures) >= limit:
                        break
        return failures

    for record in _json_lines(path):
        message = record.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if record.get("type") == "assistant":
            for block in _content_blocks(content):
                if block.get("type") == "tool_use":
                    calls[str(block.get("id") or "")] = str(block.get("name") or "unknown")
            continue
        if record.get("type") != "user":
            continue
        for block in _content_blocks(content):
            if block.get("type") != "tool_result":
                continue
            excerpt = (
                redact(str(block.get("content") or ""), 700)
                if block.get("is_error")
                else failure_excerpt({"output": block.get("content")})
            )
            if excerpt:
                failures.append({"tool": calls.get(str(block.get("tool_use_id") or ""), "unknown"), "excerpt": excerpt})
                if len(failures) >= limit:
                    return failures
    return failures


def finalize_session(workspace: Path, host: str, launch_id: str) -> dict[str, Any]:
    """Process one launcher-owned session after its agent child has exited."""
    state = load_state(workspace)
    session = next(
        (
            item
            for item in reversed(state.get("sessions", []))
            if item.get("launch_id") == launch_id and item.get("host") == host
        ),
        None,
    )
    if not session:
        return {"status": "not-recorded", "observations": 0}
    session_id = str(session.get("session_id") or "")
    path = _session_file(host, session_id)
    if path is None:
        status = "transcript-missing"
        failures: list[dict[str, str]] = []
    else:
        status = "processed"
        failures = _closed_session_failures(host, path)
        for failure in failures:
            observe_lesson(
                workspace,
                symptom=failure["excerpt"],
                category="scripts/tooling",
                session_id=session_id,
                tags=lesson_tags(f"{failure['tool']} {failure['excerpt']}"),
            )

    def close(current: dict[str, Any]) -> None:
        match = next((item for item in current.setdefault("sessions", []) if item.get("session_id") == session_id), None)
        if match:
            match.update(
                {
                    "closed_at": iso_now(),
                    "learning_status": status,
                    "observations": len(failures),
                }
            )

    mutate_state(workspace, close)
    return {"status": status, "session_id": session_id, "observations": len(failures)}


def create_digest(workspace: Path, since_hours: int, max_sessions: int = 100) -> tuple[Path, dict[str, Any]]:
    cutoff = now_utc() - timedelta(hours=since_hours)
    cutoff_timestamp = cutoff.timestamp()
    codex = _codex_sessions(cutoff_timestamp, workspace, max_sessions)
    claude = _claude_sessions(cutoff_timestamp, workspace, max_sessions)
    document = {
        "generated_at": iso_now(),
        "cutoff": cutoff.isoformat(),
        "workspace": str(workspace),
        "privacy": "User text, final assistant text, and concise failed-tool excerpts only; tool inputs and successful outputs excluded; pattern redaction applied.",
        "counts": {"codex_sessions": len(codex), "claude_sessions": len(claude)},
        "sessions": sorted(codex + claude, key=lambda item: item["modified_at"], reverse=True),
    }
    destination = reports_dir(workspace) / "digests" / f"thread-digest-{now_utc().strftime('%Y%m%d-%H%M%S')}.json"
    atomic_write_json(destination, document)
    return destination, document
