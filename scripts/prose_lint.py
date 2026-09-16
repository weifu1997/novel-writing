#!/usr/bin/env python3
"""Advisory prose-pattern linter. It does not estimate whether text is AI-written."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from chapter_guard import extract_body, measurements


PATTERNS = (
    ("not-but", re.compile(r"不是[^。！？\n]{0,24}(?:，|,)?\s*而是"), "工整否定转折，检查是否在替读者总结"),
    ("voice-contrast", re.compile(r"(?:声音|嗓音|语气)(?:并)?不(?:大|高|重)[^。！？\n]{0,18}(?:却|但)"), "高频声线反差模板"),
    ("explanation", re.compile(r"这意味着|换句话说|由此可见|显而易见|毋庸置疑|值得一提的是"), "叙述可能转成解释或论文口吻"),
    ("mind-summary", re.compile(r"[他她我](?:终于)?(?:明白|意识到|懂得)了?[^。！？\n]{0,28}"), "检查是否已由动作或后果表达"),
    ("stock-reaction", re.compile(r"深吸(?:了)?一口气|眼中闪过一丝|眸光微闪|嘴角(?:微微)?勾起(?:一抹)?|指节泛白|心头一震|心中一动|缓缓开口"), "库存反应动作，按功能做删除测试"),
    ("generic-metaphor", re.compile(r"仿佛[^。！？\n]{0,18}(?:一般|一样|似的)?|宛如[^。！？\n]{0,18}"), "检查比喻是否具体且来自人物经验"),
)
BLOCKING_PATTERNS = (
    ("reverse-not-is", re.compile(r"是[^。！？\n]{0,18}(?:，|,)?\s*不是"), "先肯定后否定的工整对比，检查是否在替读者总结"),
    ("negation-parade", re.compile(r"没有[^。！？\n]{0,12}(?:，|,)\s*没有"), "连续否定铺垫，检查是否可直接写后项"),
)
TRAILER_ENDING = re.compile(
    r"[他她]不知道的是|没人知道[^。！？\n]{0,24}|这才刚刚开始|"
    r"(?:真正的)?风暴(?:即将|才刚)|命运的齿轮"
)
TRAILER_SUMMARY = re.compile(
    r"这一夜注定|这一切都(?:结束|说明)|新的(?:人生|篇章)才刚刚开始|他终于明白"
)
PLACEHOLDER_LEAK = re.compile(r"TODO|FIXME|TBD|\[占位\]|【待补】|\bxxx\b", re.I)
META_LEAK = re.compile(r"细纲|情节点|字数合约|章法执行卡|Genre Contract|Style Contract")
TAIL_VISIBLE_CHARS = 300


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _match_findings(
    body: str,
    patterns: tuple[tuple[str, re.Pattern[str], str], ...],
    severity: str,
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for code, pattern, message in patterns:
        for match in pattern.finditer(body):
            snippet = re.sub(r"\s+", " ", match.group(0)).strip()
            findings.append(
                {
                    "code": code,
                    "severity": severity,
                    "line": line_number(body, match.start()),
                    "snippet": snippet[:80],
                    "message": message,
                }
            )
    return findings


def pattern_findings(body: str) -> list[dict[str, object]]:
    return _match_findings(body, PATTERNS, "advisory") + _match_findings(body, BLOCKING_PATTERNS, "blocking")


def _tail_start(text: str, visible_limit: int = TAIL_VISIBLE_CHARS) -> int:
    visible = 0
    for index in range(len(text) - 1, -1, -1):
        if not text[index].isspace():
            visible += 1
            if visible >= visible_limit:
                return index
    return 0


def trailer_findings(body: str) -> list[dict[str, object]]:
    start = _tail_start(body)
    findings: list[dict[str, object]] = []
    for match in TRAILER_ENDING.finditer(body, start):
        findings.append(
            {
                "code": "trailer-ending",
                "severity": "advisory",
                "line": line_number(body, match.start()),
                "snippet": re.sub(r"\s+", " ", match.group(0)).strip()[:80],
                "message": "章尾 300 个可见字符内出现预告式或上帝视角收束",
            }
        )
    for match in TRAILER_SUMMARY.finditer(body, start):
        findings.append(
            {
                "code": "trailer-summary",
                "severity": "blocking",
                "line": line_number(body, match.start()),
                "snippet": re.sub(r"\s+", " ", match.group(0)).strip()[:80],
                "message": "章尾出现总结/升华收束，改为动作、画面或未完成问题",
            }
        )
    return findings


def leak_findings(body: str) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for match in PLACEHOLDER_LEAK.finditer(body):
        findings.append(
            {
                "code": "placeholder-leak",
                "severity": "blocking",
                "line": line_number(body, match.start()),
                "snippet": match.group(0),
                "message": "正文混入占位符，应重写该句而不是润色",
            }
        )
    for match in META_LEAK.finditer(body):
        findings.append(
            {
                "code": "meta-leak",
                "severity": "blocking",
                "line": line_number(body, match.start()),
                "snippet": match.group(0),
                "message": "正文泄漏写作工程词，应删掉或改成故事内表达",
            }
        )
    return findings


def verbatim_repeat_findings(body: str) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for match in re.finditer(r"(.{12,80})\1", body, re.S):
        snippet = re.sub(r"\s+", " ", match.group(1)).strip()
        if len(snippet) < 12:
            continue
        findings.append(
            {
                "code": "verbatim-repeat",
                "severity": "blocking",
                "line": line_number(body, match.start()),
                "snippet": snippet[:80],
                "message": "连续重复同一文本块，视为生成退化而不是风格问题",
            }
        )
    return findings


def sentence_openers(body: str) -> list[dict[str, object]]:
    sentences = [part.strip() for part in re.split(r"[。！？!?]+", body) if part.strip()]
    openers = []
    for sentence in sentences:
        han = re.findall(r"[\u3400-\u9fff]", sentence)
        if len(han) >= 2:
            openers.append("".join(han[:2]))
    findings: list[dict[str, object]] = []
    for opener, count in Counter(openers).most_common():
        if count >= 5 and opener not in {"第十", "第二", "第一"}:
            findings.append(
                {
                    "code": "repeated-opener",
                    "severity": "advisory",
                    "line": None,
                    "snippet": opener,
                    "message": f"{count} 个句子以相同两字开头；检查主语或句式是否机械重复",
                }
            )
    return findings


def uniform_paragraphs(body: str) -> list[dict[str, object]]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", body) if p.strip()]
    lengths = [measurements(p)["visible_chars_v1"] for p in paragraphs]
    if len(lengths) < 8:
        return []
    mean = sum(lengths) / len(lengths)
    variance = sum((length - mean) ** 2 for length in lengths) / len(lengths)
    cv = variance**0.5 / mean if mean else 0.0
    if mean >= 12 and cv < 0.22:
        return [
            {
                "code": "uniform-paragraphs",
                "severity": "advisory",
                "line": None,
                "snippet": f"paragraphs={len(lengths)}, mean={mean:.1f}, cv={cv:.2f}",
                "message": "段落长度过齐；按镜头和信息变化复核，不要为参差机械拆段",
            }
        ]
    return []


def lint(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return {"path": str(path), "status": "invalid", "error": str(exc), "findings": []}
    body = extract_body(text)
    findings = (
        pattern_findings(body)
        + trailer_findings(body)
        + leak_findings(body)
        + verbatim_repeat_findings(body)
        + sentence_openers(body)
        + uniform_paragraphs(body)
    )
    findings.sort(key=lambda item: (item["line"] is None, item["line"] or 0, str(item["code"])))
    blocking = any(item["severity"] == "blocking" for item in findings)
    return {
        "path": str(path),
        "status": "review" if findings else "clear",
        "visible_chars_v1": measurements(text)["visible_chars_v1"],
        "finding_count": len(findings),
        "blocking_count": sum(1 for item in findings if item["severity"] == "blocking"),
        "note": "Findings are not an AI probability and require contextual review.",
        "findings": findings,
        "has_blocking": blocking,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Locate formulaic prose patterns for contextual review.")
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--fail-on", choices=("advisory", "blocking"), default="blocking")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reports = [lint(path) for path in args.paths]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for report in reports:
            print(f"{str(report['status']).upper()} {report['path']}: {len(report['findings'])} finding(s)")
            if report["status"] == "invalid":
                print(f"  error: {report['error']}")
                continue
            for finding in report["findings"]:
                where = f"line {finding['line']}" if finding["line"] is not None else "document"
                print(f"  {where} [{finding['severity']}/{finding['code']}] {finding['message']}: {finding['snippet']}")
            print("  note: findings require contextual review; do not optimize for a score")
    if any(report["status"] == "invalid" for report in reports):
        return 2
    if args.fail_on == "advisory" and any(report["findings"] for report in reports):
        return 1
    if any(report.get("has_blocking") for report in reports):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
