#!/usr/bin/env python3
"""Normalize and quality-check market ranking samples."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SOURCE_MODES = {"live", "user_provided", "historical"}


class SampleError(ValueError):
    pass


def _read_records(path: Path) -> list[Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SampleError(f"unable to read sample JSON {path}: {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("records")
    if not isinstance(payload, list):
        raise SampleError("sample JSON must be an array or an object with records[]")
    return payload


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _valid_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_samples(
    records: list[Any],
    *,
    minimum_records: int,
    minimum_lists: int,
    maximum_age_days: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    if minimum_records < 1 or minimum_lists < 1 or maximum_age_days < 0:
        raise SampleError("minimums must be positive and --max-age-days must be non-negative")
    reference_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    seen_observations: dict[tuple[str, str, int, str, str], int] = {}
    current_eligible = 0

    for index, raw in enumerate(records):
        label = f"records[{index}]"
        if not isinstance(raw, dict):
            errors.append({"code": "not-object", "record": index})
            continue
        required = ("platform", "list_name", "captured_at", "rank", "title", "source_mode")
        missing = [key for key in required if raw.get(key) in (None, "")]
        if missing:
            errors.append({"code": "missing-fields", "record": index, "fields": missing})
            continue
        platform = str(raw["platform"]).strip()
        list_name = str(raw["list_name"]).strip()
        title = str(raw["title"]).strip()
        author = str(raw["author"]).strip() if raw.get("author") is not None else None
        source_mode = str(raw["source_mode"]).strip()
        rank = raw["rank"]
        captured_at = _timestamp(raw["captured_at"])
        if not platform or not list_name or not title:
            errors.append({"code": "blank-identity", "record": index})
            continue
        if type(rank) is not int or rank < 1:
            errors.append({"code": "invalid-rank", "record": index, "value": rank})
            continue
        if captured_at is None:
            errors.append({"code": "invalid-captured-at", "record": index, "value": raw["captured_at"]})
            continue
        if source_mode not in SOURCE_MODES:
            errors.append({"code": "invalid-source-mode", "record": index, "value": source_mode})
            continue
        age_days = max(0, (reference_time - captured_at).days)
        url = raw.get("url")
        if source_mode == "live" and not _valid_url(url):
            errors.append({"code": "live-source-without-url", "record": index})
            continue
        if url not in (None, "") and not _valid_url(url):
            errors.append({"code": "invalid-url", "record": index, "value": url})
            continue
        tags = raw.get("tags", [])
        metrics = raw.get("metrics", {})
        if not isinstance(tags, list) or any(not isinstance(item, str) for item in tags):
            errors.append({"code": "invalid-tags", "record": index})
            continue
        if not isinstance(metrics, dict):
            errors.append({"code": "invalid-metrics", "record": index})
            continue
        observation = (platform.casefold(), list_name.casefold(), rank, title.casefold(), captured_at.isoformat())
        if observation in seen_observations:
            warnings.append(
                {"code": "duplicate-observation", "record": index, "first_record": seen_observations[observation]}
            )
        else:
            seen_observations[observation] = index
        stale = age_days > maximum_age_days
        if stale:
            warnings.append({"code": "stale-sample", "record": index, "age_days": age_days})
        if source_mode == "historical":
            warnings.append({"code": "historical-source", "record": index})
        if not stale and source_mode != "historical":
            current_eligible += 1
        normalized.append(
            {
                **raw,
                "platform": platform,
                "list_name": list_name,
                "rank": rank,
                "title": title,
                "author": author,
                "captured_at": captured_at.isoformat(),
                "source_mode": source_mode,
                "age_days": age_days,
                "current_eligible": not stale and source_mode != "historical",
                "tags": tags,
                "metrics": metrics,
            }
        )

    list_count = len({(item["platform"].casefold(), item["list_name"].casefold()) for item in normalized})
    if len(normalized) < minimum_records:
        errors.append(
            {"code": "insufficient-valid-records", "actual": len(normalized), "minimum": minimum_records}
        )
    if list_count < minimum_lists:
        errors.append({"code": "insufficient-lists", "actual": list_count, "minimum": minimum_lists})
    if normalized and current_eligible < minimum_records:
        warnings.append(
            {
                "code": "insufficient-current-records",
                "actual": current_eligible,
                "minimum": minimum_records,
                "detail": "current-trend claims are unsupported; historical analysis may continue",
            }
        )
    status = "block" if errors else "warn" if warnings else "pass"
    return {
        "status": status,
        "generated_at": reference_time.isoformat(),
        "thresholds": {
            "minimum_records": minimum_records,
            "minimum_lists": minimum_lists,
            "maximum_age_days": maximum_age_days,
        },
        "summary": {
            "input_records": len(records),
            "valid_records": len(normalized),
            "current_eligible_records": current_eligible,
            "distinct_lists": list_count,
            "error_count": len(errors),
            "warning_count": len(warnings),
        },
        "errors": errors,
        "warnings": warnings,
        "records": normalized,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quality-check normalized fiction ranking samples.")
    parser.add_argument("sample", type=Path)
    parser.add_argument("--min-records", type=int, default=10)
    parser.add_argument("--min-lists", type=int, default=1)
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = validate_samples(
            _read_records(args.sample),
            minimum_records=args.min_records,
            minimum_lists=args.min_lists,
            maximum_age_days=args.max_age_days,
        )
    except SampleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report["summary"]
        print(
            f"{report['status'].upper()}: {summary['valid_records']} valid record(s), "
            f"{summary['distinct_lists']} list(s), {summary['current_eligible_records']} current"
        )
    return 1 if report["status"] == "block" else 0


if __name__ == "__main__":
    raise SystemExit(main())

