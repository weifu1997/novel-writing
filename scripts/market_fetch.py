#!/usr/bin/env python3
"""Normalize public ranking pages or pasted Markdown into market_sample records."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from market_sample import SampleError, validate_samples


MOBILE_BASE = "https://m.qidian.com"
PAGECONTEXT = re.compile(
    r'<script[^>]+id=["\']vite-plugin-ssr_pageContext["\'][^>]*>([\s\S]*?)</script>',
    re.I,
)
QIDIAN_LISTS = {
    "hotsales": ("畅销榜", "/rank/hotsales/"),
    "yuepiao": ("月票榜", "/rank/yuepiao/"),
    "signnewbook": ("签约作者新书榜", "/rank/sign/"),
    "newsign": ("新人签约新书榜", "/rank/sign/"),
}
HEADING_RANK = re.compile(r"^##\s+#(\d+)\s+(.+?)\s*$")
META_LINE = re.compile(r"^\*(.+)\*\s*$")
FIELD_LINE = re.compile(r"^\*\*(.+?)：\*\*\s*(.+?)\s*$")
LINK_LINE = re.compile(r"^\[作品页\]\((https?://[^)]+)\)\s*$")
CAPTURED = re.compile(r"抓取时间[：:]\s*(\S+)")
LIST_NAME = re.compile(r"^#\s+(.+?)\s*$")
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _first(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def records_from_page_context(
    html: str,
    *,
    list_name: str,
    captured_at: str | None = None,
    platform: str = "起点",
) -> list[dict[str, Any]]:
    match = PAGECONTEXT.search(html)
    if not match:
        raise SampleError("qidian mobile HTML is missing vite-plugin-ssr_pageContext")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SampleError(f"qidian pageContext is not valid JSON: {exc}") from exc
    page_data = (
        payload.get("pageContext", {}).get("pageProps", {}).get("pageData")
        if isinstance(payload, dict)
        else None
    )
    raw_records = page_data.get("records") if isinstance(page_data, dict) else None
    if not isinstance(raw_records, list) or not raw_records:
        raise SampleError("qidian pageContext has no records[]")
    captured = captured_at or utc_now()
    records: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, dict):
            continue
        title = str(_first(raw, "bName", "bookName") or "").strip()
        book_id = str(_first(raw, "bid", "bookId") or "").strip()
        if not title:
            continue
        rank = raw.get("rankNum", index + 1)
        try:
            rank = int(rank)
        except (TypeError, ValueError):
            rank = index + 1
        url = f"{MOBILE_BASE}/book/{book_id}/" if book_id else None
        metrics: dict[str, Any] = {}
        for key, source in (
            ("words", ("cnt", "wordCount", "words", "wordCnt")),
            ("totalRecommendations", ("totalRecommend", "totalRecommendations", "recommendCount", "totalRec")),
            ("rankValue", ("rankCnt", "rankValue")),
            ("signing", ("signStatus", "signing", "contractStatus")),
            ("pricing", ("vipStatus", "pricing", "chargeStatus")),
        ):
            value = _first(raw, *source)
            if value not in (None, ""):
                metrics[key] = value
        records.append(
            {
                "platform": platform,
                "list_name": list_name,
                "captured_at": captured,
                "rank": rank,
                "title": title,
                "author": str(_first(raw, "bAuth", "author") or "") or None,
                "url": url,
                "category": "·".join(str(item) for item in (raw.get("cat"), raw.get("subCat")) if item) or None,
                "status": str(_first(raw, "status", "bookStatus", "serializationStatus") or "") or None,
                "tags": [],
                "metrics": metrics,
                "synopsis": str(raw.get("desc") or "") or None,
                "source_mode": "live" if url else "user_provided",
            }
        )
    if not records:
        raise SampleError("qidian pageContext produced no titled records")
    return records


def records_from_markdown(text: str, *, source_mode: str = "user_provided") -> list[dict[str, Any]]:
    lines = text.splitlines()
    list_name = "未命名榜单"
    captured_at = utc_now()
    platform = "未知平台"
    heading = LIST_NAME.match(lines[0].strip()) if lines else None
    if heading:
        title = heading.group(1)
        if "·" in title:
            platform, list_name = [part.strip() for part in title.split("·", 1)]
        else:
            list_name = title
    captured = CAPTURED.search(text)
    if captured:
        captured_at = captured.group(1)
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines:
        ranked = HEADING_RANK.match(line)
        if ranked:
            if current:
                records.append(current)
            current = {
                "platform": platform,
                "list_name": list_name,
                "captured_at": captured_at,
                "rank": int(ranked.group(1)),
                "title": ranked.group(2).strip(),
                "author": None,
                "url": None,
                "tags": [],
                "metrics": {},
                "source_mode": source_mode,
            }
            continue
        if current is None:
            continue
        meta = META_LINE.match(line)
        if meta:
            parts = [part.strip() for part in meta.group(1).split("·") if part.strip()]
            if parts:
                current["author"] = parts[0]
            if len(parts) > 1:
                current["category"] = parts[1]
            if len(parts) > 2:
                current["status"] = parts[2]
            continue
        field = FIELD_LINE.match(line)
        if field:
            name, value = field.group(1).strip(), field.group(2).strip()
            if name == "标签":
                current["tags"] = [item for item in re.split(r"[、,，]", value) if item]
            else:
                current["metrics"][name] = None if value in {"[待补]", "待补"} else value
            continue
        link = LINK_LINE.match(line)
        if link:
            current["url"] = link.group(1)
            if source_mode == "user_provided":
                current["source_mode"] = "live"
    if current:
        records.append(current)
    if not records:
        raise SampleError("markdown contains no ## #N title headings")
    return records


def fetch_url(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read()
    except (URLError, TimeoutError, OSError) as exc:
        raise SampleError(f"unable to fetch {url}: {exc}") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise SampleError(f"fetched page is not UTF-8: {exc}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Turn a public ranking page or pasted list into market_sample JSON.")
    parser.add_argument("--html-file", type=Path, help="frozen qidian mobile HTML fixture")
    parser.add_argument("--markdown-file", type=Path, help="pasted ranking Markdown")
    parser.add_argument("--qidian-mobile", choices=sorted(QIDIAN_LISTS), help="fetch a public m.qidian.com list")
    parser.add_argument("--list-name", help="override list_name for HTML fixtures")
    parser.add_argument("--min-records", type=int, default=10)
    parser.add_argument("--min-lists", type=int, default=1)
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.markdown_file is not None:
            records = records_from_markdown(args.markdown_file.read_text(encoding="utf-8"))
        elif args.html_file is not None:
            list_name = args.list_name or "畅销榜"
            records = records_from_page_context(args.html_file.read_text(encoding="utf-8"), list_name=list_name)
        elif args.qidian_mobile:
            list_name, path = QIDIAN_LISTS[args.qidian_mobile]
            records = records_from_page_context(fetch_url(MOBILE_BASE + path), list_name=list_name)
        else:
            raise SampleError("provide --html-file, --markdown-file, or --qidian-mobile")
        report = validate_samples(
            records,
            minimum_records=args.min_records,
            minimum_lists=args.min_lists,
            maximum_age_days=args.max_age_days,
        )
    except (SampleError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report["summary"]
        print(
            f"{report['status'].upper()}: {summary['valid_records']} valid record(s), "
            f"{summary['distinct_lists']} list(s)"
        )
    return 1 if report["status"] == "block" else 0


if __name__ == "__main__":
    raise SystemExit(main())
