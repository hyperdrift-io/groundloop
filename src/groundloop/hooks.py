from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .config import find_project
from .state import load_state, observe_lesson, record_session
from .status import build_status, format_status
from .util import redact


ISSUE_PATTERNS = (
    re.compile(r"(?i)exit code[:=]\s*(-?\d+)"),
    re.compile(r"(?i)\b(script failed|script error|error:|parsererror|traceback)\b"),
    re.compile(r"(?i)\b(invalid escap|unrecognized flag|not recognized as|deprecated|will be removed)\b"),
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
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def failure_excerpt(payload: dict[str, Any]) -> str | None:
    response = payload.get("tool_response") or payload.get("tool_result") or payload.get("output") or ""
    text = _text(response)
    if not text:
        return None
    exit_match = ISSUE_PATTERNS[0].search(text)
    if exit_match and int(exit_match.group(1)) == 0:
        exit_match = None
    if not exit_match and not any(pattern.search(text) for pattern in ISSUE_PATTERNS[1:]):
        return None
    marker = next((match for pattern in ISSUE_PATTERNS for match in [pattern.search(text)] if match), None)
    start = max(0, marker.start() - 100) if marker else 0
    return redact(text[start : start + 800], 700)


def _lesson_tags(text: str) -> list[str]:
    lowered = text.lower()
    known = {
        "powershell": ("powershell", "parsererror", "variable reference"),
        "git": ("git ", "repository", "revision", "refspec"),
        "python": ("python", "traceback", "pip"),
        "shell": ("bash", "shell", "escape", "quot"),
        "javascript": ("node", "npm", "yarn", "typescript"),
    }
    return [tag for tag, needles in known.items() if any(needle in lowered for needle in needles)]


def _relevant_lessons(root: Path, prompt: str) -> list[dict[str, Any]]:
    tokens = set(re.findall(r"[a-z][a-z0-9_-]{2,}", prompt.lower()))
    lessons = [item for item in load_state(root).get("operational_lessons", []) if item.get("status") == "active"]
    scored: list[tuple[int, dict[str, Any]]] = []
    for lesson in lessons:
        haystack = " ".join(
            [lesson.get("category") or "", lesson.get("symptom") or "", lesson.get("resolution") or "", *lesson.get("tags", [])]
        ).lower()
        score = sum(1 for token in tokens if token in haystack)
        if score or any(tag in tokens for tag in lesson.get("tags", [])):
            scored.append((score, lesson))
    return [lesson for _, lesson in sorted(scored, key=lambda pair: pair[0], reverse=True)[:2]]


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
        record_session(root, session_id, resolved_host, os.environ.get("GROUNDLOOP_VARIANT", "treatment"))
        return format_status(build_status(root, project)) + "\n"

    if event == "post-tool":
        excerpt = failure_excerpt(payload)
        if excerpt:
            observe_lesson(
                root,
                symptom=excerpt,
                category="scripts/tooling",
                session_id=session_id or None,
                tags=_lesson_tags(excerpt),
            )
        return ""

    if event == "user-prompt":
        prompt = str(payload.get("prompt") or "")
        lessons = _relevant_lessons(root, prompt)
        if not lessons:
            return ""
        lines = ["Groundloop relevant operational lessons:"]
        for lesson in lessons:
            resolution = lesson.get("resolution") or "Review the recorded symptom before repeating the workflow."
            lines.append(f"- {lesson['symptom']}: {resolution}")
        return redact("\n".join(lines), 900) + "\n"
    return ""
