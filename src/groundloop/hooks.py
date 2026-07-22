from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .config import find_project
from .state import record_session
from .status import build_status, format_status
from .util import redact


ISSUE_PATTERNS = (
    re.compile(r"(?i)exit code[:=]\s*(-?\d+)"),
    re.compile(r"(?i)\b(script failed|script error|error:|parsererror|traceback)\b"),
    re.compile(r"(?i)\b(invalid escap|unrecognized flag|not recognized as)\b"),
    re.compile(r"(?i)\b(futurewarning|deprecationwarning|runtimewarning|userwarning|warning:)\b"),
)


def detect_host(requested: str) -> str:
    if requested != "auto":
        return requested
    if os.environ.get("PLUGIN_ROOT"):
        return "codex"
    if os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return "claude"
    return "unknown"


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_text(item) for item in value)))
    if isinstance(value, dict):
        preferred = ("output", "stderr", "error", "message", "content", "text")
        parts = [_text(value[key]) for key in preferred if key in value]
        if parts:
            return "\n".join(filter(None, parts))
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def failure_excerpt(payload: dict[str, Any]) -> str | None:
    response = payload.get("tool_response") or payload.get("tool_result") or payload.get("output") or ""
    text = _text(response)
    if not text:
        return None
    explicitly_failed = isinstance(response, dict) and (
        response.get("success") is False
        or response.get("status") in {"failed", "error"}
        or isinstance(response.get("exit_code"), int)
        and response["exit_code"] != 0
        or isinstance(response.get("returncode"), int)
        and response["returncode"] != 0
    )
    exit_match = ISSUE_PATTERNS[0].search(text)
    if exit_match and int(exit_match.group(1)) == 0:
        exit_match = None
    if not explicitly_failed and not exit_match and not any(pattern.search(text) for pattern in ISSUE_PATTERNS[1:]):
        return None
    marker = next((match for pattern in ISSUE_PATTERNS for match in [pattern.search(text)] if match), None)
    start = max(0, marker.start() - 100) if marker else 0
    return redact(text[start : start + 800], 700)


def lesson_tags(text: str) -> list[str]:
    lowered = text.lower()
    known = {
        "powershell": ("powershell", "parsererror", "variable reference"),
        "git": ("git ", "repository", "revision", "refspec"),
        "python": ("python", "traceback", "pip"),
        "shell": ("bash", "shell", "escape", "quot"),
        "javascript": ("node", "npm", "yarn", "typescript"),
    }
    return [tag for tag, needles in known.items() if any(needle in lowered for needle in needles)]


def handle_hook(host: str, event: str, payload: dict[str, Any]) -> str:
    if os.environ.get("GROUNDLOOP_ENABLED") != "1":
        return ""
    cwd = Path(payload.get("cwd") or os.getcwd())
    found = find_project(cwd)
    if not found:
        return "GROUNDLOOP - PARTIAL\nWorkspace is not initialised; run groundloop init.\n" if event == "session-start" else ""
    root, project = found
    resolved_host = detect_host(host)
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "")

    if event == "session-start":
        record_session(
            root,
            session_id,
            resolved_host,
            os.environ.get("GROUNDLOOP_VARIANT", "treatment"),
            launch_id=os.environ.get("GROUNDLOOP_LAUNCH_ID"),
        )
        return format_status(build_status(root, project)) + "\n"
    # Compatibility for sessions that loaded an older plugin definition.
    # Learning is deliberately deferred until the launcher-owned child process exits.
    return ""
