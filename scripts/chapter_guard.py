#!/usr/bin/env python3
"""Deterministic length guard for Chinese fiction chapters."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from pathlib import Path


METRIC = "visible_chars_v1"
_FRONTMATTER_KEY = re.compile(r"^[A-Za-z_\u3400-\u9fff][^:\n]{0,80}:[ \t]*.*$")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+\S")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_FENCE = re.compile(r"^[ \t]*(```+|~~~+).*$", re.M)
_SCENE_BREAK = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.M)
_MARKUP = re.compile(r"(?<!\\)[*_~`]")
_CHAPTER_NUMBER = r"[0-9零〇一二三四五六七八九十百千万两]+"
_PLAIN_CHAPTER_TITLE = re.compile(
    rf"^[ \t\u3000]*(?:第[ \t\u3000]*{_CHAPTER_NUMBER}[ \t\u3000]*(?:章|回|节)|"
    rf"楔子|序章|序言|引子|终章|尾声|后记|番外(?:[ \t\u3000]*{_CHAPTER_NUMBER})?)"
    r"(?:[ \t\u3000]*$|[ \t\u3000:：_\-—]+.+)$"
)
_CJK = re.compile(
    "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    "\U00020000-\U0002a6df\U0002a700-\U0002b73f"
    "\U0002b740-\U0002b81f\U0002b820-\U0002ceaf"
    "\U0002ceb0-\U0002ebef\U00030000-\U0003134f]"
)


class GuardError(ValueError):
    pass


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")


def _frontmatter_closing_index(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        return -1
    limit = min(len(lines), 201)
    closing = next((i for i in range(1, limit) if lines[i].strip() in {"---", "..."}), -1)
    if closing < 2 or not any(_FRONTMATTER_KEY.match(line) for line in lines[1:closing]):
        return -1
    return closing


def strip_frontmatter(text: str) -> str:
    lines = text.split("\n")
    closing = _frontmatter_closing_index(lines)
    if closing < 0:
        return text
    return "\n".join(lines[closing + 1 :])


def _blank_non_newline(value: str) -> str:
    return "".join("\n" if char == "\n" else " " for char in value)


def _blank_line(line: str) -> str:
    return _blank_non_newline(line)


def _find_balanced_end(text: str, start: int, opening: str, closing: str) -> int | None:
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "\n":
            return None
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _strip_markdown_links(text: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        image = text.startswith("![", index)
        if text[index] != "[" and not image:
            output.append(text[index])
            index += 1
            continue
        label_start = index + 2 if image else index + 1
        label_end = _find_balanced_end(text, label_start - 1, "[", "]")
        if label_end is None or label_end + 1 >= len(text) or text[label_end + 1] != "(":
            output.append(text[index])
            index += 1
            continue
        target_end = _find_balanced_end(text, label_end + 1, "(", ")")
        if target_end is None:
            output.append(text[index])
            index += 1
            continue
        output.append(text[label_start:label_end])
        index = target_end + 1
    return "".join(output)


def extract_body(text: str) -> str:
    body = normalize_newlines(text)
    lines = body.splitlines(keepends=True)
    closing = _frontmatter_closing_index(body.split("\n"))
    if closing >= 0:
        for index in range(closing + 1):
            lines[index] = _blank_line(lines[index])
    body = "".join(lines)
    body = _HTML_COMMENT.sub(lambda match: _blank_non_newline(match.group(0)), body)

    lines = body.splitlines(keepends=True)
    first_content = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first_content is not None:
        candidate = lines[first_content].rstrip("\n")
        if _HEADING.match(candidate) or _PLAIN_CHAPTER_TITLE.match(candidate):
            lines[first_content] = _blank_line(lines[first_content])
    body = "".join(lines)

    body = _strip_markdown_links(body)
    body = _FENCE.sub(lambda match: _blank_non_newline(match.group(0)), body)
    body = _SCENE_BREAK.sub(lambda match: _blank_non_newline(match.group(0)), body)
    body = re.sub(r"^[ \t]{0,3}>[ \t]?", lambda match: " " * len(match.group(0)), body, flags=re.M)
    body = re.sub(
        r"^[ \t]{0,3}#{1,6}[ \t]+",
        lambda match: " " * len(match.group(0)),
        body,
        flags=re.M,
    )
    body = _MARKUP.sub(" ", body)
    return body


def measurements(text: str) -> dict[str, int]:
    body = extract_body(text)
    visible = [char for char in body if not char.isspace()]
    return {
        METRIC: len(visible),
        "han_chars": len(_CJK.findall(body)),
        "latin_words": len(re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)*", body)),
        "digits": sum(char.isdigit() for char in body),
        "punctuation": sum(unicodedata.category(char).startswith("P") for char in visible),
    }


def bounds(args: argparse.Namespace) -> tuple[int | None, int | None]:
    if args.target is not None:
        if args.min_chars is not None or args.max_chars is not None:
            raise GuardError("--target cannot be combined with --min or --max")
        if args.target <= 0 or not 0 <= args.tolerance < 1:
            raise GuardError("target must be positive and tolerance must be in [0, 1)")
        low = math.ceil(args.target * (1 - args.tolerance))
        high = math.floor(args.target * (1 + args.tolerance))
        return low, high
    if args.min_chars is None and args.max_chars is None:
        raise GuardError("provide --min/--max or --target")
    if args.min_chars is not None and args.min_chars < 0:
        raise GuardError("--min must be non-negative")
    if args.max_chars is not None and args.max_chars < 0:
        raise GuardError("--max must be non-negative")
    if args.min_chars is not None and args.max_chars is not None and args.min_chars > args.max_chars:
        raise GuardError("--min cannot exceed --max")
    return args.min_chars, args.max_chars


def evaluate(path: Path, low: int | None, high: int | None, metric: str) -> dict[str, object]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return {"path": str(path), "status": "invalid", "error": str(exc)}
    counts = measurements(text)
    actual = counts[metric]
    if low is not None and actual < low:
        status = "under"
        delta = low - actual
    elif high is not None and actual > high:
        status = "over"
        delta = actual - high
    else:
        status = "pass"
        delta = 0
    return {
        "path": str(path),
        "metric": metric,
        "minimum": low,
        "maximum": high,
        "actual": actual,
        "status": status,
        "delta_to_band": delta,
        "measurements": counts,
        "body_sha256": hashlib.sha256(extract_body(text).encode("utf-8")).hexdigest(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check fiction chapter length with a stable visible-character metric.")
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--min", dest="min_chars", type=int, help="inclusive lower bound")
    parser.add_argument("--max", dest="max_chars", type=int, help="inclusive upper bound")
    parser.add_argument("--target", type=int, help="target used with --tolerance")
    parser.add_argument("--tolerance", type=float, default=0.05, help="fraction around --target (default: 0.05)")
    parser.add_argument(
        "--metric",
        choices=(METRIC, "han_chars"),
        default=METRIC,
        help=f"counting metric (default: {METRIC})",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        low, high = bounds(args)
    except GuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    results = [evaluate(path, low, high, args.metric) for path in args.paths]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            if result["status"] == "invalid":
                print(f"INVALID {result['path']}: {result['error']}")
                continue
            print(
                f"{str(result['status']).upper()} {result['path']}: "
                f"{result['actual']} {result['metric']} "
                f"(range {result['minimum'] if result['minimum'] is not None else '-inf'}.."
                f"{result['maximum'] if result['maximum'] is not None else '+inf'}, "
                f"delta {result['delta_to_band']})"
            )
    if any(result["status"] == "invalid" for result in results):
        return 2
    return 0 if all(result["status"] == "pass" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
