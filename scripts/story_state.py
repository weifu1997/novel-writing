#!/usr/bin/env python3
"""Transactional continuity state for long-running fiction projects.

The SQLite database is the authority for accepted chapter snapshots and story
facts. Markdown files remain editable working copies; ``verify`` detects drift
and ``checkout`` restores an accepted snapshot without pretending that several
filesystem files can be committed atomically with SQLite.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from chapter_guard import METRIC as LENGTH_METRIC, extract_body, measurements
from prose_lint import lint as prose_lint


SCHEMA_VERSION = 2
DELTA_VERSION = 1
DATABASE_RELATIVE = Path("追踪") / "story-state.sqlite3"
ENTITY_TYPES = {"character", "item", "location", "faction"}
THREAD_TYPES = {"foreshadow", "promise", "question", "debt"}
THREAD_STATUSES = {"open", "advanced", "resolved", "abandoned"}
IMPORTANCE_LEVELS = {"low", "medium", "high", "critical"}
TIMELINE_MODES = {"present", "flashback", "dream", "unknown"}
REVEAL_STATUSES = {"hidden", "partial", "revealed"}
CHAPTER_MODES = {"append", "revision", "revalidate"}
LENGTH_METRICS = {LENGTH_METRIC, "han_chars"}
DERIVED_VIEW_DIR = Path("追踪") / "视图"
HOT_ENTITY_LIMIT = 8
HOT_THREAD_LIMIT = 8
THREAD_IMPORTANCE_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
REFERENCE_KEYS = {
    "location_id",
    "holder_id",
    "owner_id",
    "leader_id",
    "parent_id",
    "faction_id",
    "faction_ids",
    "member_ids",
    "character_id",
    "participant_ids",
    "related_ids",
    "source_id",
    "target_id",
}
RESOURCE_TABLES = {
    "entity": ("entities", "entity_id"),
    "thread": ("story_threads", "thread_id"),
    "timeline": ("timeline_events", "event_id"),
    "volume": ("volumes", "volume_id"),
}
MISSING = object()


class StateError(ValueError):
    """A user-correctable state, schema, or continuity error."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise StateError(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def as_object(value: Any, label: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{label} must be an object")
    return value


def as_list(value: Any, label: str) -> list[Any]:
    require(isinstance(value, list), f"{label} must be an array")
    return value


def clean_string(value: Any, label: str, *, allow_empty: bool = False, maximum: int = 500) -> str:
    require(isinstance(value, str), f"{label} must be a string")
    result = value.strip()
    require(allow_empty or bool(result), f"{label} must not be empty")
    require(len(result.encode("utf-8")) <= maximum, f"{label} is too long")
    return result


def clean_id(value: Any, label: str) -> str:
    result = clean_string(value, label, maximum=120)
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", result) is not None, f"{label} is not a stable ID")
    return result


def reject_unknown(document: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(document) - allowed)
    require(not unknown, f"{label} contains unsupported fields: {', '.join(unknown)}")


def normalized_evidence(value: str) -> str:
    return re.sub(r"\s+", "", value)


def require_evidence(body: str, value: Any, label: str) -> str:
    evidence = clean_string(value, label, maximum=600)
    require(
        normalized_evidence(evidence) in normalized_evidence(extract_body(body)),
        f"{label} is not present in the chapter body",
    )
    return evidence


