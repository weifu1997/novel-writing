#!/usr/bin/env python3
"""Manage resumable, source-bound fiction deconstruction workspaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from legacy_inventory import detect_chapters


SCHEMA_VERSION = 1
STAGES = tuple(range(7))
META_NAME = "_meta.json"
STAGE_FILES = {
    0: ("来源.md",),
    1: ("开篇.md",),
    3: ("剧情/单元.md", "剧情/节奏.md", "剧情/情绪与承诺.md"),
    4: ("人物与关系.md", "设定与机制.md", "章法.md"),
    5: ("文风.md",),
    6: ("召回卡.md", "拆文报告.md"),
}
RECALL_FIELDS = ("id", "function", "conditions", "source", "similarity_risk", "failure_mode", "confidence")
RECALL_CONFIDENCE = {"high", "medium", "low"}
RECALL_TASKS = {
    "planning": ("剧情/单元.md", "剧情/节奏.md", "剧情/情绪与承诺.md", "召回卡.md", "拆文报告.md"),
    "chapter": ("章法.md", "召回卡.md", "开篇.md"),
    "style": ("文风.md",),
    "review": ("召回卡.md", "拆文报告.md", "人物与关系.md"),
}
ENTRY_MARK = re.compile(r"(?m)^(?:##\s+\S|id:\s*\S)")
RECALL_BLOCK = re.compile(r"(?ms)^```ya?ml\n(.*?)```")


class WorkspaceError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def meta_path(workspace: Path) -> Path:
    return workspace.resolve() / META_NAME


def read_meta(workspace: Path) -> dict[str, Any]:
    path = meta_path(workspace)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"unable to read workspace metadata {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise WorkspaceError(f"unsupported or invalid workspace metadata: {path}")
    return value


def _source_path(workspace: Path, meta: dict[str, Any]) -> Path:
    original = Path(str(meta["source"]["path"]))
    snapshot = meta["source"].get("snapshot")
    if original.is_file():
        return original
    if snapshot:
        candidate = workspace.resolve() / str(snapshot)
        if candidate.is_file():
            return candidate
    raise WorkspaceError("source file is unavailable and no readable snapshot exists")


def assert_source_unchanged(workspace: Path, meta: dict[str, Any]) -> Path:
    source = _source_path(workspace, meta)
    current = file_sha256(source)
    if current != meta["source"]["sha256"]:
        raise WorkspaceError("source hash changed; initialize a new versioned workspace before continuing")
    return source


def initialize_workspace(
    workspace: Path,
    source: Path,
    title: str,
    kind: str,
    snapshot_source: bool,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    source = source.resolve()
    if kind not in {"long", "short"}:
        raise WorkspaceError("--kind must be long or short")
    if not title.strip():
        raise WorkspaceError("--title must not be empty")
    if not source.is_file():
        raise WorkspaceError(f"source file does not exist: {source}")
    if meta_path(workspace).exists():
        raise WorkspaceError(f"workspace already initialized: {workspace}")
    try:
        raw = source.read_bytes()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise WorkspaceError(f"source must be a readable UTF-8 text file: {exc}") from exc
    workspace.mkdir(parents=True, exist_ok=True)
    snapshot: str | None = None
    if snapshot_source:
        snapshot_path = workspace / "原文" / source.name
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, snapshot_path)
        snapshot = snapshot_path.relative_to(workspace).as_posix()
    boundaries, filename_sequence = detect_chapters(source, text)
    metadata: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "title": title.strip(),
        "kind": kind,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "source": {
            "path": str(source),
            "snapshot": snapshot,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "filename_chapter_seq": filename_sequence,
            "chapter_boundaries": boundaries,
        },
        "completed_stages": [],
        "in_progress_stage": None,
        "stale_stages": [],
        "artifacts": {},
        "failures": [],
    }
    atomic_json(meta_path(workspace), metadata)
    return workspace_status(workspace, metadata)


def _validate_stage(stage: int) -> None:
    if stage not in STAGES:
        raise WorkspaceError("stage must be between 0 and 6")


def begin_stage(workspace: Path, stage: int) -> dict[str, Any]:
    workspace = workspace.resolve()
    _validate_stage(stage)
    meta = read_meta(workspace)
    assert_source_unchanged(workspace, meta)
    active = meta.get("in_progress_stage")
    if active is not None:
        if active == stage:
            return workspace_status(workspace, meta)
        raise WorkspaceError(f"stage {active} is already in progress")
    completed = sorted(int(item) for item in meta.get("completed_stages", []))
    if stage in completed:
        invalidated = [item for item in completed if item >= stage]
        meta["completed_stages"] = [item for item in completed if item < stage]
        meta["stale_stages"] = sorted(set(meta.get("stale_stages", [])) | set(invalidated))
        meta["artifacts"] = {
            key: value for key, value in meta.get("artifacts", {}).items() if int(key) < stage
        }
    else:
        expected = next((item for item in STAGES if item not in completed), None)
        if expected is None:
            raise WorkspaceError("all stages are complete; reopen an earlier stage to revise")
        if stage != expected:
            raise WorkspaceError(f"stage {stage} cannot begin before stage {expected} is complete")
    meta["in_progress_stage"] = stage
    meta["updated_at"] = utc_now()
    atomic_json(meta_path(workspace), meta)
    return workspace_status(workspace, meta)


def _relative_artifact(workspace: Path, value: str) -> tuple[str, Path]:
    candidate = (workspace / value).resolve()
    try:
        relative = candidate.relative_to(workspace.resolve()).as_posix()
    except ValueError as exc:
        raise WorkspaceError(f"artifact escapes workspace: {value}") from exc
    if not candidate.is_file() or candidate.stat().st_size == 0:
        raise WorkspaceError(f"artifact is missing or empty: {value}")
    return relative, candidate


def complete_stage(workspace: Path, stage: int, artifacts: list[str]) -> dict[str, Any]:
    workspace = workspace.resolve()
    _validate_stage(stage)
    if not artifacts:
        raise WorkspaceError("at least one --artifact is required")
    meta = read_meta(workspace)
    assert_source_unchanged(workspace, meta)
    if meta.get("in_progress_stage") != stage:
        raise WorkspaceError(f"stage {stage} is not the active in-progress stage")
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in artifacts:
        relative, path = _relative_artifact(workspace, value)
        if relative in seen:
            raise WorkspaceError(f"duplicate artifact: {relative}")
        seen.add(relative)
        accepted.append({"path": relative, "sha256": file_sha256(path), "bytes": path.stat().st_size})
    accepted_paths = {item["path"] for item in accepted}
    if stage == 2:
        if not any(path.startswith("章节/") for path in accepted_paths):
            raise WorkspaceError("stage 2 requires at least one non-empty artifact under 章节/")
    else:
        missing_required = [path for path in STAGE_FILES.get(stage, ()) if path not in accepted_paths]
        if missing_required:
            raise WorkspaceError(
                f"stage {stage} is missing required artifacts: {', '.join(missing_required)}"
            )
    _validate_stage_contents(workspace, stage, accepted_paths)
    completed = sorted(set(int(item) for item in meta.get("completed_stages", [])) | {stage})
    expected_prefix = list(range(max(completed) + 1))
    if completed != expected_prefix:
        raise WorkspaceError("completed stages must remain a contiguous prefix")
    meta["completed_stages"] = completed
    meta["in_progress_stage"] = None
    meta["stale_stages"] = [item for item in meta.get("stale_stages", []) if item != stage]
    meta.setdefault("artifacts", {})[str(stage)] = accepted
    meta["updated_at"] = utc_now()
    atomic_json(meta_path(workspace), meta)
    return workspace_status(workspace, meta)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise WorkspaceError(f"unable to read artifact {path}: {exc}") from exc


def _has_entry(text: str) -> bool:
    return ENTRY_MARK.search(text) is not None


def _parse_simple_yaml(block: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[key.strip()] = value.strip().strip("\"'")
    return parsed


def _recall_cards(text: str) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    for match in RECALL_BLOCK.finditer(text):
        parsed = _parse_simple_yaml(match.group(1))
        if parsed:
            cards.append(parsed)
    if not cards:
        parsed = _parse_simple_yaml(text)
        if parsed:
            cards.append(parsed)
    return cards


def _validate_stage_contents(workspace: Path, stage: int, accepted_paths: set[str]) -> None:
    if stage == 3:
        for relative in ("剧情/节奏.md", "剧情/情绪与承诺.md"):
            if relative in accepted_paths and not _has_entry(_read_text(workspace / relative)):
                raise WorkspaceError(f"{relative} needs at least one heading or id: entry")
        return
    if stage != 6 or "召回卡.md" not in accepted_paths:
        return
    cards = _recall_cards(_read_text(workspace / "召回卡.md"))
    if not cards:
        raise WorkspaceError("召回卡.md needs at least one YAML recall card")
    for index, card in enumerate(cards):
        missing = [field for field in RECALL_FIELDS if not str(card.get(field, "")).strip()]
        if missing:
            raise WorkspaceError(f"recall card {index} is missing fields: {', '.join(missing)}")
        confidence = str(card.get("confidence", "")).strip()
        if confidence not in RECALL_CONFIDENCE:
            raise WorkspaceError("recall card confidence must be high, medium, or low")


def recall_index(workspace: Path, task: str) -> dict[str, Any]:
    workspace = workspace.resolve()
    if task not in RECALL_TASKS:
        raise WorkspaceError("--task must be planning, chapter, style, or review")
    meta = read_meta(workspace)
    status = workspace_status(workspace, meta)
    wanted = list(RECALL_TASKS[task])
    artifacts = meta.get("artifacts", {})
    stale = set(int(item) for item in meta.get("stale_stages", []))
    available: list[dict[str, Any]] = []
    missing: list[str] = []
    for relative in wanted:
        found = None
        stage_key = None
        for key, entries in artifacts.items():
            for entry in entries:
                if entry.get("path") == relative:
                    found = entry
                    stage_key = int(key)
                    break
            if found:
                break
        if found is None:
            missing.append(relative)
            continue
        available.append(
            {
                "path": relative,
                "stage": stage_key,
                "stale": stage_key in stale,
                "sha256": found.get("sha256"),
            }
        )
    return {
        **status,
        "task": task,
        "recall": available,
        "missing_recall": missing,
    }


def record_failure(workspace: Path, stage: int, detail: str) -> dict[str, Any]:
    workspace = workspace.resolve()
    _validate_stage(stage)
    meta = read_meta(workspace)
    if not detail.strip():
        raise WorkspaceError("failure detail must not be empty")
    meta.setdefault("failures", []).append({"stage": stage, "detail": detail.strip(), "recorded_at": utc_now()})
    meta["updated_at"] = utc_now()
    atomic_json(meta_path(workspace), meta)
    return workspace_status(workspace, meta)


def workspace_status(workspace: Path, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    workspace = workspace.resolve()
    value = meta or read_meta(workspace)
    source_error: str | None = None
    try:
        source_path = assert_source_unchanged(workspace, value)
    except WorkspaceError as exc:
        source_path = None
        source_error = str(exc)
    completed = sorted(int(item) for item in value.get("completed_stages", []))
    active = value.get("in_progress_stage")
    stale = sorted(int(item) for item in value.get("stale_stages", []))
    artifact_findings: list[dict[str, Any]] = []
    for stage, entries in value.get("artifacts", {}).items():
        for entry in entries:
            path = workspace / entry["path"]
            if not path.is_file():
                artifact_findings.append({"stage": int(stage), "path": entry["path"], "code": "missing"})
            elif file_sha256(path) != entry["sha256"]:
                artifact_findings.append({"stage": int(stage), "path": entry["path"], "code": "drift"})
    if source_error or artifact_findings:
        status = "block"
    elif active is not None:
        status = "in_progress"
    elif stale:
        status = "stale"
    elif completed == list(STAGES):
        status = "complete"
    else:
        status = "ready"
    next_stage = active if active is not None else next((item for item in STAGES if item not in completed), None)
    return {
        "status": status,
        "workspace": str(workspace),
        "title": value["title"],
        "kind": value["kind"],
        "source": str(source_path) if source_path else value["source"]["path"],
        "source_error": source_error,
        "completed_stages": completed,
        "in_progress_stage": active,
        "stale_stages": stale,
        "next_stage": next_stage,
        "artifact_findings": artifact_findings,
        "failure_count": len(value.get("failures", [])),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage a resumable fiction deconstruction workspace.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init")
    init.add_argument("--workspace", required=True, type=Path)
    init.add_argument("--source", required=True, type=Path)
    init.add_argument("--title", required=True)
    init.add_argument("--kind", choices=("long", "short"), required=True)
    init.add_argument("--snapshot-source", action="store_true")
    init.add_argument("--json", action="store_true")

    for command in ("begin", "complete", "fail"):
        child = subparsers.add_parser(command)
        child.add_argument("--workspace", required=True, type=Path)
        child.add_argument("--stage", required=True, type=int)
        if command == "complete":
            child.add_argument("--artifact", action="append", default=[])
        if command == "fail":
            child.add_argument("--detail", required=True)
        child.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("--workspace", required=True, type=Path)
    status.add_argument("--json", action="store_true")

    recall = subparsers.add_parser("recall")
    recall.add_argument("--workspace", required=True, type=Path)
    recall.add_argument("--task", choices=tuple(RECALL_TASKS), required=True)
    recall.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "init":
            result = initialize_workspace(args.workspace, args.source, args.title, args.kind, args.snapshot_source)
        elif args.command == "begin":
            result = begin_stage(args.workspace, args.stage)
        elif args.command == "complete":
            result = complete_stage(args.workspace, args.stage, args.artifact)
        elif args.command == "fail":
            result = record_failure(args.workspace, args.stage, args.detail)
        elif args.command == "recall":
            result = recall_index(args.workspace, args.task)
        else:
            result = workspace_status(args.workspace)
    except WorkspaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"{result['status'].upper()} {result['workspace']}: completed={result['completed_stages']}, "
            f"active={result['in_progress_stage']}, next={result['next_stage']}"
        )
    return 1 if result["status"] == "block" else 0


if __name__ == "__main__":
    raise SystemExit(main())
