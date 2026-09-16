#!/usr/bin/env python3
"""Build a deterministic inventory for legacy Markdown and text manuscripts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

from chapter_guard import extract_body, measurements


SUPPORTED_SUFFIXES = {".md", ".txt"}
_CHAPTER_TOKEN = r"[0-9零〇一二三四五六七八九十百千万亿两]+"
_CHAPTER = re.compile(rf"第\s*({_CHAPTER_TOKEN})\s*(?:章|回|节)")
_CONTENT_CHAPTER = re.compile(
    rf"^[ \t]*(?:#{{1,6}}[ \t]+)?第[ \t\u3000]*({_CHAPTER_TOKEN})"
    r"[ \t\u3000]*(?:章|回|节)(?:[ \t\u3000]*$|[ \t\u3000:：_\-—]+.+)$"
)
_FENCE_START = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_LARGE_UNITS = {"万": 10_000, "亿": 100_000_000}


class InventoryError(ValueError):
    pass


def chinese_number(value: str) -> int | None:
    normalized = unicodedata.normalize("NFKC", value)
    if normalized.isdigit():
        return int(normalized)
    if not normalized or any(
        char not in _DIGITS and char not in _SMALL_UNITS and char not in _LARGE_UNITS
        for char in normalized
    ):
        return None
    if all(char in _DIGITS for char in normalized):
        return int("".join(str(_DIGITS[char]) for char in normalized))
    total = 0
    section = 0
    number = 0
    for char in normalized:
        if char in _DIGITS:
            number = _DIGITS[char]
        elif char in _SMALL_UNITS:
            if number == 0:
                number = 1
            section += number * _SMALL_UNITS[char]
            number = 0
        else:
            section += number
            if section == 0:
                section = 1
            total += section * _LARGE_UNITS[char]
            section = 0
            number = 0
    return total + section + number


def _chapter_match(value: str) -> int | None:
    match = _CHAPTER.search(unicodedata.normalize("NFKC", value))
    return chinese_number(match.group(1)) if match else None


def content_chapters(text: str) -> list[dict[str, int | str | None]]:
    lines = text.splitlines()
    closing = -1
    if lines and lines[0].strip() == "---":
        closing = next(
            (index for index, line in enumerate(lines[1:201], start=1) if line.strip() in {"---", "..."}),
            -1,
        )
    start = closing + 1 if closing >= 0 else 0
    candidates: list[dict[str, int | str | None]] = []
    comment = False
    fence_char: str | None = None
    for line_number, line in enumerate(lines[start:], start=start + 1):
        stripped = line.lstrip()
        if comment:
            if "-->" in stripped:
                comment = False
            continue
        if stripped.startswith("<!--"):
            if "-->" not in stripped:
                comment = True
            continue
        fence = _FENCE_START.match(line)
        if fence:
            marker = fence.group(1)[0]
            if fence_char is None:
                fence_char = marker
            elif fence_char == marker:
                fence_char = None
            continue
        if fence_char is not None:
            continue
        match = _CONTENT_CHAPTER.match(line)
        if not match:
            continue
        sequence = chinese_number(match.group(1))
        if sequence is not None:
            candidates.append({"chapter_seq": sequence, "line": line_number, "source": "content"})
    return candidates


def detect_chapters(path: Path, text: str) -> tuple[list[dict[str, int | str | None]], int | None]:
    filename_sequence = _chapter_match(path.stem)
    candidates = content_chapters(text)
    if candidates:
        return candidates, filename_sequence
    if filename_sequence is not None:
        return [{"chapter_seq": filename_sequence, "line": None, "source": "filename"}], filename_sequence
    return [], None


def _natural_key(path: Path) -> tuple[tuple[int, Any], ...]:
    parts = re.split(r"(\d+)", path.as_posix().casefold())
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)


def discover(source: Path) -> tuple[Path, list[Path]]:
    if not source.exists():
        raise InventoryError(f"source does not exist: {source}")
    if source.is_file():
        if source.suffix.casefold() not in SUPPORTED_SUFFIXES:
            raise InventoryError("source file must use .md or .txt")
        return source.parent, [source]
    if not source.is_dir():
        raise InventoryError(f"source is not a regular file or directory: {source}")
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        for filename in filenames:
            path = Path(directory) / filename
            if not filename.startswith(".") and path.suffix.casefold() in SUPPORTED_SUFFIXES:
                files.append(path)
    files.sort(key=lambda path: _natural_key(path.relative_to(source)))
    return source, files


def _missing_ranges(numbers: list[int]) -> tuple[int, list[dict[str, int]]]:
    if len(numbers) < 2:
        return 0, []
    missing_count = 0
    ranges: list[dict[str, int]] = []
    for left, right in zip(numbers, numbers[1:]):
        if right <= left + 1:
            continue
        start = left + 1
        end = right - 1
        missing_count += end - start + 1
        ranges.append({"start": start, "end": end})
    return missing_count, ranges


def build_inventory(source: Path) -> dict[str, Any]:
    root, paths = discover(source)
    records: list[dict[str, Any]] = []
    chapter_occurrences: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    content_paths: defaultdict[str, list[str]] = defaultdict(list)
    ordered_occurrences: list[dict[str, Any]] = []
    filename_conflicts: list[dict[str, Any]] = []

    for path in paths:
        relative = path.relative_to(root).as_posix()
        try:
            raw = path.read_bytes()
        except OSError as exc:
            records.append(
                {
                    "path": relative,
                    "status": "unreadable",
                    "error": str(exc),
                }
            )
            continue
        sha256 = hashlib.sha256(raw).hexdigest()
        encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError as exc:
            records.append(
                {
                    "path": relative,
                    "status": "invalid_encoding",
                    "bytes": len(raw),
                    "sha256": sha256,
                    "encoding": "not-utf-8",
                    "error": str(exc),
                }
            )
            continue

        chapter_candidates, filename_sequence = detect_chapters(path, text)
        sequences = [int(candidate["chapter_seq"]) for candidate in chapter_candidates]
        if filename_sequence is not None and sequences and filename_sequence not in sequences:
            filename_conflicts.append(
                {
                    "path": relative,
                    "filename_chapter_seq": filename_sequence,
                    "content_chapter_sequences": sequences,
                }
            )
        counts = measurements(text)
        body = extract_body(text).strip()
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        empty = counts["visible_chars_v1"] == 0
        record = {
            "path": relative,
            "status": "empty" if empty else "ok",
            "bytes": len(raw),
            "sha256": sha256,
            "encoding": encoding,
            "chapter_seq": sequences[0] if sequences else None,
            "chapter_sequences": sequences,
            "chapter_count": len(sequences),
            "chapter_detected_from": chapter_candidates[0]["source"] if chapter_candidates else None,
            "chapter_candidates": chapter_candidates,
            "visible_chars_v1": counts["visible_chars_v1"],
            "han_chars": counts["han_chars"],
            "body_sha256": body_sha256,
            "empty_body": empty,
        }
        records.append(record)
        for candidate in chapter_candidates:
            occurrence = {
                "path": relative,
                "line": candidate["line"],
                "source": candidate["source"],
                "chapter_seq": candidate["chapter_seq"],
            }
            chapter_occurrences[int(candidate["chapter_seq"])].append(occurrence)
            ordered_occurrences.append(occurrence)
        if not empty:
            content_paths[body_sha256].append(relative)

    valid_records = [record for record in records if record["status"] in {"ok", "empty"}]
    chapter_numbers = sorted(chapter_occurrences)
    missing_count, missing_ranges = _missing_ranges(chapter_numbers)
    duplicates = [
        {
            "chapter_seq": sequence,
            "paths": [entry["path"] for entry in entries],
            "occurrences": entries,
        }
        for sequence, entries in sorted(chapter_occurrences.items())
        if len(entries) > 1
    ]
    duplicate_contents = [
        {"body_sha256": digest, "paths": entries}
        for digest, entries in sorted(content_paths.items())
        if len(entries) > 1
    ]
    order_issues: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for occurrence in ordered_occurrences:
        if previous is not None and occurrence["chapter_seq"] < previous["chapter_seq"]:
            order_issues.append(
                {
                    "previous_path": previous["path"],
                    "previous_line": previous["line"],
                    "previous_chapter_seq": previous["chapter_seq"],
                    "path": occurrence["path"],
                    "line": occurrence["line"],
                    "chapter_seq": occurrence["chapter_seq"],
                }
            )
        previous = occurrence

    encoding_errors = [
        record["path"] for record in records if record["status"] in {"invalid_encoding", "unreadable"}
    ]
    empty_files = [record["path"] for record in records if record["status"] == "empty"]
    unidentified = [record["path"] for record in valid_records if not record["chapter_sequences"]]
    issue_count = (
        len(encoding_errors)
        + len(empty_files)
        + len(duplicates)
        + missing_count
        + len(duplicate_contents)
        + len(order_issues)
        + len(unidentified)
        + len(filename_conflicts)
    )
    return {
        "schema_version": 1,
        "source": str(source.resolve()),
        "status": "review" if issue_count else "clear",
        "metric": "visible_chars_v1",
        "summary": {
            "file_count": len(records),
            "readable_file_count": len(valid_records),
            "chapter_number_count": len(chapter_numbers),
            "chapter_occurrence_count": len(ordered_occurrences),
            "visible_chars_v1": sum(record.get("visible_chars_v1", 0) for record in records),
            "issue_count": issue_count,
        },
        "files": records,
        "issues": {
            "encoding_or_read_errors": encoding_errors,
            "empty_body_files": empty_files,
            "unidentified_chapter_files": unidentified,
            "filename_content_conflicts": filename_conflicts,
            "duplicate_chapter_numbers": duplicates,
            "missing_chapter_count": missing_count,
            "missing_chapter_ranges": missing_ranges,
            "order_anomalies": order_issues,
            "duplicate_contents": duplicate_contents,
        },
        "note": (
            "Mechanical inventory only. Duplicate, missing, and unidentified files require authorial "
            "adjudication before defining the accepted corpus."
        ),
    }


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InventoryError(f"unable to read accepted manifest {path}: {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("accepted") or payload.get("items") or payload.get("records")
    if not isinstance(payload, list) or not payload:
        raise InventoryError("accepted manifest must be a non-empty array")
    return payload


def _chapter_title(line: str | None, sequence: int) -> str:
    if not line:
        return f"第{sequence:03d}章"
    stripped = re.sub(r"^#{1,6}\s*", "", line).strip()
    stripped = re.sub(r"^第\s*[0-9零〇一二三四五六七八九十百千万亿两]+\s*(?:章|回|节)\s*[:：_\-—]*\s*", "", stripped)
    return stripped or f"第{sequence:03d}章"


def _slice_chapter(lines: list[str], candidates: list[dict[str, Any]], sequence: int) -> str:
    indexed = [item for item in candidates if item.get("chapter_seq") == sequence and item.get("line")]
    if not indexed:
        raise InventoryError(f"chapter {sequence} has no reliable content line number")
    start = int(indexed[0]["line"]) - 1
    later = sorted(
        int(item["line"]) - 1
        for item in candidates
        if item.get("line") and int(item["line"]) - 1 > start
    )
    end = later[0] if later else len(lines)
    return "\n".join(lines[start:end]).rstrip() + "\n"


def split_accepted_corpus(source: Path, staging: Path, manifest_path: Path) -> dict[str, Any]:
    report = build_inventory(source)
    root = Path(report["source"])
    files = {item["path"]: item for item in report["files"]}
    manifest = _load_manifest(manifest_path)
    staging = staging.resolve()
    if staging.exists() and any(staging.iterdir()):
        raise InventoryError(f"staging directory is not empty: {staging}")
    accepted: list[dict[str, Any]] = []
    seen_seq: set[int] = set()
    for index, raw in enumerate(manifest):
        if not isinstance(raw, dict):
            raise InventoryError(f"manifest[{index}] must be an object")
        status = str(raw.get("status", "accepted")).strip()
        if status != "accepted":
            continue
        relative = str(raw.get("path") or "").strip()
        sequence = raw.get("chapter_seq")
        digest = str(raw.get("sha256") or "").strip()
        if not relative or type(sequence) is not int or sequence < 1 or not digest:
            raise InventoryError(f"manifest[{index}] needs path, sha256, and positive chapter_seq")
        if sequence in seen_seq:
            raise InventoryError(f"duplicate accepted chapter_seq {sequence}")
        record = files.get(relative)
        if record is None:
            raise InventoryError(f"manifest path is not in inventory: {relative}")
        if record.get("status") not in {"ok"}:
            raise InventoryError(f"manifest path is not a readable accepted chapter: {relative}")
        if record.get("sha256") != digest:
            raise InventoryError(f"sha256 mismatch for {relative}")
        if sequence not in record.get("chapter_sequences", []):
            raise InventoryError(f"chapter {sequence} is not present in {relative}")
        seen_seq.add(sequence)
        accepted.append({"path": relative, "sha256": digest, "chapter_seq": sequence, "record": record})
    if not accepted:
        raise InventoryError("accepted manifest contains no accepted chapters")
    body_dir = staging / "正文"
    body_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[dict[str, Any]] = []
    for item in sorted(accepted, key=lambda entry: entry["chapter_seq"]):
        source_file = root / item["path"] if root.is_dir() else root
        if root.is_file():
            source_file = root
        text = source_file.read_text(encoding="utf-8-sig")
        lines = text.splitlines()
        candidates = item["record"]["chapter_candidates"]
        if len(item["record"]["chapter_sequences"]) == 1 and candidates and candidates[0].get("line") is None:
            chunk = text if text.endswith("\n") else text + "\n"
            title_line = None
        else:
            chunk = _slice_chapter(lines, candidates, item["chapter_seq"])
            title_line = lines[int(next(c["line"] for c in candidates if c["chapter_seq"] == item["chapter_seq"] and c.get("line"))) - 1]
        title = _chapter_title(title_line, item["chapter_seq"])
        output_name = f"第{item['chapter_seq']:03d}章_{title}.md"
        output_path = body_dir / output_name
        output_path.write_text(chunk, encoding="utf-8")
        outputs.append(
            {
                "chapter_seq": item["chapter_seq"],
                "source_path": item["path"],
                "source_sha256": item["sha256"],
                "output": f"正文/{output_name}",
            }
        )
    sidecar = {
        "schema_version": 1,
        "source": str(root),
        "staging": str(staging),
        "chapters": outputs,
        "note": "Mechanical split only. Confirm titles and extras before story_state init.",
    }
    (staging / "accepted-corpus.json").write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return sidecar


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inventory legacy .md/.txt fiction without modifying it.")
    parser.add_argument("source", type=Path, help="legacy manuscript file or directory")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--accepted-manifest", type=Path, help="author-adjudicated accepted corpus JSON")
    parser.add_argument("--split-staging", type=Path, help="write accepted chapters into an empty staging directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.split_staging is not None:
            if args.accepted_manifest is None:
                raise InventoryError("--split-staging requires --accepted-manifest")
            report = split_accepted_corpus(args.source, args.split_staging, args.accepted_manifest)
            status_code = 0
        else:
            report = build_inventory(args.source)
            status_code = 0 if report["status"] == "clear" else 1
    except InventoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.split_staging is not None:
        print(f"SPLIT {report['staging']}: {len(report['chapters'])} chapter(s)")
    else:
        summary = report["summary"]
        print(
            f"{report['status'].upper()} {report['source']}: "
            f"{summary['file_count']} file(s), {summary['visible_chars_v1']} visible chars, "
            f"{summary['issue_count']} issue(s)"
        )
        for name, value in report["issues"].items():
            count = value if name == "missing_chapter_count" else len(value)
            if count:
                print(f"  {name}: {count}")
        print(f"  note: {report['note']}")
    return status_code


if __name__ == "__main__":
    raise SystemExit(main())