def read_utf8(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise StateError(f"unable to read {label} {path}: {exc}") from exc


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(read_utf8(path, label))
    except json.JSONDecodeError as exc:
        raise StateError(f"invalid JSON in {label} {path}: {exc}") from exc
    return as_object(value, label)


def project_path(project: Path, value: Path) -> str:
    resolved = value.resolve()
    try:
        return str(resolved.relative_to(project.resolve()))
    except ValueError:
        return str(resolved)


def resolve_project_path(project: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project / path


def database_path(project: Path) -> Path:
    return project.resolve() / DATABASE_RELATIVE


def connect(project: Path, *, must_exist: bool = True) -> sqlite3.Connection:
    path = database_path(project)
    existed = path.is_file()
    if must_exist:
        require(existed, f"state database does not exist: {path}; run init first")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    if existed:
        migrate_database(connection)
    return connection


SCHEMA = r"""
PRAGMA journal_mode = WAL;
CREATE TABLE project_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE commits (
    commit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER,
    state_revision INTEGER NOT NULL UNIQUE,
    chapter_id TEXT NOT NULL,
    chapter_seq INTEGER NOT NULL,
    mode TEXT NOT NULL,
    continuity_changed INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    payload_json TEXT NOT NULL,
    rollback_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE volumes (
    volume_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    title TEXT NOT NULL,
    planned_end_chapter INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    state_json TEXT NOT NULL DEFAULT '{}',
    updated_commit INTEGER
);
CREATE TABLE chapters (
    chapter_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    title TEXT NOT NULL,
    volume_id TEXT,
    current_commit_id INTEGER NOT NULL,
    body_path TEXT NOT NULL,
    record_path TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    style_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    accepted_at TEXT NOT NULL
);
CREATE TABLE chapter_versions (
    commit_id INTEGER PRIMARY KEY,
    chapter_id TEXT NOT NULL,
    body_content TEXT NOT NULL,
    record_content TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    body_path TEXT NOT NULL,
    record_path TEXT NOT NULL,
    style_json TEXT NOT NULL,
    length_json TEXT NOT NULL
);
CREATE TABLE entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    name TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    state_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_chapter TEXT NOT NULL,
    updated_chapter TEXT NOT NULL,
    updated_commit INTEGER
);
CREATE TABLE story_threads (
    thread_id TEXT PRIMARY KEY,
    thread_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    importance TEXT NOT NULL,
    introduced_chapter INTEGER NOT NULL,
    due_chapter INTEGER,
    due_volume_id TEXT,
    last_progress_chapter INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    updated_commit INTEGER
);
CREATE TABLE timeline_events (
    event_id TEXT PRIMARY KEY,
    sequence REAL NOT NULL,
    story_time TEXT NOT NULL,
    mode TEXT NOT NULL,
    fact TEXT NOT NULL,
    location_id TEXT,
    participants_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    updated_commit INTEGER
);
CREATE TABLE resource_changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_id INTEGER NOT NULL,
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    fields_json TEXT NOT NULL
);
CREATE TABLE touches (
    touch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_id INTEGER NOT NULL,
    chapter_id TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    field TEXT NOT NULL,
    access TEXT NOT NULL
);
CREATE TABLE dependencies (
    dependency_id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_id INTEGER NOT NULL,
    dependent_chapter TEXT NOT NULL,
    prerequisite_kind TEXT NOT NULL,
    prerequisite_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE arc_beats (
    beat_id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_id INTEGER NOT NULL,
    chapter_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    beat TEXT NOT NULL,
    before_text TEXT,
    after_text TEXT,
    evidence TEXT NOT NULL
);
CREATE TABLE volume_audits (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id TEXT NOT NULL,
    end_commit_id INTEGER NOT NULL,
    state_revision INTEGER NOT NULL,
    end_chapter_seq INTEGER NOT NULL,
    stale_after INTEGER NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by_commit_id INTEGER
);
CREATE INDEX touches_resource_idx ON touches(resource_kind, resource_id, field, commit_id);
CREATE INDEX dependencies_prerequisite_idx ON dependencies(prerequisite_kind, prerequisite_id);
CREATE INDEX dependencies_dependent_idx ON dependencies(dependent_chapter);
CREATE INDEX arc_beats_character_idx ON arc_beats(character_id, chapter_id);
CREATE INDEX volume_audits_open_idx ON volume_audits(volume_id, status, resolved_at, audit_id);
"""


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def migrate_database(connection: sqlite3.Connection) -> None:
    meta_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'project_meta'"
    ).fetchone()
    if meta_table is None:
        return
    version = meta_get(connection, "schema_version")
    require(isinstance(version, int), "database schema version is invalid")
    require(version <= SCHEMA_VERSION, f"database schema {version} is newer than supported {SCHEMA_VERSION}")
    if version == SCHEMA_VERSION:
        return
    require(version == 1, f"no migration path from database schema {version}")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if "length_json" not in table_columns(connection, "chapter_versions"):
            connection.execute("ALTER TABLE chapter_versions ADD COLUMN length_json TEXT NOT NULL DEFAULT '{}'")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS volume_audits ("
            "audit_id INTEGER PRIMARY KEY AUTOINCREMENT, volume_id TEXT NOT NULL, "
            "end_commit_id INTEGER NOT NULL, state_revision INTEGER NOT NULL, "
            "end_chapter_seq INTEGER NOT NULL, stale_after INTEGER NOT NULL, "
            "status TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL, "
            "resolved_at TEXT, resolved_by_commit_id INTEGER)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS volume_audits_open_idx "
            "ON volume_audits(volume_id, status, resolved_at, audit_id)"
        )
        for row in connection.execute(
            "SELECT v.commit_id, v.body_content, v.body_sha256 FROM chapter_versions v "
            "WHERE v.length_json = '{}'"
        ).fetchall():
            counts = measurements(row["body_content"])
            legacy = {
                "legacy_unverified": True,
                "metric": LENGTH_METRIC,
                "actual": counts[LENGTH_METRIC],
                "measurements": counts,
                "source_body_sha256": row["body_sha256"],
            }
            connection.execute(
                "UPDATE chapter_versions SET length_json = ? WHERE commit_id = ?",
                (canonical_json(legacy), row["commit_id"]),
            )
        connection.execute(
            "UPDATE commits SET status = 'superseded' "
            "WHERE status = 'active' AND commit_id NOT IN (SELECT current_commit_id FROM chapters)"
        )
        meta_set(connection, "schema_version", SCHEMA_VERSION)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def meta_get(connection: sqlite3.Connection, key: str) -> Any:
    row = connection.execute("SELECT value FROM project_meta WHERE key = ?", (key,)).fetchone()
    require(row is not None, f"database metadata is missing {key}")
    return json.loads(row["value"])


def meta_set(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        "INSERT INTO project_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, canonical_json(value)),
    )


def initialize(project: Path, title: str) -> dict[str, Any]:
    project = project.resolve()
    require(project.is_dir(), f"project directory does not exist: {project}")
    path = database_path(project)
    require(not path.exists(), f"refusing to overwrite existing state database: {path}")
    connection = connect(project, must_exist=False)
    try:
        connection.executescript(SCHEMA)
        meta_set(connection, "schema_version", SCHEMA_VERSION)
        meta_set(connection, "title", title)
        meta_set(connection, "state_revision", 0)
        meta_set(connection, "head_commit", None)
        meta_set(connection, "created_at", utc_now())
        connection.commit()
    except Exception:
        connection.close()
        path.unlink(missing_ok=True)
        raise
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return {"status": "initialized", "database": str(path), "state_revision": 0, "title": title}


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def pointer_parts(pointer: str, label: str) -> list[str]:
    require(pointer.startswith("/"), f"{label} must be a JSON pointer beginning with /")
    require(pointer != "/", f"{label} must identify a field")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def pointer_get(document: Any, pointer: str) -> Any:
    current = document
    for part in pointer_parts(pointer, "path"):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return MISSING
    return current


def pointer_set(document: dict[str, Any], pointer: str, value: Any) -> None:
    parts = pointer_parts(pointer, "path")
    current: Any = document
    for part in parts[:-1]:
        require(isinstance(current, dict), f"path {pointer} crosses a non-object value")
        if part not in current:
            current[part] = {}
        require(isinstance(current[part], dict), f"path {pointer} crosses a non-object value")
        current = current[part]
    require(isinstance(current, dict), f"path {pointer} parent is not an object")
    current[parts[-1]] = copy.deepcopy(value)


def pointer_unset(document: dict[str, Any], pointer: str) -> None:
    parts = pointer_parts(pointer, "path")
    current: Any = document
    for part in parts[:-1]:
        require(isinstance(current, dict) and part in current, f"path {pointer} does not exist")
        current = current[part]
    require(isinstance(current, dict) and parts[-1] in current, f"path {pointer} does not exist")
    del current[parts[-1]]


def validate_expectation(change: dict[str, Any], current: Any, label: str) -> None:
    has_expect = "expect" in change
    expect_missing = change.get("expect_missing", False)
    require(isinstance(expect_missing, bool), f"{label}.expect_missing must be boolean")
    require(has_expect != expect_missing, f"{label} must contain exactly one of expect or expect_missing=true")
    if expect_missing:
        require(current is MISSING, f"{label} conflict: field already exists")
    else:
        require(current is not MISSING, f"{label} conflict: field is missing")
        require(current == change["expect"], f"{label} conflict: expected {change['expect']!r}, found {current!r}")


def style_metrics(text: str) -> dict[str, Any]:
    body = extract_body(text)
    sentence_lengths = [
        len(re.sub(r"\s+", "", sentence))
        for sentence in re.split(r"[。！？!?]+", body)
        if re.sub(r"\s+", "", sentence)
    ]
    paragraph_lengths = [
        len(re.sub(r"\s+", "", paragraph))
        for paragraph in re.split(r"\n\s*\n|\n", body)
        if re.sub(r"\s+", "", paragraph)
    ]

    def distribution(values: list[int]) -> tuple[float, float]:
        if not values:
            return 0.0, 0.0
        mean = statistics.fmean(values)
        deviation = statistics.pstdev(values) if len(values) > 1 else 0.0
        return round(mean, 3), round(deviation / mean if mean else 0.0, 3)

    sentence_mean, sentence_cv = distribution(sentence_lengths)
    paragraph_mean, paragraph_cv = distribution(paragraph_lengths)
    visible = measurements(text)["visible_chars_v1"]
    dialogue = sum(len(match.group(1)) for match in re.finditer(r"[“\"]([^”\"]+)[”\"]", body))
    lint_report = prose_lint_from_text(text)
    return {
        "visible_chars": visible,
        "sentence_count": len(sentence_lengths),
        "sentence_mean": sentence_mean,
        "sentence_cv": sentence_cv,
        "paragraph_count": len(paragraph_lengths),
        "paragraph_mean": paragraph_mean,
        "paragraph_cv": paragraph_cv,
        "dialogue_ratio": round(dialogue / max(visible, 1), 4),
        "formula_findings_per_1000": round(len(lint_report["findings"]) * 1000 / max(visible, 1), 3),
    }


def prose_lint_from_text(text: str) -> dict[str, Any]:
    fd, name = tempfile.mkstemp(suffix=".md")
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return prose_lint(path)
    finally:
        path.unlink(missing_ok=True)


def entity_document(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": row["entity_type"],
        "name": row["name"],
        "aliases": json.loads(row["aliases_json"]),
        "active": bool(row["active"]),
        "state": json.loads(row["state_json"]),
    }


def entity_row(entity_id: str, document: dict[str, Any], chapter_id: str, previous: dict[str, Any] | None) -> dict[str, Any]:
    reject_unknown(document, {"type", "name", "aliases", "active", "state"}, f"entity {entity_id}")
    entity_type = clean_string(document.get("type"), f"entity {entity_id}.type", maximum=40)
    require(entity_type in ENTITY_TYPES, f"entity {entity_id}.type must be one of {sorted(ENTITY_TYPES)}")
    aliases = as_list(document.get("aliases", []), f"entity {entity_id}.aliases")
    aliases = [clean_string(item, f"entity {entity_id}.aliases", maximum=120) for item in aliases]
    state = as_object(document.get("state", {}), f"entity {entity_id}.state")
    active = document.get("active", True)
    require(isinstance(active, bool), f"entity {entity_id}.active must be boolean")
    return {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "name": clean_string(document.get("name"), f"entity {entity_id}.name", maximum=160),
        "aliases_json": canonical_json(aliases),
        "state_json": canonical_json(state),
        "active": int(active),
        "created_chapter": previous["created_chapter"] if previous else chapter_id,
        "updated_chapter": chapter_id,
        "updated_commit": None,
    }


def thread_document(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": row["thread_type"],
        "summary": row["summary"],
        "status": row["status"],
        "importance": row["importance"],
        "introduced_chapter": row["introduced_chapter"],
        "due_chapter": row["due_chapter"],
        "due_volume_id": row["due_volume_id"],
        "last_progress_chapter": row["last_progress_chapter"],
        "state": json.loads(row["state_json"]),
    }


def thread_row(thread_id: str, document: dict[str, Any], chapter_seq: int) -> dict[str, Any]:
    allowed = {
        "type", "summary", "status", "importance", "introduced_chapter", "due_chapter",
        "due_volume_id", "last_progress_chapter", "state",
    }
    reject_unknown(document, allowed, f"thread {thread_id}")
    thread_type = clean_string(document.get("type"), f"thread {thread_id}.type", maximum=40)
    status = clean_string(document.get("status"), f"thread {thread_id}.status", maximum=40)
    importance = clean_string(document.get("importance"), f"thread {thread_id}.importance", maximum=40)
    require(thread_type in THREAD_TYPES, f"thread {thread_id}.type must be one of {sorted(THREAD_TYPES)}")
    require(status in THREAD_STATUSES, f"thread {thread_id}.status must be one of {sorted(THREAD_STATUSES)}")
    require(importance in IMPORTANCE_LEVELS, f"thread {thread_id}.importance must be one of {sorted(IMPORTANCE_LEVELS)}")
    introduced = document.get("introduced_chapter")
    due = document.get("due_chapter")
    last_progress = document.get("last_progress_chapter", chapter_seq)
    require(type(introduced) is int and 0 < introduced <= chapter_seq, f"thread {thread_id}.introduced_chapter must be between 1 and the current chapter")
    require(due is None or type(due) is int and due >= introduced, f"thread {thread_id}.due_chapter is invalid")
    require(
        type(last_progress) is int and introduced <= last_progress <= chapter_seq,
        f"thread {thread_id}.last_progress_chapter must be between introduced_chapter and the current chapter",
    )
    due_volume = document.get("due_volume_id")
    if due_volume is not None:
        due_volume = clean_id(due_volume, f"thread {thread_id}.due_volume_id")
    return {
        "thread_id": thread_id,
        "thread_type": thread_type,
        "summary": clean_string(document.get("summary"), f"thread {thread_id}.summary", maximum=800),
        "status": status,
        "importance": importance,
        "introduced_chapter": introduced,
        "due_chapter": due,
        "due_volume_id": due_volume,
        "last_progress_chapter": last_progress,
        "state_json": canonical_json(as_object(document.get("state", {}), f"thread {thread_id}.state")),
        "updated_commit": None,
    }


def timeline_document(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sequence": row["sequence"],
        "story_time": row["story_time"],
        "mode": row["mode"],
        "fact": row["fact"],
        "location_id": row["location_id"],
        "participant_ids": json.loads(row["participants_json"]),
        "state": json.loads(row["state_json"]),
    }


def normalize_reveal_state(state: dict[str, Any], fact: str, label: str) -> dict[str, Any]:
    normalized = dict(state)
    status = normalized.get("reveal_status")
    if status is None:
        normalized["reveal_status"] = "revealed"
    else:
        status = clean_string(status, f"{label}.reveal_status", maximum=40)
        require(status in REVEAL_STATUSES, f"{label}.reveal_status must be one of {sorted(REVEAL_STATUSES)}")
        normalized["reveal_status"] = status
    knowledge = normalized.get("reader_knowledge")
    if knowledge is None:
        if normalized["reveal_status"] == "hidden":
            normalized["reader_knowledge"] = ""
        else:
            normalized["reader_knowledge"] = fact
    else:
        allow_empty = normalized["reveal_status"] == "hidden"
        normalized["reader_knowledge"] = clean_string(
            knowledge, f"{label}.reader_knowledge", allow_empty=allow_empty, maximum=1200
        )
    reveal_chapter = normalized.get("reveal_chapter")
    if reveal_chapter is not None:
        require(
            type(reveal_chapter) is int and reveal_chapter >= 1,
            f"{label}.reveal_chapter must be a positive integer or null",
        )
    return normalized


def public_timeline_event(row: dict[str, Any]) -> dict[str, Any]:
    document = timeline_document(row)
    state = normalize_reveal_state(as_object(document.get("state", {}), "timeline.state"), document["fact"], "timeline")
    document["state"] = state
    document["id"] = row["event_id"]
    document["reveal_status"] = state["reveal_status"]
    document["reader_knowledge"] = state["reader_knowledge"]
    document["reveal_chapter"] = state.get("reveal_chapter")
    return document


def timeline_row(event_id: str, document: dict[str, Any], chapter_id: str) -> dict[str, Any]:
    reject_unknown(
        document,
        {"sequence", "story_time", "mode", "fact", "location_id", "participant_ids", "state"},
        f"timeline {event_id}",
    )
    sequence = document.get("sequence")
    require(type(sequence) in {int, float} and math.isfinite(sequence), f"timeline {event_id}.sequence must be finite")
    mode = clean_string(document.get("mode"), f"timeline {event_id}.mode", maximum=40)
    require(mode in TIMELINE_MODES, f"timeline {event_id}.mode must be one of {sorted(TIMELINE_MODES)}")
    location = document.get("location_id")
    if location is not None:
        location = clean_id(location, f"timeline {event_id}.location_id")
    participants = as_list(document.get("participant_ids", []), f"timeline {event_id}.participant_ids")
    participants = [clean_id(item, f"timeline {event_id}.participant_ids") for item in participants]
    require(len(participants) == len(set(participants)), f"timeline {event_id}.participant_ids contains duplicates")
    fact = clean_string(document.get("fact"), f"timeline {event_id}.fact", maximum=1200)
    state = normalize_reveal_state(
        as_object(document.get("state", {}), f"timeline {event_id}.state"),
        fact,
        f"timeline {event_id}.state",
    )
    return {
        "event_id": event_id,
        "sequence": float(sequence),
        "story_time": clean_string(document.get("story_time"), f"timeline {event_id}.story_time", maximum=240),
        "mode": mode,
        "fact": fact,
        "location_id": location,
        "participants_json": canonical_json(participants),
        "state_json": canonical_json(state),
        "chapter_id": chapter_id,
        "updated_commit": None,
    }


def volume_row(document: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(document, {"id", "seq", "title", "planned_end_chapter", "status", "state"}, "chapter.volume")
    volume_id = clean_id(document.get("id"), "chapter.volume.id")
    seq = document.get("seq")
    end = document.get("planned_end_chapter")
    require(type(seq) is int and seq > 0, "chapter.volume.seq must be positive")
    require(end is None or type(end) is int and end > 0, "chapter.volume.planned_end_chapter must be positive or null")
    status = clean_string(document.get("status", "active"), "chapter.volume.status", maximum=40)
    require(status in {"planned", "active", "complete"}, "chapter.volume.status is invalid")
    return {
        "volume_id": volume_id,
        "seq": seq,
        "title": clean_string(document.get("title"), "chapter.volume.title", maximum=200),
        "planned_end_chapter": end,
        "status": status,
        "state_json": canonical_json(as_object(document.get("state", {}), "chapter.volume.state")),
        "updated_commit": None,
    }


def style_report_for_path(path: Path, text: str) -> dict[str, Any]:
    result = style_metrics(text)
    result["source"] = str(path)
    return result


def length_gate(
    raw_contract: Any,
    body: str,
    record: str,
    inherited: dict[str, Any] | None,
) -> dict[str, Any]:
    require(measurements(body)[LENGTH_METRIC] > 0, "chapter body must contain prose, not only markup or a title")
    require(measurements(record)[LENGTH_METRIC] > 0, "chapter record must contain meaningful content")
    if raw_contract is None:
        require(
            inherited is not None and not inherited.get("legacy_unverified"),
            "length_contract is required; migrated legacy chapters must declare one on their next revision",
        )
        contract = {
            "metric": inherited.get("metric"),
            "minimum": inherited.get("minimum"),
            "maximum": inherited.get("maximum"),
        }
    else:
        contract = as_object(raw_contract, "length_contract")
        reject_unknown(contract, {"metric", "minimum", "maximum"}, "length_contract")

    metric = clean_string(contract.get("metric", LENGTH_METRIC), "length_contract.metric", maximum=40)
    require(metric in LENGTH_METRICS, f"length_contract.metric must be one of {sorted(LENGTH_METRICS)}")
    minimum = contract.get("minimum")
    maximum = contract.get("maximum")
    require(minimum is not None or maximum is not None, "length_contract needs minimum, maximum, or both")
    require(minimum is None or type(minimum) is int and minimum >= 1, "length_contract.minimum must be a positive integer or null")
    require(maximum is None or type(maximum) is int and maximum >= 1, "length_contract.maximum must be a positive integer or null")
    require(minimum is None or maximum is None or minimum <= maximum, "length_contract.minimum cannot exceed maximum")

    counts = measurements(body)
    actual = counts[metric]
    require(minimum is None or actual >= minimum, f"chapter length is under contract: {actual} {metric}, minimum {minimum}")
    require(maximum is None or actual <= maximum, f"chapter length is over contract: {actual} {metric}, maximum {maximum}")
    return {
        "metric": metric,
        "minimum": minimum,
        "maximum": maximum,
        "actual": actual,
        "status": "pass",
        "measurements": counts,
        "source_body_sha256": sha256_text(body),
        "counted_body_sha256": sha256_text(extract_body(body)),
    }


def mutation_fields(operation: str, path: str | None = None) -> list[str]:
    if operation == "create":
        return ["*"]
    return [path or "*"]


def apply_document_changes(
    *,
    connection: sqlite3.Connection,
    section: list[Any],
    kind: str,
    body: str,
    chapter_id: str,
    chapter_seq: int,
    cache: dict[str, dict[str, Any] | None],
    mutations: list[dict[str, Any]],
) -> None:
    table, key = RESOURCE_TABLES[kind]
    for index, raw_change in enumerate(section):
        label = f"{kind}_changes[{index}]"
        change = as_object(raw_change, label)
        reject_unknown(
            change,
            {"op", "id", "type", "name", "aliases", "state", "path", "value", "expect", "expect_missing", "evidence"},
            label,
        )
        operation = clean_string(change.get("op"), f"{label}.op", maximum=20)
        require(operation in {"create", "set", "unset", "delete", "deactivate", "reactivate"}, f"{label}.op is invalid")
        resource_id = clean_id(change.get("id"), f"{label}.id")
        require_evidence(body, change.get("evidence"), f"{label}.evidence")
        if resource_id not in cache:
            cache[resource_id] = row_dict(connection.execute(f"SELECT * FROM {table} WHERE {key} = ?", (resource_id,)).fetchone())
        before = copy.deepcopy(cache[resource_id])

        if operation == "create":
            require(before is None, f"{label} conflict: {resource_id} already exists")
            if kind == "entity":
                document = {
                    "type": change.get("type"),
                    "name": change.get("name"),
                    "aliases": change.get("aliases", []),
                    "active": True,
                    "state": change.get("state", {}),
                }
                after = entity_row(resource_id, document, chapter_id, None)
            elif kind == "thread":
                document = as_object(change.get("state"), f"{label}.state")
                after = thread_row(resource_id, document, chapter_seq)
            else:
                document = as_object(change.get("state"), f"{label}.state")
                after = timeline_row(resource_id, document, chapter_id)
            fields = ["*"]
        else:
            require(before is not None, f"{label} conflict: {resource_id} does not exist")
            if kind == "entity":
                document = entity_document(before)
            elif kind == "thread":
                document = thread_document(before)
            else:
                document = timeline_document(before)

            if operation == "delete":
                require(kind == "timeline", f"{label}.delete applies only to timeline events")
                require("expect" in change and not change.get("expect_missing", False), f"{label}.delete requires the complete expected event snapshot")
                expected = timeline_document(
                    timeline_row(resource_id, as_object(change["expect"], f"{label}.expect"), chapter_id)
                )
                require(expected == document, f"{label} conflict: expected timeline snapshot does not match")
                after = None
                path = None
            elif operation in {"deactivate", "reactivate"}:
                require(kind == "entity", f"{label}.{operation} applies only to entities")
                expected = operation == "deactivate"
                require(document["active"] is expected, f"{label} conflict: active state does not match")
                document["active"] = not expected
                path = "/active"
            else:
                path = clean_string(change.get("path"), f"{label}.path", maximum=200)
                root = pointer_parts(path, f"{label}.path")[0]
                require(not (kind in {"entity", "thread"} and root == "type"), f"{label} cannot change immutable /type")
                current = pointer_get(document, path)
                validate_expectation(change, current, label)
                if operation == "set":
                    require("value" in change, f"{label}.value is required")
                    require(current is MISSING or current != change["value"], f"{label} is a no-op")
                    pointer_set(document, path, change["value"])
                else:
                    pointer_unset(document, path)

            if operation == "delete":
                pass
            elif kind == "entity":
                after = entity_row(resource_id, document, chapter_id, before)
            elif kind == "thread":
                if operation in {"set", "unset"} and path != "/last_progress_chapter":
                    document["last_progress_chapter"] = chapter_seq
                after = thread_row(resource_id, document, chapter_seq)
            else:
                after = timeline_row(resource_id, document, chapter_id)
            fields = mutation_fields(operation, path)

        cache[resource_id] = after
        mutations.append({"kind": kind, "id": resource_id, "before": before, "after": copy.deepcopy(after), "fields": fields})


def collect_reference_ids(value: Any, key: str | None = None) -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for child_key, child in value.items():
            if child_key in REFERENCE_KEYS:
                if child_key.endswith("_ids"):
                    require(isinstance(child, list), f"reference field {child_key} must be an array")
                    for item in child:
                        yield child_key, clean_id(item, f"reference field {child_key}")
                elif child is not None:
                    yield child_key, clean_id(child, f"reference field {child_key}")
            elif child_key == "relationships":
                require(isinstance(child, (dict, list)), "relationships must be an object or an array")
                if isinstance(child, dict):
                    for target in child:
                        yield "relationship_id", clean_id(target, "relationships target")
                else:
                    for index, relationship in enumerate(child):
                        require(isinstance(relationship, dict), f"relationships[{index}] must be an object")
                        require("target_id" in relationship, f"relationships[{index}].target_id is required")
            yield from collect_reference_ids(child, child_key)
    elif isinstance(value, list):
        for child in value:
            yield from collect_reference_ids(child, key)


def current_resources(
    connection: sqlite3.Connection,
    entity_cache: dict[str, dict[str, Any] | None],
    thread_cache: dict[str, dict[str, Any] | None],
    timeline_cache: dict[str, dict[str, Any] | None],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    entities = {row["entity_id"]: dict(row) for row in connection.execute("SELECT * FROM entities")}
    threads = {row["thread_id"]: dict(row) for row in connection.execute("SELECT * FROM story_threads")}
    timeline = {row["event_id"]: dict(row) for row in connection.execute("SELECT * FROM timeline_events")}
    for key, value in entity_cache.items():
        if value is None:
            entities.pop(key, None)
        else:
            entities[key] = value
    for key, value in thread_cache.items():
        if value is None:
            threads.pop(key, None)
        else:
            threads[key] = value
    for key, value in timeline_cache.items():
        if value is None:
            timeline.pop(key, None)
        else:
            timeline[key] = value
    return entities, threads, timeline


def validate_resource_integrity(
    entities: dict[str, dict[str, Any]],
    threads: dict[str, dict[str, Any]],
    timeline: dict[str, dict[str, Any]],
    chapter_sequences: dict[str, int],
) -> None:
    active = {entity_id: row for entity_id, row in entities.items() if row["active"]}
    for entity_id, row in active.items():
        state = json.loads(row["state_json"])
        for field, target in collect_reference_ids(state):
            require(target in active, f"entity {entity_id}.{field} references missing or inactive entity {target}")
            if field == "location_id":
                require(active[target]["entity_type"] == "location", f"entity {entity_id}.location_id does not reference a location")
            if field in {"faction_id", "faction_ids"}:
                require(active[target]["entity_type"] == "faction", f"entity {entity_id}.{field} does not reference a faction")
            if field in {"holder_id", "leader_id", "member_ids", "character_id", "participant_ids"}:
                require(active[target]["entity_type"] == "character", f"entity {entity_id}.{field} does not reference a character")
        if row["entity_type"] == "item":
            holder = state.get("holder_id")
            location = state.get("location_id")
            if holder and location and holder in active:
                holder_location = json.loads(active[holder]["state_json"]).get("location_id")
                require(
                    holder_location in {None, location},
                    f"item {entity_id} is at {location}, but holder {holder} is at {holder_location}",
                )
    for thread_id, row in threads.items():
        for field, target in collect_reference_ids(json.loads(row["state_json"])):
            require(target in entities, f"thread {thread_id}.{field} references missing entity {target}")
    for event_id, row in timeline.items():
        if row["location_id"] is not None:
            require(row["location_id"] in entities, f"timeline {event_id} references missing location {row['location_id']}")
            require(entities[row["location_id"]]["entity_type"] == "location", f"timeline {event_id}.location_id is not a location")
        for participant in json.loads(row["participants_json"]):
            require(participant in entities, f"timeline {event_id} references missing participant {participant}")
            require(entities[participant]["entity_type"] == "character", f"timeline {event_id} participant {participant} is not a character")

    occupied: dict[tuple[float, str], tuple[str, str]] = {}
    for event_id, row in timeline.items():
        if row["mode"] != "present" or row["location_id"] is None:
            continue
        for participant in json.loads(row["participants_json"]):
            key = (row["sequence"], participant)
            previous = occupied.get(key)
            if previous and previous[0] != row["location_id"]:
                raise StateError(
                    f"timeline conflict: {participant} appears at {previous[0]} ({previous[1]}) and "
                    f"{row['location_id']} ({event_id}) at sequence {row['sequence']}"
                )
            occupied[key] = (row["location_id"], event_id)

    progress: dict[str, tuple[int, float, str]] = {}
    ordered_events = sorted(
        timeline.items(),
        key=lambda item: (chapter_sequences.get(item[1]["chapter_id"], 10**12), item[1]["sequence"], item[0]),
    )
    for event_id, row in ordered_events:
        if row["mode"] != "present":
            continue
        chapter_seq = chapter_sequences.get(row["chapter_id"])
        require(chapter_seq is not None, f"timeline {event_id} belongs to unknown chapter {row['chapter_id']}")
        for participant in json.loads(row["participants_json"]):
            previous = progress.get(participant)
            if previous and chapter_seq > previous[0] and row["sequence"] < previous[1]:
                raise StateError(
                    f"timeline regression: {participant} moves from sequence {previous[1]} ({previous[2]}) "
                    f"back to {row['sequence']} ({event_id}) in present mode; mark a flashback explicitly"
                )
            if previous is None or (chapter_seq, row["sequence"]) >= (previous[0], previous[1]):
                progress[participant] = (chapter_seq, row["sequence"], event_id)


def resource_exists(
    kind: str,
    resource_id: str,
    entities: dict[str, dict[str, Any]],
    threads: dict[str, dict[str, Any]],
    timeline: dict[str, dict[str, Any]],
) -> bool:
    if kind == "entity":
        return resource_id in entities
    if kind == "thread":
        return resource_id in threads
    if kind == "timeline":
        return resource_id in timeline
    return False


def parse_references(
    raw_references: list[Any],
    body: str,
    entities: dict[str, dict[str, Any]],
    threads: dict[str, dict[str, Any]],
    timeline: dict[str, dict[str, Any]],
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_references):
        label = f"references[{index}]"
        reference = as_object(raw, label)
        reject_unknown(reference, {"kind", "id", "fields", "reason", "evidence"}, label)
        kind = clean_string(reference.get("kind"), f"{label}.kind", maximum=30)
        require(kind in {"entity", "thread", "timeline", "chapter"}, f"{label}.kind is invalid")
        resource_id = clean_id(reference.get("id"), f"{label}.id")
        if kind == "chapter":
            require(connection.execute("SELECT 1 FROM chapters WHERE chapter_id = ?", (resource_id,)).fetchone() is not None, f"{label} references unknown chapter {resource_id}")
        else:
            require(resource_exists(kind, resource_id, entities, threads, timeline), f"{label} references unknown {kind} {resource_id}")
        fields = as_list(reference.get("fields", ["*"]), f"{label}.fields")
        fields = [clean_string(field, f"{label}.fields", maximum=200) for field in fields]
        require(fields, f"{label}.fields must not be empty")
        reason = clean_string(reference.get("reason"), f"{label}.reason", maximum=500)
        evidence = require_evidence(body, reference.get("evidence"), f"{label}.evidence")
        references.append({"kind": kind, "id": resource_id, "fields": fields, "reason": reason, "evidence": evidence})
    return references


def parse_arc_beats(
    raw_beats: list[Any],
    body: str,
    entities: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    beats: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_beats):
        label = f"arc_beats[{index}]"
        beat = as_object(raw, label)
        reject_unknown(beat, {"character_id", "dimension", "beat", "before", "after", "evidence"}, label)
        character_id = clean_id(beat.get("character_id"), f"{label}.character_id")
        require(character_id in entities and entities[character_id]["entity_type"] == "character", f"{label} character does not exist")
        before = beat.get("before")
        after = beat.get("after")
        require(before is None or isinstance(before, str), f"{label}.before must be string or null")
        require(after is None or isinstance(after, str), f"{label}.after must be string or null")
        beats.append(
            {
                "character_id": character_id,
                "dimension": clean_string(beat.get("dimension"), f"{label}.dimension", maximum=100),
                "beat": clean_string(beat.get("beat"), f"{label}.beat", maximum=500),
                "before": before.strip() if isinstance(before, str) else None,
                "after": after.strip() if isinstance(after, str) else None,
                "evidence": require_evidence(body, beat.get("evidence"), f"{label}.evidence"),
            }
        )
    return beats


def field_overlap(left: str, right: str) -> bool:
    if "*" in {left, right}:
        return True
    left = left.rstrip("/")
    right = right.rstrip("/")
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def impact_chapters(connection: sqlite3.Connection, chapter_id: str) -> list[dict[str, Any]]:
    source = connection.execute("SELECT seq FROM chapters WHERE chapter_id = ?", (chapter_id,)).fetchone()
    require(source is not None, f"unknown chapter {chapter_id}")
    rows = connection.execute(
        "SELECT d.dependent_chapter, d.prerequisite_id, d.relation, d.detail, ch.seq "
        "FROM dependencies d JOIN commits c ON c.commit_id = d.commit_id "
        "JOIN chapters ch ON ch.chapter_id = d.dependent_chapter AND ch.current_commit_id = d.commit_id "
        "WHERE d.prerequisite_kind = 'chapter' AND c.status = 'active'"
    ).fetchall()
    outgoing: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        outgoing.setdefault(row["prerequisite_id"], []).append(row)
    result: dict[str, dict[str, Any]] = {}
    queue = [chapter_id]
    while queue:
        prerequisite = queue.pop(0)
        for row in outgoing.get(prerequisite, []):
            dependent = row["dependent_chapter"]
            if row["seq"] <= source["seq"] or dependent in result:
                continue
            result[dependent] = {
                "chapter_id": dependent,
                "seq": row["seq"],
                "via": prerequisite,
                "relation": row["relation"],
                "detail": row["detail"],
            }
            queue.append(dependent)
    return sorted(result.values(), key=lambda item: (item["seq"], item["chapter_id"]))


def changed_resources_for_chapter(connection: sqlite3.Connection, chapter_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT DISTINCT t.resource_kind, t.resource_id, t.field "
        "FROM touches t JOIN commits c ON c.commit_id = t.commit_id "
        "JOIN chapters ch ON ch.chapter_id = t.chapter_id AND ch.current_commit_id = t.commit_id "
        "WHERE t.chapter_id = ? AND t.access = 'write' AND c.status = 'active' "
        "ORDER BY t.resource_kind, t.resource_id, t.field",
        (chapter_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def touched_resources_for_chapter(connection: sqlite3.Connection, chapter_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT DISTINCT t.resource_kind, t.resource_id, t.field, t.access "
        "FROM touches t JOIN commits c ON c.commit_id = t.commit_id "
        "JOIN chapters ch ON ch.chapter_id = t.chapter_id AND ch.current_commit_id = t.commit_id "
        "WHERE t.chapter_id = ? AND c.status = 'active' "
        "ORDER BY t.resource_kind, t.resource_id, t.field, t.access",
        (chapter_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def build_plan(
    connection: sqlite3.Connection,
    project: Path,
    delta: dict[str, Any],
    body_path: Path,
    record_path: Path,
    body: str,
    record: str,
) -> dict[str, Any]:
    reject_unknown(
        delta,
        {
            "schema_version", "expected_revision", "length_contract", "chapter", "entity_changes",
            "thread_changes", "timeline_changes", "references", "arc_beats",
        },
        "delta",
    )
    require(delta.get("schema_version") == DELTA_VERSION, f"delta.schema_version must be {DELTA_VERSION}")
    expected_revision = delta.get("expected_revision")
    require(type(expected_revision) is int and expected_revision >= 0, "delta.expected_revision must be non-negative")
    current_revision = meta_get(connection, "state_revision")
    require(expected_revision == current_revision, f"stale delta: expected revision {expected_revision}, current revision is {current_revision}")

    chapter = as_object(delta.get("chapter"), "delta.chapter")
    reject_unknown(chapter, {"id", "seq", "title", "mode", "summary", "volume", "continuity_changed"}, "delta.chapter")
    chapter_id = clean_id(chapter.get("id"), "delta.chapter.id")
    seq = chapter.get("seq")
    require(type(seq) is int and seq > 0, "delta.chapter.seq must be positive")
    mode = clean_string(chapter.get("mode"), "delta.chapter.mode", maximum=20)
    require(mode in CHAPTER_MODES, f"delta.chapter.mode must be one of {sorted(CHAPTER_MODES)}")
    title = clean_string(chapter.get("title"), "delta.chapter.title", maximum=300)
    summary = clean_string(chapter.get("summary"), "delta.chapter.summary", maximum=1200)
    continuity_changed = chapter.get("continuity_changed", mode != "revalidate")
    require(isinstance(continuity_changed, bool), "delta.chapter.continuity_changed must be boolean")

    existing_chapter = row_dict(connection.execute("SELECT * FROM chapters WHERE chapter_id = ?", (chapter_id,)).fetchone())
    seq_owner = connection.execute("SELECT chapter_id FROM chapters WHERE seq = ?", (seq,)).fetchone()
    if mode == "append":
        require(existing_chapter is None, f"append chapter already exists: {chapter_id}")
        if seq_owner is not None:
            raise StateError(f"chapter sequence {seq} already belongs to {seq_owner['chapter_id']}")
        max_seq = connection.execute("SELECT MAX(seq) AS value FROM chapters").fetchone()["value"]
        require(max_seq is None or seq > max_seq, f"append chapter sequence must be greater than current maximum {max_seq}")
        dirty = connection.execute("SELECT chapter_id FROM chapters WHERE needs_review = 1 ORDER BY seq LIMIT 1").fetchone()
        if dirty is not None:
            raise StateError(
                f"cannot append while {dirty['chapter_id']} still needs review after a past-chapter revision"
            )
    else:
        require(existing_chapter is not None, f"{mode} requires an existing chapter: {chapter_id}")
        require(existing_chapter["seq"] == seq, f"chapter {chapter_id} sequence cannot change")
        require(seq_owner is None or seq_owner["chapter_id"] == chapter_id, f"chapter sequence {seq} is already in use")

    inherited_length: dict[str, Any] | None = None
    if existing_chapter is not None:
        inherited_row = connection.execute(
            "SELECT length_json FROM chapter_versions WHERE commit_id = ?",
            (existing_chapter["current_commit_id"],),
        ).fetchone()
        if inherited_row is not None:
            inherited_length = as_object(json.loads(inherited_row["length_json"]), "accepted length record")
    length = length_gate(delta.get("length_contract"), body, record, inherited_length)

    raw_volume = chapter.get("volume")
    volume_mutation: dict[str, Any] | None = None
    volume_id: str | None = None
    if raw_volume is not None:
        desired_volume = volume_row(as_object(raw_volume, "delta.chapter.volume"))
        volume_id = desired_volume["volume_id"]
        before_volume = row_dict(connection.execute("SELECT * FROM volumes WHERE volume_id = ?", (volume_id,)).fetchone())
        if before_volume is None:
            volume_mutation = {"kind": "volume", "id": volume_id, "before": None, "after": desired_volume, "fields": ["*"]}
        else:
            require(before_volume["seq"] == desired_volume["seq"], f"chapter.volume.seq cannot change for {volume_id}")
            compared = ("title", "planned_end_chapter", "status", "state_json")
            changed_fields = [f"/{key}" for key in compared if before_volume[key] != desired_volume[key]]
            if changed_fields:
                volume_mutation = {
                    "kind": "volume",
                    "id": volume_id,
                    "before": before_volume,
                    "after": desired_volume,
                    "fields": changed_fields,
                }
    elif existing_chapter:
        volume_id = existing_chapter["volume_id"]

    if mode == "append":
        target_volume_seq = desired_volume["seq"] if raw_volume is not None else None
        blocker = connection.execute(
            "SELECT va.audit_id, va.volume_id, v.seq FROM volume_audits va "
            "JOIN volumes v ON v.volume_id = va.volume_id "
            "WHERE va.status = 'block' AND va.resolved_at IS NULL "
            "ORDER BY v.seq, va.audit_id LIMIT 1"
        ).fetchone()
        if blocker is not None and (
            volume_id != blocker["volume_id"]
            and (target_volume_seq is None or target_volume_seq > blocker["seq"])
        ):
            raise StateError(
                f"cannot open a later volume while audit {blocker['audit_id']} for "
                f"{blocker['volume_id']} has unresolved blockers"
            )
        if target_volume_seq is not None:
            unaudited = connection.execute(
                "SELECT v.volume_id FROM volumes v WHERE v.seq < ? "
                "AND (v.status = 'complete' OR v.planned_end_chapter IS NOT NULL AND EXISTS ("
                "SELECT 1 FROM chapters c WHERE c.volume_id = v.volume_id AND c.seq >= v.planned_end_chapter)) "
                "AND NOT EXISTS (SELECT 1 FROM volume_audits va WHERE va.volume_id = v.volume_id) "
                "ORDER BY v.seq LIMIT 1",
                (target_volume_seq,),
            ).fetchone()
            if unaudited is not None:
                raise StateError(
                    f"cannot open a later volume before {unaudited['volume_id']} has a persisted volume audit"
                )

    entity_cache: dict[str, dict[str, Any] | None] = {}
    thread_cache: dict[str, dict[str, Any] | None] = {}
    timeline_cache: dict[str, dict[str, Any] | None] = {}
    mutations: list[dict[str, Any]] = []
    apply_document_changes(
        connection=connection,
        section=as_list(delta.get("entity_changes", []), "entity_changes"),
        kind="entity",
        body=body,
        chapter_id=chapter_id,
        chapter_seq=seq,
        cache=entity_cache,
        mutations=mutations,
    )
    apply_document_changes(
        connection=connection,
        section=as_list(delta.get("thread_changes", []), "thread_changes"),
        kind="thread",
        body=body,
        chapter_id=chapter_id,
        chapter_seq=seq,
        cache=thread_cache,
        mutations=mutations,
    )
    apply_document_changes(
        connection=connection,
        section=as_list(delta.get("timeline_changes", []), "timeline_changes"),
        kind="timeline",
        body=body,
        chapter_id=chapter_id,
        chapter_seq=seq,
        cache=timeline_cache,
        mutations=mutations,
    )
    require(continuity_changed or not mutations, "continuity_changed=false cannot include state mutations")
    require(mode != "revalidate" or not mutations, "revalidate cannot include state mutations")
    require(
        continuity_changed or volume_mutation is None,
        "continuity_changed=false cannot change volume metadata",
    )
    require(mode != "revalidate" or volume_mutation is None, "revalidate cannot change volume metadata")

    entities, threads, timeline = current_resources(connection, entity_cache, thread_cache, timeline_cache)
    chapter_sequences = {
        row["chapter_id"]: row["seq"] for row in connection.execute("SELECT chapter_id, seq FROM chapters")
    }
    chapter_sequences[chapter_id] = seq
    validate_resource_integrity(entities, threads, timeline, chapter_sequences)
    references = parse_references(
        as_list(delta.get("references", []), "references"), body, entities, threads, timeline, connection
    )
    beats = parse_arc_beats(as_list(delta.get("arc_beats", []), "arc_beats"), body, entities)

    style = style_report_for_path(body_path, body)
    after_chapter = {
        "chapter_id": chapter_id,
        "seq": seq,
        "title": title,
        "volume_id": volume_id,
        "current_commit_id": None,
        "body_path": project_path(project, body_path),
        "record_path": project_path(project, record_path),
        "body_sha256": sha256_text(body),
        "record_sha256": sha256_text(record),
        "style_json": canonical_json(style),
        "summary": summary,
        "needs_review": 0,
        "accepted_at": utc_now(),
    }
    impacted = impact_chapters(connection, chapter_id) if existing_chapter and continuity_changed else []
    touches: list[dict[str, Any]] = []
    for mutation in mutations:
        for field in mutation["fields"]:
            touches.append({"kind": mutation["kind"], "id": mutation["id"], "field": field, "access": "write"})
    if volume_mutation:
        touches.append({"kind": "volume", "id": volume_id, "field": "*", "access": "write"})
    for reference in references:
        if reference["kind"] != "chapter":
            for field in reference["fields"]:
                touches.append({"kind": reference["kind"], "id": reference["id"], "field": field, "access": "read"})

    return {
        "expected_revision": expected_revision,
        "chapter": chapter,
        "chapter_id": chapter_id,
        "chapter_seq": seq,
        "mode": mode,
        "continuity_changed": continuity_changed,
        "existing_chapter": existing_chapter,
        "after_chapter": after_chapter,
        "volume_mutation": volume_mutation,
        "mutations": mutations,
        "references": references,
        "arc_beats": beats,
        "touches": touches,
        "impacted": impacted,
        "style": style,
        "length": length,
        "body": body,
        "record": record,
    }


def scrub_plan(plan: dict[str, Any]) -> dict[str, Any]:
    def public_snapshot(kind: str, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        if kind == "entity":
            return entity_document(row)
        if kind == "thread":
            return thread_document(row)
        if kind == "timeline":
            return timeline_document(row)
        return row

    return {
        "status": "pass",
        "state_revision": plan["expected_revision"],
        "chapter": {"id": plan["chapter_id"], "seq": plan["chapter_seq"], "mode": plan["mode"]},
        "changes": [
            {
                "kind": item["kind"],
                "id": item["id"],
                "fields": item["fields"],
                "before": public_snapshot(item["kind"], item["before"]),
                "after": public_snapshot(item["kind"], item["after"]),
            }
            for item in plan["mutations"]
        ],
        "volume_change": (
            {
                "id": plan["volume_mutation"]["id"],
                "fields": plan["volume_mutation"]["fields"],
                "before": plan["volume_mutation"]["before"],
                "after": plan["volume_mutation"]["after"],
            }
            if plan["volume_mutation"]
            else None
        ),
        "references": len(plan["references"]),
        "arc_beats": len(plan["arc_beats"]),
        "impacted_chapters": plan["impacted"],
        "style": plan["style"],
        "length": plan["length"],
        "body_sha256": plan["after_chapter"]["body_sha256"],
        "record_sha256": plan["after_chapter"]["record_sha256"],
    }


def upsert_row(connection: sqlite3.Connection, table: str, primary_key: str, row: dict[str, Any]) -> None:
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column != primary_key)
    connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({primary_key}) DO UPDATE SET {updates}",
        tuple(row[column] for column in columns),
    )


def latest_writer(
    connection: sqlite3.Connection,
    resource_kind: str,
    resource_id: str,
    field: str,
    dependent_chapter: str,
    dependent_seq: int,
) -> sqlite3.Row | None:
    rows = connection.execute(
        "SELECT t.chapter_id, t.field, t.commit_id FROM touches t "
        "JOIN commits c ON c.commit_id = t.commit_id "
        "JOIN chapters ch ON ch.chapter_id = t.chapter_id AND ch.current_commit_id = t.commit_id "
        "WHERE t.resource_kind = ? AND t.resource_id = ? AND t.access = 'write' "
        "AND c.status = 'active' AND t.chapter_id != ? AND ch.seq < ? ORDER BY ch.seq DESC, t.commit_id DESC",
        (resource_kind, resource_id, dependent_chapter, dependent_seq),
    ).fetchall()
    return next((row for row in rows if field_overlap(field, row["field"])), None)


def insert_dependencies(connection: sqlite3.Connection, commit_id: int, plan: dict[str, Any]) -> int:
    chapter_id = plan["chapter_id"]
    dependencies: set[tuple[str, str, str, str]] = set()
    for touch in plan["touches"]:
        dependencies.add((touch["kind"], touch["id"], "uses-resource", touch["field"]))
        writer = latest_writer(
            connection,
            touch["kind"],
            touch["id"],
            touch["field"],
            chapter_id,
            plan["chapter_seq"],
        )
        if writer:
            dependencies.add(("chapter", writer["chapter_id"], "continues-state", f"{touch['kind']}:{touch['id']}{touch['field']}"))
    for reference in plan["references"]:
        if reference["kind"] == "chapter":
            dependencies.add(("chapter", reference["id"], "explicit", reference["reason"]))
    for prerequisite_kind, prerequisite_id, relation, detail in sorted(dependencies):
        connection.execute(
            "INSERT INTO dependencies(commit_id, dependent_chapter, prerequisite_kind, prerequisite_id, relation, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (commit_id, chapter_id, prerequisite_kind, prerequisite_id, relation, detail),
        )
    return len(dependencies)


def commit_plan(connection: sqlite3.Connection, delta: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    revision = plan["expected_revision"] + 1
    parent = meta_get(connection, "head_commit")
    cursor = connection.execute(
        "INSERT INTO commits(parent_id, state_revision, chapter_id, chapter_seq, mode, continuity_changed, created_at, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            parent,
            revision,
            plan["chapter_id"],
            plan["chapter_seq"],
            plan["mode"],
            int(plan["continuity_changed"]),
            utc_now(),
            canonical_json(delta),
        ),
    )
    commit_id = int(cursor.lastrowid)
    superseded_commit_id = None
    if plan["existing_chapter"] is not None:
        superseded_commit_id = plan["existing_chapter"]["current_commit_id"]
        connection.execute(
            "UPDATE commits SET status = 'superseded' WHERE commit_id = ?",
            (superseded_commit_id,),
        )

    all_mutations = ([plan["volume_mutation"]] if plan["volume_mutation"] else []) + plan["mutations"]
    for mutation in all_mutations:
        table, primary_key = RESOURCE_TABLES[mutation["kind"]]
        after = copy.deepcopy(mutation["after"])
        if after is None:
            connection.execute(f"DELETE FROM {table} WHERE {primary_key} = ?", (mutation["id"],))
        else:
            after["updated_commit"] = commit_id
            upsert_row(connection, table, primary_key, after)
        connection.execute(
            "INSERT INTO resource_changes(commit_id, resource_kind, resource_id, before_json, after_json, fields_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                commit_id,
                mutation["kind"],
                mutation["id"],
                canonical_json(mutation["before"]) if mutation["before"] is not None else None,
                canonical_json(after) if after is not None else None,
                canonical_json(mutation["fields"]),
            ),
        )

    after_chapter = copy.deepcopy(plan["after_chapter"])
    after_chapter["current_commit_id"] = commit_id
    upsert_row(connection, "chapters", "chapter_id", after_chapter)
    connection.execute(
        "INSERT INTO chapter_versions(commit_id, chapter_id, body_content, record_content, body_sha256, record_sha256, body_path, record_path, style_json, length_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            commit_id,
            plan["chapter_id"],
            plan["body"],
            plan["record"],
            after_chapter["body_sha256"],
            after_chapter["record_sha256"],
            after_chapter["body_path"],
            after_chapter["record_path"],
            after_chapter["style_json"],
            canonical_json(plan["length"]),
        ),
    )
    for touch in plan["touches"]:
        connection.execute(
            "INSERT INTO touches(commit_id, chapter_id, resource_kind, resource_id, field, access) VALUES (?, ?, ?, ?, ?, ?)",
            (commit_id, plan["chapter_id"], touch["kind"], touch["id"], touch["field"], touch["access"]),
        )
    dependency_count = insert_dependencies(connection, commit_id, plan)
    for beat in plan["arc_beats"]:
        connection.execute(
            "INSERT INTO arc_beats(commit_id, chapter_id, character_id, dimension, beat, before_text, after_text, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                commit_id,
                plan["chapter_id"],
                beat["character_id"],
                beat["dimension"],
                beat["beat"],
                beat["before"],
                beat["after"],
                beat["evidence"],
            ),
        )

    review_before: dict[str, int] = {}
    for impacted in plan["impacted"]:
        row = connection.execute("SELECT needs_review FROM chapters WHERE chapter_id = ?", (impacted["chapter_id"],)).fetchone()
        if row is not None:
            review_before[impacted["chapter_id"]] = row["needs_review"]
            connection.execute("UPDATE chapters SET needs_review = 1 WHERE chapter_id = ?", (impacted["chapter_id"],))

    rollback = {
        "chapter_before": plan["existing_chapter"],
        "review_before": review_before,
        "superseded_commit_id": superseded_commit_id,
    }
    connection.execute("UPDATE commits SET rollback_json = ? WHERE commit_id = ?", (canonical_json(rollback), commit_id))
    meta_set(connection, "state_revision", revision)
    meta_set(connection, "head_commit", commit_id)
    return {
        "status": "committed",
        "commit_id": commit_id,
        "state_revision": revision,
        "chapter_id": plan["chapter_id"],
        "changes": len(all_mutations),
        "dependencies": dependency_count,
        "impacted_chapters": [item["chapter_id"] for item in plan["impacted"]],
        "body_sha256": after_chapter["body_sha256"],
        "record_sha256": after_chapter["record_sha256"],
        "length": plan["length"],
    }


def check_or_commit(project: Path, body_path: Path, record_path: Path, delta_path: Path, *, commit: bool) -> dict[str, Any]:
    project = project.resolve()
    connection = connect(project)
    try:
        connection.execute("BEGIN IMMEDIATE" if commit else "BEGIN")
        delta = read_json(delta_path, "delta")
        body = read_utf8(body_path, "chapter body")
        record = read_utf8(record_path, "chapter record")
        plan = build_plan(connection, project, delta, body_path, record_path, body, record)
        if not commit:
            result = scrub_plan(plan)
            connection.rollback()
            return result
        result = commit_plan(connection, delta, plan)
        volume_id = plan["after_chapter"]["volume_id"]
        if volume_id is not None:
            volume = connection.execute(
                "SELECT planned_end_chapter, status FROM volumes WHERE volume_id = ?", (volume_id,)
            ).fetchone()
            audit_due = volume is not None and (
                volume["status"] == "complete"
                or volume["planned_end_chapter"] is not None
                and plan["chapter_seq"] >= volume["planned_end_chapter"]
                or connection.execute(
                    "SELECT 1 FROM volume_audits WHERE volume_id = ? LIMIT 1", (volume_id,)
                ).fetchone() is not None
            )
            if audit_due:
                audit = persist_volume_audit(
                    connection,
                    compute_volume_audit(connection, volume_id, 20, result["commit_id"]),
                    20,
                    result["commit_id"],
                    result["state_revision"],
                )
                result["volume_audit"] = audit
                if audit["status"] == "block":
                    result["status"] = "committed_with_audit_blockers"
                    result["note"] = "the chapter commit succeeded; resolve the audit blockers before opening a later volume"
        connection.commit()
        views = render_derived_views(project)
        result["derived_views"] = [str(path) for path in views]
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def restore_resource(connection: sqlite3.Connection, kind: str, resource_id: str, snapshot: str | None) -> None:
    table, primary_key = RESOURCE_TABLES[kind]
    if snapshot is None:
        connection.execute(f"DELETE FROM {table} WHERE {primary_key} = ?", (resource_id,))
    else:
        row = as_object(json.loads(snapshot), f"rollback {kind}:{resource_id}")
        upsert_row(connection, table, primary_key, row)


def rollback_head(project: Path, requested_commit: int | None) -> dict[str, Any]:
    connection = connect(project.resolve())
    try:
        connection.execute("BEGIN IMMEDIATE")
        head = meta_get(connection, "head_commit")
        require(head is not None, "there is no active commit to roll back")
        if requested_commit is not None:
            require(requested_commit == head, f"only the head commit can be rolled back safely; current head is {head}")
        commit = connection.execute("SELECT * FROM commits WHERE commit_id = ? AND status = 'active'", (head,)).fetchone()
        require(commit is not None, f"head commit {head} is not active")
        changes = connection.execute(
            "SELECT * FROM resource_changes WHERE commit_id = ? ORDER BY change_id DESC", (head,)
        ).fetchall()
        for change in changes:
            restore_resource(connection, change["resource_kind"], change["resource_id"], change["before_json"])

        rollback = as_object(json.loads(commit["rollback_json"]), "rollback data")
        chapter_before = rollback.get("chapter_before")
        if chapter_before is None:
            connection.execute("DELETE FROM chapters WHERE chapter_id = ?", (commit["chapter_id"],))
        else:
            upsert_row(connection, "chapters", "chapter_id", as_object(chapter_before, "chapter_before"))
        for chapter_id, value in as_object(rollback.get("review_before", {}), "review_before").items():
            connection.execute("UPDATE chapters SET needs_review = ? WHERE chapter_id = ?", (int(value), chapter_id))

        connection.execute("DELETE FROM volume_audits WHERE end_commit_id = ?", (head,))
        connection.execute(
            "UPDATE volume_audits SET resolved_at = NULL, resolved_by_commit_id = NULL "
            "WHERE resolved_by_commit_id = ?",
            (head,),
        )
        connection.execute("UPDATE commits SET status = 'rolled_back' WHERE commit_id = ?", (head,))
        superseded_commit_id = rollback.get("superseded_commit_id")
        if superseded_commit_id is not None:
            connection.execute(
                "UPDATE commits SET status = 'active' WHERE commit_id = ?",
                (superseded_commit_id,),
            )
        revision = int(meta_get(connection, "state_revision")) + 1
        meta_set(connection, "state_revision", revision)
        meta_set(connection, "head_commit", commit["parent_id"])
        connection.commit()
        result = {
            "status": "rolled_back",
            "commit_id": head,
            "chapter_id": commit["chapter_id"],
            "head_commit": commit["parent_id"],
            "state_revision": revision,
            "note": "accepted database state was restored; run checkout explicitly to overwrite working Markdown files",
        }
        if any(path.is_file() for path in derived_view_paths(project).values()):
            result["derived_views"] = [str(path) for path in render_derived_views(project)]
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def backup_working_file(project: Path, path: Path, expected: str) -> str | None:
    if not path.exists():
        return None
    current = read_utf8(path, "working file")
    if current == expected:
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    try:
        relative = path.resolve().relative_to(project.resolve())
    except ValueError:
        relative = Path(path.name)
    backup = project / "追踪" / "回滚存档" / stamp / relative
    backup.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(backup, current)
    return str(backup)


def checkout_chapter(project: Path, chapter_id: str) -> dict[str, Any]:
    project = project.resolve()
    connection = connect(project)
    try:
        row = connection.execute(
            "SELECT ch.chapter_id, ch.current_commit_id, v.* FROM chapters ch "
            "JOIN chapter_versions v ON v.commit_id = ch.current_commit_id WHERE ch.chapter_id = ?",
            (chapter_id,),
        ).fetchone()
        require(row is not None, f"no accepted chapter snapshot for {chapter_id}")
        body_path = resolve_project_path(project, row["body_path"])
        record_path = resolve_project_path(project, row["record_path"])
        backups = [
            item
            for item in (
                backup_working_file(project, body_path, row["body_content"]),
                backup_working_file(project, record_path, row["record_content"]),
            )
            if item
        ]
        atomic_write(body_path, row["body_content"])
        atomic_write(record_path, row["record_content"])
        return {
            "status": "checked_out",
            "chapter_id": chapter_id,
            "commit_id": row["current_commit_id"],
            "body_path": str(body_path),
            "record_path": str(record_path),
            "backups": backups,
        }
    finally:
        connection.close()


def verify_project(project: Path) -> dict[str, Any]:
    project = project.resolve()
    connection = connect(project)
    findings: list[dict[str, Any]] = []
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            findings.append({"severity": "block", "code": "sqlite-integrity", "detail": integrity})
        schema = meta_get(connection, "schema_version")
        if schema != SCHEMA_VERSION:
            findings.append({"severity": "block", "code": "schema-version", "detail": f"expected {SCHEMA_VERSION}, found {schema}"})
        for row in connection.execute("SELECT * FROM chapters ORDER BY seq"):
            if row["needs_review"]:
                findings.append({"severity": "block", "code": "needs-review", "chapter_id": row["chapter_id"]})
            version = connection.execute(
                "SELECT v.length_json, c.status FROM chapter_versions v "
                "JOIN commits c ON c.commit_id = v.commit_id WHERE v.commit_id = ?",
                (row["current_commit_id"],),
            ).fetchone()
            if version is None or version["status"] != "active":
                findings.append({"severity": "block", "code": "invalid-current-version", "chapter_id": row["chapter_id"]})
            else:
                length = as_object(json.loads(version["length_json"]), "stored length record")
                if length.get("legacy_unverified"):
                    findings.append({"severity": "warn", "code": "legacy-length-unverified", "chapter_id": row["chapter_id"]})
                elif length.get("status") != "pass" or length.get("source_body_sha256") != row["body_sha256"]:
                    findings.append({"severity": "block", "code": "invalid-length-record", "chapter_id": row["chapter_id"]})
            for field, hash_field in (("body_path", "body_sha256"), ("record_path", "record_sha256")):
                path = resolve_project_path(project, row[field])
                if not path.is_file():
                    findings.append({"severity": "block", "code": "missing-working-file", "chapter_id": row["chapter_id"], "path": str(path)})
                    continue
                try:
                    digest = sha256_text(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError) as exc:
                    findings.append({"severity": "block", "code": "unreadable-working-file", "chapter_id": row["chapter_id"], "detail": str(exc)})
                    continue
                if digest != row[hash_field]:
                    findings.append({"severity": "block", "code": "working-copy-drift", "chapter_id": row["chapter_id"], "path": str(path)})
        dangling = connection.execute(
            "SELECT d.dependent_chapter FROM dependencies d LEFT JOIN chapters c ON c.chapter_id = d.dependent_chapter "
            "JOIN commits cm ON cm.commit_id = d.commit_id "
            "WHERE cm.status = 'active' AND (c.chapter_id IS NULL OR c.current_commit_id != d.commit_id) LIMIT 1"
        ).fetchone()
        if dangling:
            findings.append({"severity": "block", "code": "dangling-dependency", "chapter_id": dangling["dependent_chapter"]})
        for audit in connection.execute(
            "SELECT audit_id, volume_id FROM volume_audits "
            "WHERE status = 'block' AND resolved_at IS NULL ORDER BY audit_id"
        ):
            findings.append(
                {"severity": "block", "code": "volume-audit-blocker", "audit_id": audit["audit_id"], "volume_id": audit["volume_id"]}
            )
        findings.extend(derived_view_findings(project, connection))
        status = "block" if any(item["severity"] == "block" for item in findings) else "warn" if findings else "pass"
        return {
            "status": status,
            "database": str(database_path(project)),
            "state_revision": meta_get(connection, "state_revision"),
            "head_commit": meta_get(connection, "head_commit"),
            "accepted_chapters": connection.execute("SELECT COUNT(*) FROM chapters").fetchone()[0],
            "findings": findings,
        }
    finally:
        connection.close()


def batch_status(project: Path, start: int, end: int) -> dict[str, Any]:
    require(start > 0 and end >= start, "batch range must satisfy 1 <= start <= end")
    verification = verify_project(project)
    connection = connect(project.resolve())
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT chapter_id, seq, title, current_commit_id, needs_review "
                "FROM chapters WHERE seq BETWEEN ? AND ? ORDER BY seq",
                (start, end),
            )
        ]
        accepted = {int(row["seq"]): row for row in rows}
        completed: list[dict[str, Any]] = []
        next_seq = start
        while next_seq <= end and next_seq in accepted:
            completed.append(accepted[next_seq])
            next_seq += 1
        findings = list(verification["findings"])
        non_prefix = [seq for seq in sorted(accepted) if seq >= next_seq]
        if non_prefix:
            findings.append(
                {
                    "severity": "block",
                    "code": "batch-range-gap",
                    "detail": f"accepted chapters exist after missing chapter {next_seq}",
                    "chapter_sequences": non_prefix,
                }
            )
        max_seq = connection.execute("SELECT MAX(seq) FROM chapters").fetchone()[0]
        global_next = 1 if max_seq is None else int(max_seq) + 1
        if next_seq <= end and next_seq != global_next:
            findings.append(
                {
                    "severity": "block",
                    "code": "batch-not-at-head",
                    "detail": f"batch expects chapter {next_seq}, but the project append head is {global_next}",
                }
            )
        blocking = [item for item in findings if item.get("severity") == "block"]
        if blocking:
            status = "block"
        elif next_seq > end:
            status = "complete"
        elif completed:
            status = "in_progress"
        else:
            status = "ready"
        return {
            "status": status,
            "database": str(database_path(project.resolve())),
            "range": {"start": start, "end": end},
            "state_revision": meta_get(connection, "state_revision"),
            "head_commit": meta_get(connection, "head_commit"),
            "global_next_chapter_seq": global_next,
            "next_chapter_seq": None if next_seq > end else next_seq,
            "completed_chapters": completed,
            "remaining_chapter_sequences": list(range(next_seq, end + 1)) if next_seq <= end else [],
            "findings": findings,
        }
    finally:
        connection.close()


def impact_report(project: Path, chapter_id: str) -> dict[str, Any]:
    connection = connect(project.resolve())
    try:
        impacted = impact_chapters(connection, chapter_id)
        affected_resources: dict[tuple[str, str, str, str], set[str]] = {}
        for item in impacted:
            for resource in touched_resources_for_chapter(connection, item["chapter_id"]):
                key = (
                    resource["resource_kind"],
                    resource["resource_id"],
                    resource["field"],
                    resource["access"],
                )
                affected_resources.setdefault(key, set()).add(item["chapter_id"])
        return {
            "status": "review" if impacted else "clear",
            "source_chapter": chapter_id,
            "changed_resources": changed_resources_for_chapter(connection, chapter_id),
            "impacted_chapters": impacted,
            "impacted_resources": [
                {
                    "resource_kind": key[0],
                    "resource_id": key[1],
                    "field": key[2],
                    "access": key[3],
                    "chapters": sorted(chapters),
                }
                for key, chapters in sorted(affected_resources.items())
            ],
        }
    finally:
        connection.close()


def median_metric(rows: list[sqlite3.Row], field: str) -> float:
    return statistics.median(json.loads(row["style_json"])[field] for row in rows)


def style_drift_findings(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    if len(rows) < 6:
        return [{"severity": "info", "code": "style-sample-small", "detail": "fewer than 6 chapters; drift comparison deferred"}]
    baseline = rows[:3]
    recent = rows[-3:]
    findings: list[dict[str, Any]] = []
    rules = {
        "sentence_mean": (0.65, 1.45),
        "paragraph_mean": (0.55, 1.65),
        "sentence_cv": (0.55, 1.80),
        "paragraph_cv": (0.50, 2.00),
    }
    for field, (low, high) in rules.items():
        start = median_metric(baseline, field)
        end = median_metric(recent, field)
        ratio = end / start if start else 1.0
        if ratio < low or ratio > high:
            findings.append({"severity": "warn", "code": "style-drift", "metric": field, "baseline": round(start, 3), "recent": round(end, 3), "ratio": round(ratio, 3)})
    dialogue_start = median_metric(baseline, "dialogue_ratio")
    dialogue_end = median_metric(recent, "dialogue_ratio")
    if abs(dialogue_end - dialogue_start) > 0.18:
        findings.append({"severity": "warn", "code": "style-drift", "metric": "dialogue_ratio", "baseline": dialogue_start, "recent": dialogue_end})
    formula_start = median_metric(baseline, "formula_findings_per_1000")
    formula_end = median_metric(recent, "formula_findings_per_1000")
    if formula_end > formula_start + 3:
        findings.append({"severity": "warn", "code": "formula-rise", "baseline": formula_start, "recent": formula_end, "detail": "heuristic only; review in context"})
    return findings


def resources_as_of_commit(
    connection: sqlite3.Connection,
    end_commit_id: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    snapshots: dict[str, dict[str, dict[str, Any]]] = {"entity": {}, "thread": {}, "timeline": {}}
    rows = connection.execute(
        "SELECT rc.resource_kind, rc.resource_id, rc.after_json FROM resource_changes rc "
        "JOIN commits c ON c.commit_id = rc.commit_id "
        "WHERE rc.commit_id <= ? AND c.status != 'rolled_back' "
        "AND rc.resource_kind IN ('entity', 'thread', 'timeline') ORDER BY rc.change_id",
        (end_commit_id,),
    ).fetchall()
    for row in rows:
        resources = snapshots[row["resource_kind"]]
        if row["after_json"] is None:
            resources.pop(row["resource_id"], None)
        else:
            resources[row["resource_id"]] = as_object(
                json.loads(row["after_json"]),
                f"historical {row['resource_kind']}:{row['resource_id']}",
            )
    return snapshots["entity"], snapshots["thread"], snapshots["timeline"]


def compute_volume_audit(
    connection: sqlite3.Connection,
    volume_id: str,
    stale_after: int,
    end_commit_id: int,
) -> dict[str, Any]:
    volume = connection.execute("SELECT * FROM volumes WHERE volume_id = ?", (volume_id,)).fetchone()
    require(volume is not None, f"unknown volume {volume_id}")
    chapters = connection.execute(
        "SELECT * FROM chapters WHERE volume_id = ? ORDER BY seq", (volume_id,)
    ).fetchall()
    require(chapters, f"volume {volume_id} has no accepted chapters")
    start_seq, end_seq = chapters[0]["seq"], chapters[-1]["seq"]
    findings: list[dict[str, Any]] = []
    for row in chapters:
        if row["needs_review"]:
            findings.append({"severity": "block", "code": "needs-review", "chapter_id": row["chapter_id"]})

    entities_at_end, threads_at_end, timeline_at_end = resources_as_of_commit(connection, end_commit_id)
    thread_rows = sorted(threads_at_end.values(), key=lambda row: (row["introduced_chapter"], row["thread_id"]))
    debt_summary: list[dict[str, Any]] = []
    for thread in thread_rows:
        if thread["introduced_chapter"] > end_seq:
            continue
        open_thread = thread["status"] in {"open", "advanced"}
        overdue = open_thread and (
            thread["due_chapter"] is not None and thread["due_chapter"] <= end_seq
            or thread["due_volume_id"] == volume_id
        )
        stale = open_thread and end_seq - thread["last_progress_chapter"] > stale_after
        if overdue:
            findings.append({"severity": "block", "code": "overdue-thread", "thread_id": thread["thread_id"], "summary": thread["summary"]})
        elif stale and thread["importance"] in {"high", "critical"}:
            findings.append({"severity": "warn", "code": "stale-thread", "thread_id": thread["thread_id"], "last_progress_chapter": thread["last_progress_chapter"]})
        debt_summary.append({"id": thread["thread_id"], "type": thread["thread_type"], "status": thread["status"], "overdue": overdue, "stale": stale})

    arc_rows = connection.execute(
        "WITH effective AS ("
        "SELECT chapter_id, MAX(commit_id) AS commit_id FROM commits "
        "WHERE commit_id <= ? AND status != 'rolled_back' GROUP BY chapter_id) "
        "SELECT a.*, ch.seq FROM arc_beats a JOIN effective e ON e.commit_id = a.commit_id "
        "JOIN chapters ch ON ch.chapter_id = a.chapter_id "
        "WHERE ch.seq BETWEEN ? AND ? ORDER BY ch.seq, a.beat_id",
        (end_commit_id, start_seq, end_seq),
    ).fetchall()
    arc_summary: dict[str, dict[str, Any]] = {}
    for beat in arc_rows:
        item = arc_summary.setdefault(beat["character_id"], {"beat_count": 0, "dimensions": set(), "first_chapter": beat["chapter_id"], "last_chapter": beat["chapter_id"]})
        item["beat_count"] += 1
        item["dimensions"].add(beat["dimension"])
        item["last_chapter"] = beat["chapter_id"]
    core_characters = []
    for entity in entities_at_end.values():
        if entity["entity_type"] != "character" or not entity["active"]:
            continue
        state = json.loads(entity["state_json"])
        if state.get("role") == "main" or state.get("importance") == "core":
            core_characters.append(entity["entity_id"])
    for character_id in core_characters:
        touched = connection.execute(
            "WITH effective AS ("
            "SELECT chapter_id, MAX(commit_id) AS commit_id FROM commits "
            "WHERE commit_id <= ? AND status != 'rolled_back' GROUP BY chapter_id) "
            "SELECT 1 FROM touches t JOIN effective e ON e.commit_id = t.commit_id "
            "JOIN chapters ch ON ch.chapter_id = t.chapter_id "
            "WHERE t.resource_kind = 'entity' AND t.resource_id = ? "
            "AND ch.seq BETWEEN ? AND ? LIMIT 1",
            (end_commit_id, character_id, start_seq, end_seq),
        ).fetchone()
        if touched and character_id not in arc_summary:
            findings.append({"severity": "warn", "code": "missing-character-arc", "character_id": character_id, "detail": "core character appears in the volume but has no recorded arc beat"})

    try:
        chapter_sequences = {
            row["chapter_id"]: row["seq"]
            for row in connection.execute("SELECT chapter_id, seq FROM chapters")
        }
        validate_resource_integrity(entities_at_end, threads_at_end, timeline_at_end, chapter_sequences)
    except StateError as exc:
        findings.append({"severity": "block", "code": "timeline-or-reference-conflict", "detail": str(exc)})
    findings.extend(style_drift_findings(chapters))

    arc_output = {
        character_id: {**item, "dimensions": sorted(item["dimensions"])}
        for character_id, item in sorted(arc_summary.items())
    }
    status = "block" if any(item["severity"] == "block" for item in findings) else "warn" if any(item["severity"] == "warn" for item in findings) else "pass"
    return {
        "status": status,
        "volume": {"id": volume_id, "title": volume["title"], "start_chapter": start_seq, "end_chapter": end_seq, "accepted_chapters": len(chapters)},
        "character_arcs": arc_output,
        "plot_threads": debt_summary,
        "style": {"baseline_chapters": [row["chapter_id"] for row in chapters[:3]], "recent_chapters": [row["chapter_id"] for row in chapters[-3:]]},
        "findings": findings,
        "snapshot": {"end_commit_id": end_commit_id},
        "note": "style findings are drift heuristics, not an AI probability or a literary score",
    }


def persist_volume_audit(
    connection: sqlite3.Connection,
    result: dict[str, Any],
    stale_after: int,
    end_commit_id: int,
    state_revision: int,
) -> dict[str, Any]:
    volume_id = result["volume"]["id"]
    created_at = utc_now()
    connection.execute(
        "UPDATE volume_audits SET resolved_at = ?, resolved_by_commit_id = ? "
        "WHERE volume_id = ? AND status = 'block' AND resolved_at IS NULL",
        (created_at, end_commit_id, volume_id),
    )
    cursor = connection.execute(
        "INSERT INTO volume_audits(volume_id, end_commit_id, state_revision, end_chapter_seq, stale_after, status, result_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, '{}', ?)",
        (
            volume_id,
            end_commit_id,
            state_revision,
            result["volume"]["end_chapter"],
            stale_after,
            result["status"],
            created_at,
        ),
    )
    stored = copy.deepcopy(result)
    stored["audit"] = {
        "id": int(cursor.lastrowid),
        "end_commit_id": end_commit_id,
        "state_revision": state_revision,
        "created_at": created_at,
        "immutable": True,
    }
    connection.execute(
        "UPDATE volume_audits SET result_json = ? WHERE audit_id = ?",
        (canonical_json(stored), cursor.lastrowid),
    )
    return stored


def audit_volume(
    project: Path,
    volume_id: str,
    stale_after: int,
    *,
    audit_id: int | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    connection = connect(project.resolve())
    try:
        if audit_id is not None:
            stored = connection.execute(
                "SELECT result_json FROM volume_audits WHERE volume_id = ? AND audit_id = ?",
                (volume_id, audit_id),
            ).fetchone()
            require(stored is not None, f"unknown audit {audit_id} for volume {volume_id}")
            return as_object(json.loads(stored["result_json"]), "stored volume audit")
        latest = connection.execute(
            "SELECT result_json FROM volume_audits WHERE volume_id = ? ORDER BY audit_id DESC LIMIT 1",
            (volume_id,),
        ).fetchone()
        if latest is not None and not refresh:
            return as_object(json.loads(latest["result_json"]), "stored volume audit")
        connection.execute("BEGIN IMMEDIATE")
        head = meta_get(connection, "head_commit")
        require(head is not None, "cannot audit a project with no commits")
        boundary = connection.execute(
            "SELECT MAX(current_commit_id) AS commit_id FROM chapters WHERE volume_id = ?",
            (volume_id,),
        ).fetchone()["commit_id"]
        require(boundary is not None, f"volume {volume_id} has no accepted chapters")
        boundary_revision = connection.execute(
            "SELECT state_revision FROM commits WHERE commit_id = ?", (boundary,)
        ).fetchone()["state_revision"]
        result = compute_volume_audit(connection, volume_id, stale_after, int(boundary))
        stored = persist_volume_audit(
            connection, result, stale_after, int(boundary), int(boundary_revision)
        )
        connection.commit()
        return stored
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def recent_chapter_rows(connection: sqlite3.Connection, recent: int) -> list[sqlite3.Row]:
    rows = connection.execute(
        "SELECT chapter_id, seq, title, summary FROM chapters ORDER BY seq DESC LIMIT ?",
        (recent,),
    ).fetchall()
    return list(reversed(rows))


def hot_resource_ids(
    connection: sqlite3.Connection,
    chapter_ids: list[str],
    kind: str,
    limit: int,
) -> list[str]:
    if not chapter_ids:
        return []
    placeholders = ", ".join("?" for _ in chapter_ids)
    rows = connection.execute(
        f"SELECT t.resource_id FROM touches t "
        f"JOIN chapters ch ON ch.chapter_id = t.chapter_id AND ch.current_commit_id = t.commit_id "
        f"JOIN commits c ON c.commit_id = t.commit_id "
        f"WHERE t.resource_kind = ? AND t.chapter_id IN ({placeholders}) AND c.status = 'active' "
        f"ORDER BY ch.seq DESC, t.touch_id DESC",
        (kind, *chapter_ids),
    ).fetchall()
    ordered: list[str] = []
    seen: set[str] = set()
    for row in rows:
        resource_id = row["resource_id"]
        if resource_id in seen:
            continue
        seen.add(resource_id)
        ordered.append(resource_id)
        if len(ordered) >= limit:
            break
    return ordered


def open_thread_rows(connection: sqlite3.Connection, limit: int = HOT_THREAD_LIMIT) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM story_threads WHERE status IN ('open', 'advanced')"
    ).fetchall()
    documents = [{"id": row["thread_id"], **thread_document(dict(row))} for row in rows]
    documents.sort(
        key=lambda item: (
            THREAD_IMPORTANCE_RANK.get(str(item.get("importance")), 9),
            item.get("due_chapter") is None,
            item.get("due_chapter") or 10**9,
            item["id"],
        )
    )
    return documents[:limit]


def load_entity_documents(connection: sqlite3.Connection, entity_ids: list[str], *, required: bool) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for entity_id in entity_ids:
        row = connection.execute("SELECT * FROM entities WHERE entity_id = ? AND active = 1", (entity_id,)).fetchone()
        if row is None:
            require(not required, f"unknown active entity {entity_id}")
            continue
        documents.append({"id": entity_id, **entity_document(dict(row))})
    return documents


def load_thread_documents(connection: sqlite3.Connection, thread_ids: list[str], *, required: bool) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for thread_id in thread_ids:
        row = connection.execute("SELECT * FROM story_threads WHERE thread_id = ?", (thread_id,)).fetchone()
        if row is None:
            require(not required, f"unknown thread {thread_id}")
            continue
        documents.append({"id": thread_id, **thread_document(dict(row))})
    return documents


def related_timeline_events(
    connection: sqlite3.Connection,
    entity_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows = connection.execute("SELECT * FROM timeline_events ORDER BY sequence, event_id").fetchall()
    author: list[dict[str, Any]] = []
    reader: list[dict[str, Any]] = []
    for row in rows:
        event = public_timeline_event(dict(row))
        participants = set(event.get("participant_ids") or [])
        location = event.get("location_id")
        if entity_ids and not (participants & entity_ids) and location not in entity_ids:
            continue
        author.append(event)
        if event["reveal_status"] == "revealed":
            reader.append(event)
        elif event["reveal_status"] == "partial" and event["reader_knowledge"]:
            reader.append({**event, "fact": event["reader_knowledge"]})
    return author, reader, author


def context_document(
    connection: sqlite3.Connection,
    entity_ids: list[str],
    thread_ids: list[str],
    recent: int,
) -> dict[str, Any]:
    require(1 <= recent <= 10, "--recent must be between 1 and 10")
    recent_rows = recent_chapter_rows(connection, recent)
    recent_ids = [row["chapter_id"] for row in recent_rows]
    requested_entities = [clean_id(item, "--entity") for item in entity_ids]
    requested_threads = [clean_id(item, "--thread") for item in thread_ids]
    auto_entities = hot_resource_ids(connection, recent_ids, "entity", HOT_ENTITY_LIMIT)
    auto_threads = hot_resource_ids(connection, recent_ids, "thread", HOT_THREAD_LIMIT)
    if requested_entities:
        load_entity_documents(connection, requested_entities, required=True)
    if requested_threads:
        load_thread_documents(connection, requested_threads, required=True)
    entity_order = list(dict.fromkeys(auto_entities + requested_entities))[:HOT_ENTITY_LIMIT + len(requested_entities)]
    thread_order = list(dict.fromkeys(auto_threads + requested_threads))[:HOT_THREAD_LIMIT + len(requested_threads)]
    entities = load_entity_documents(connection, entity_order, required=False)
    selected_threads = load_thread_documents(connection, thread_order, required=False)
    open_threads = open_thread_rows(connection)
    hot_ids = {item["id"] for item in entities}
    author_timeline, reader_timeline, timeline = related_timeline_events(connection, hot_ids)
    return {
        "status": "ok",
        "state_revision": meta_get(connection, "state_revision"),
        "selection": {
            "requested_entities": requested_entities,
            "requested_threads": requested_threads,
            "automatic": not requested_entities and not requested_threads,
        },
        "entities": entities,
        "threads": selected_threads,
        "open_threads": open_threads,
        "recent_chapters": [dict(row) for row in recent_rows],
        "timeline": timeline,
        "information_boundary": {
            "author_timeline": author_timeline,
            "reader_timeline": reader_timeline,
        },
        "note": "automatic hot set is a lower bound; pass --entity/--thread to add cold resources",
    }


def derived_view_texts(document: dict[str, Any]) -> dict[str, str]:
    banner = "<!-- 派生视图，勿手改；以 story-state.sqlite3 为准。 -->\n"
    entity_lines = [
        f"- `{item['id']}` {item['name']}（{item['type']}）：{canonical_json(item.get('state', {}))}"
        for item in document.get("entities", [])
    ]
    thread_lines = [
        f"- `{item['id']}` {item['status']}/{item['importance']}：{item['summary']}"
        for item in document.get("open_threads", [])
    ]
    recent_lines = [
        f"- 第{item['seq']}章 {item['title']}：{item['summary']}"
        for item in document.get("recent_chapters", [])
    ]
    context = banner + "# 续写上下文\n\n## 近章\n" + ("\n".join(recent_lines) or "- （无）")
    context += "\n\n## 热实体\n" + ("\n".join(entity_lines) or "- （无）")
    context += "\n\n## 未关闭线索\n" + ("\n".join(thread_lines) or "- （无）") + "\n"

    def timeline_markdown(title: str, events: list[dict[str, Any]], *, reader: bool) -> str:
        lines = [banner + f"# {title}\n"]
        if not events:
            lines.append("- （无）\n")
            return "".join(lines)
        for event in events:
            payload = event["reader_knowledge"] if reader else event["fact"]
            extra = f" [{event['reveal_status']}]" if not reader else ""
            lines.append(f"- `{event['id']}` {event['story_time']}{extra}：{payload}\n")
        return "".join(lines)

    boundary = document.get("information_boundary", {})
    return {
        "上下文.md": context,
        "时间线-作者真相.md": timeline_markdown("作者真相", boundary.get("author_timeline", []), reader=False),
        "时间线-读者已知.md": timeline_markdown("读者已知", boundary.get("reader_timeline", []), reader=True),
    }


def derived_view_paths(project: Path) -> dict[str, Path]:
    root = project.resolve() / DERIVED_VIEW_DIR
    return {name: root / name for name in ("上下文.md", "时间线-作者真相.md", "时间线-读者已知.md")}


def render_derived_views(project: Path, document: dict[str, Any] | None = None) -> list[Path]:
    project = project.resolve()
    if document is None:
        connection = connect(project)
        try:
            document = context_document(connection, [], [], 3)
        finally:
            connection.close()
    written: list[Path] = []
    for name, text in derived_view_texts(document).items():
        path = project / DERIVED_VIEW_DIR / name
        atomic_write(path, text)
        written.append(path)
    return written


def derived_view_findings(project: Path, connection: sqlite3.Connection) -> list[dict[str, Any]]:
    paths = derived_view_paths(project)
    existing = [path for path in paths.values() if path.is_file()]
    if not existing:
        return []
    expected = derived_view_texts(context_document(connection, [], [], 3))
    findings: list[dict[str, Any]] = []
    for name, path in paths.items():
        if not path.is_file():
            findings.append({"severity": "block", "code": "derived-view-drift", "path": str(path), "detail": "missing derived view"})
            continue
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            findings.append({"severity": "block", "code": "derived-view-drift", "path": str(path), "detail": str(exc)})
            continue
        if sha256_text(current) != sha256_text(expected[name]):
            findings.append({"severity": "block", "code": "derived-view-drift", "path": str(path)})
    return findings


def context_pack(
    project: Path,
    entity_ids: list[str],
    thread_ids: list[str],
    recent: int,
    *,
    render_views: bool = False,
) -> dict[str, Any]:
    project = project.resolve()
    connection = connect(project)
    try:
        document = context_document(connection, entity_ids, thread_ids, recent)
    finally:
        connection.close()
    if render_views:
        document["derived_views"] = [str(path) for path in render_derived_views(project, document)]
    return document


def emit(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(pretty_json(result))
        return
    status = str(result.get("status", "ok")).upper()
    print(f"{status}: {result.get('chapter_id') or result.get('database') or result.get('source_chapter') or result.get('volume', {}).get('id', '')}")
    print(pretty_json(result))


def common_project(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--json", action="store_true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Transactional story state for long fiction projects.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create a new story-state database")
    common_project(init_parser)
    init_parser.add_argument("--title")

    for command in ("check", "commit"):
        child = subparsers.add_parser(command, help=f"{command} a chapter delta")
        common_project(child)
        child.add_argument("--body", required=True, type=Path)
        child.add_argument("--record", required=True, type=Path)
        child.add_argument("--delta", required=True, type=Path)

    rollback_parser = subparsers.add_parser("rollback", help="roll back the active head commit")
    common_project(rollback_parser)
    rollback_parser.add_argument("--commit", type=int)

    checkout_parser = subparsers.add_parser("checkout", help="restore accepted Markdown snapshots")
    common_project(checkout_parser)
    checkout_parser.add_argument("--chapter", required=True)

    verify_parser = subparsers.add_parser("verify", help="check database and working-copy integrity")
    common_project(verify_parser)

    batch_parser = subparsers.add_parser("batch-status", help="derive a resumable serial chapter-batch checkpoint")
    common_project(batch_parser)
    batch_parser.add_argument("--start", required=True, type=int)
    batch_parser.add_argument("--end", required=True, type=int)

    impact_parser = subparsers.add_parser("impact", help="find downstream dependencies before revising a chapter")
    common_project(impact_parser)
    impact_parser.add_argument("--chapter", required=True)

    audit_parser = subparsers.add_parser("audit-volume", help="audit arcs, debts, timeline, and style drift")
    common_project(audit_parser)
    audit_parser.add_argument("--volume", required=True)
    audit_parser.add_argument("--stale-after", type=int, default=20)
    audit_selection = audit_parser.add_mutually_exclusive_group()
    audit_selection.add_argument("--audit-id", type=int, help="return one immutable stored audit")
    audit_selection.add_argument("--refresh", action="store_true", help="persist a new audit revision")

    context_parser = subparsers.add_parser("context", help="load a selective current-state context pack")
    common_project(context_parser)
    context_parser.add_argument("--entity", action="append", default=[])
    context_parser.add_argument("--thread", action="append", default=[])
    context_parser.add_argument("--recent", type=int, default=3)
    context_parser.add_argument("--render-views", action="store_true", help="write read-only derived Markdown views")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "init":
            result = initialize(args.project, args.title or args.project.resolve().name)
        elif args.command in {"check", "commit"}:
            result = check_or_commit(args.project, args.body, args.record, args.delta, commit=args.command == "commit")
        elif args.command == "rollback":
            result = rollback_head(args.project, args.commit)
        elif args.command == "checkout":
            result = checkout_chapter(args.project, clean_id(args.chapter, "--chapter"))
        elif args.command == "verify":
            result = verify_project(args.project)
        elif args.command == "batch-status":
            result = batch_status(args.project, args.start, args.end)
        elif args.command == "impact":
            result = impact_report(args.project, clean_id(args.chapter, "--chapter"))
        elif args.command == "audit-volume":
            require(args.stale_after >= 1, "--stale-after must be positive")
            require(args.audit_id is None or args.audit_id >= 1, "--audit-id must be positive")
            result = audit_volume(
                args.project,
                clean_id(args.volume, "--volume"),
                args.stale_after,
                audit_id=args.audit_id,
                refresh=args.refresh,
            )
        elif args.command == "context":
            result = context_pack(
                args.project,
                args.entity,
                args.thread,
                args.recent,
                render_views=args.render_views,
            )
        else:  # pragma: no cover
            raise StateError(f"unsupported command {args.command}")
        emit(result, as_json=args.json)
        return 1 if args.command in {"verify", "audit-volume", "batch-status"} and result["status"] == "block" else 0
    except StateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"ERROR: database failure: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
