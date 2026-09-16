#!/usr/bin/env python3
"""Validate the aggregate length of an ordered short-fiction manuscript."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

from chapter_guard import METRIC, extract_body, measurements


class ManuscriptError(ValueError):
    pass


def manuscript_bounds(
    minimum: int | None,
    maximum: int | None,
    target: int | None,
    tolerance: float,
) -> tuple[int | None, int | None]:
    if target is not None:
        if minimum is not None or maximum is not None:
            raise ManuscriptError("--target cannot be combined with --min or --max")
        if target <= 0 or not 0 <= tolerance < 1:
            raise ManuscriptError("target must be positive and tolerance must be in [0, 1)")
        return math.ceil(target * (1 - tolerance)), math.floor(target * (1 + tolerance))
    if minimum is None and maximum is None:
        raise ManuscriptError("provide --min/--max or --target")
    if minimum is not None and minimum < 0:
        raise ManuscriptError("--min must be non-negative")
    if maximum is not None and maximum < 0:
        raise ManuscriptError("--max must be non-negative")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ManuscriptError("--min cannot exceed --max")
    return minimum, maximum


def evaluate_manuscript(
    paths: list[Path],
    minimum: int | None,
    maximum: int | None,
    metric: str = METRIC,
) -> dict[str, Any]:
    if not paths:
        raise ManuscriptError("at least one manuscript file is required")
    resolved = [path.resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise ManuscriptError("duplicate manuscript paths are not allowed")
    files: list[dict[str, Any]] = []
    combined_parts: list[str] = []
    invalid = False
    total = 0
    for order, path in enumerate(paths, start=1):
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            files.append({"order": order, "path": str(path), "status": "invalid", "error": str(exc)})
            invalid = True
            continue
        counts = measurements(text)
        body = extract_body(text)
        actual = counts[metric]
        empty = actual == 0
        files.append(
            {
                "order": order,
                "path": str(path),
                "status": "empty" if empty else "ok",
                "metric": metric,
                "actual": actual,
                "measurements": counts,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            }
        )
        total += actual
        combined_parts.append(body)
        invalid = invalid or empty
    if invalid:
        status = "invalid"
        delta = 0
    elif minimum is not None and total < minimum:
        status = "under"
        delta = minimum - total
    elif maximum is not None and total > maximum:
        status = "over"
        delta = total - maximum
    else:
        status = "pass"
        delta = 0
    return {
        "status": status,
        "metric": metric,
        "minimum": minimum,
        "maximum": maximum,
        "actual": total,
        "delta_to_band": delta,
        "file_count": len(paths),
        "files": files,
        "combined_body_sha256": hashlib.sha256("\n\u241e\n".join(combined_parts).encode("utf-8")).hexdigest(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check an ordered short-fiction manuscript as one length contract.")
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--min", dest="minimum", type=int)
    parser.add_argument("--max", dest="maximum", type=int)
    parser.add_argument("--target", type=int)
    parser.add_argument("--tolerance", type=float, default=0.05)
    parser.add_argument("--metric", choices=(METRIC, "han_chars"), default=METRIC)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        minimum, maximum = manuscript_bounds(args.minimum, args.maximum, args.target, args.tolerance)
        report = evaluate_manuscript(args.paths, minimum, maximum, args.metric)
    except ManuscriptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"{report['status'].upper()}: {report['actual']} {report['metric']} across "
            f"{report['file_count']} file(s) (range "
            f"{report['minimum'] if report['minimum'] is not None else '-inf'}.."
            f"{report['maximum'] if report['maximum'] is not None else '+inf'})"
        )
    if report["status"] == "invalid":
        return 2
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

