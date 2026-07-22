from __future__ import annotations

import hashlib
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import SCHEMA_VERSION, project_dir, state_path
from .util import atomic_write_json, iso_now, read_json, redact


def default_state(root: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "workspace": str(root.resolve()),
        "created_at": iso_now(),
        "updated_at": iso_now(),
        "last_remote_refresh": None,
        "last_reflection": None,
        "sessions": [],
        "operational_lessons": [],
        "proposals": [],
        "scout": {"last_reviewed_at": None, "next_due_at": None},
    }


@contextmanager
def state_lock(root: Path, timeout: float = 3.0) -> Iterator[None]:
    lock = project_dir(root) / "locks" / "state.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"{os.getpid()} {time.time()}".encode())
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 30:
                    lock.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("Groundloop state is busy")
            time.sleep(0.05)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def load_state(root: Path) -> dict[str, Any]:
    return read_json(state_path(root), default_state(root))


def save_state(root: Path, state: dict[str, Any]) -> None:
    state["schema_version"] = SCHEMA_VERSION
    state["updated_at"] = iso_now()
    atomic_write_json(state_path(root), state)


def mutate_state(root: Path, operation) -> dict[str, Any]:
    with state_lock(root):
        state = load_state(root)
        operation(state)
        save_state(root, state)
        return state


def record_session(root: Path, session_id: str, host: str, variant: str) -> None:
    if not session_id:
        return

    def update(state: dict[str, Any]) -> None:
        sessions = state.setdefault("sessions", [])
        if not any(item.get("session_id") == session_id for item in sessions):
            sessions.append({"session_id": session_id, "host": host, "variant": variant, "started_at": iso_now()})
            del sessions[:-100]

    mutate_state(root, update)


def _normalise_signature(text: str) -> str:
    value = redact(text, 700).lower()
    value = re.sub(r"\b[0-9a-f]{7,40}\b", "<hash>", value)
    value = re.sub(r"\b\d+\b", "<n>", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def observe_lesson(
    root: Path,
    symptom: str,
    category: str = "scripts/tooling",
    resolution: str | None = None,
    severity: str = "normal",
    session_id: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    signature = _normalise_signature(symptom)
    identifier = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
    result: dict[str, Any] = {}

    def update(state: dict[str, Any]) -> None:
        lessons = state.setdefault("operational_lessons", [])
        lesson = next((item for item in lessons if item.get("id") == identifier), None)
        if lesson is None:
            lesson = {
                "id": identifier,
                "category": category,
                "symptom": redact(symptom, 500),
                "signature": signature,
                "resolution": redact(resolution, 500) if resolution else None,
                "severity": severity,
                "status": "active" if severity == "severe" else "watch",
                "first_seen": iso_now(),
                "last_seen": iso_now(),
                "occurrences": 0,
                "sessions": [],
                "tags": sorted(set(tags or [])),
                "destination": None,
            }
            lessons.append(lesson)
        lesson["last_seen"] = iso_now()
        if resolution:
            lesson["resolution"] = redact(resolution, 500)
        known_sessions = lesson.setdefault("sessions", [])
        occurrence_key = session_id or f"manual:{lesson['last_seen']}"
        if occurrence_key not in known_sessions:
            known_sessions.append(occurrence_key)
            lesson["occurrences"] = len(known_sessions)
        lesson["tags"] = sorted(set(lesson.get("tags", [])) | set(tags or []))
        if lesson.get("severity") == "severe" or lesson["occurrences"] >= 2:
            lesson["status"] = "active"
        result.update(lesson)

    mutate_state(root, update)
    return result


def change_lesson(root: Path, identifier: str, status: str, destination: str | None = None) -> bool:
    found = False

    def update(state: dict[str, Any]) -> None:
        nonlocal found
        for lesson in state.setdefault("operational_lessons", []):
            if lesson.get("id") == identifier:
                lesson["status"] = status
                lesson["reviewed_at"] = iso_now()
                if destination:
                    lesson["destination"] = redact(destination, 500)
                found = True
                break

    mutate_state(root, update)
    return found
