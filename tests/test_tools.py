#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from chapter_guard import bounds, extract_body, measurements  # noqa: E402
from analysis_workspace import (  # noqa: E402
    WorkspaceError,
    begin_stage,
    complete_stage,
    initialize_workspace,
    recall_index,
    workspace_status,
)
from legacy_inventory import InventoryError, build_inventory, chinese_number, split_accepted_corpus  # noqa: E402
from manuscript_guard import ManuscriptError, evaluate_manuscript  # noqa: E402
from market_fetch import records_from_markdown, records_from_page_context  # noqa: E402
from market_sample import validate_samples  # noqa: E402
from prose_lint import lint, pattern_findings, trailer_findings  # noqa: E402


class ChapterGuardTests(unittest.TestCase):
    def test_frontmatter_and_markdown_title_are_excluded(self) -> None:
        source = "---\ntitle: 测试\n---\n# 第一章 标题\n\n正文。"
        self.assertEqual(extract_body(source).strip(), "正文。")
        self.assertEqual(measurements(source)["visible_chars_v1"], 3)

    def test_plain_chapter_title_is_excluded(self) -> None:
        source = "第001章_军宣新星\n\n正文。"
        self.assertEqual(extract_body(source).strip(), "正文。")
        self.assertEqual(measurements(source)["han_chars"], 2)

    def test_prose_starting_with_chapter_words_is_not_a_title(self) -> None:
        source = "第一章结束了，他才回来。"
        self.assertIn("第一章结束了", extract_body(source))

    def test_comment_before_title_does_not_hide_title(self) -> None:
        source = "<!-- 编辑注释 -->\n第1章 标题\n正文。"
        self.assertEqual(extract_body(source).strip(), "正文。")

    def test_nested_parentheses_in_markdown_link_are_removed(self) -> None:
        source = "# 第一章\n看[资料](https://x.test/a_(b))结束。"
        self.assertEqual(extract_body(source).strip(), "看资料结束。")

    def test_visible_and_han_metrics_remain_distinct(self) -> None:
        source = "# 第一章\n你好，world 12。"
        result = measurements(source)
        self.assertEqual(result["visible_chars_v1"], 11)
        self.assertEqual(result["han_chars"], 2)
        self.assertEqual(result["latin_words"], 1)
        self.assertEqual(result["digits"], 2)

    def test_target_bounds_round_outward(self) -> None:
        args = Namespace(target=3000, tolerance=0.05, min_chars=None, max_chars=None)
        self.assertEqual(bounds(args), (2850, 3150))

    def test_cli_exit_codes(self) -> None:
        script = SCRIPTS / "chapter_guard.py"
        with tempfile.TemporaryDirectory() as directory:
            chapter = Path(directory) / "第001章_测试.md"
            chapter.write_text("第1章 测试\n正文。", encoding="utf-8")
            passed = subprocess.run(
                [sys.executable, str(script), str(chapter), "--min", "3", "--max", "3"],
                check=False,
                capture_output=True,
                text=True,
            )
            under = subprocess.run(
                [sys.executable, str(script), str(chapter), "--min", "4"],
                check=False,
                capture_output=True,
                text=True,
            )
            invalid = subprocess.run(
                [sys.executable, str(script), str(Path(directory) / "missing.md"), "--min", "1"],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertEqual(under.returncode, 1, under.stdout + under.stderr)
        self.assertEqual(invalid.returncode, 2, invalid.stdout + invalid.stderr)


class ProseLintTests(unittest.TestCase):
    def test_finding_line_matches_original_file(self) -> None:
        source = (
            "---\n"
            "title: 测试\n"
            "---\n"
            "<!-- 编辑\n"
            "注释 -->\n"
            "# 第一章\n"
            "\n"
            "他仿佛掉进井里。"
        )
        findings = pattern_findings(extract_body(source))
        generic = next(item for item in findings if item["code"] == "generic-metaphor")
        self.assertEqual(generic["line"], 8)

    def test_trailer_pattern_only_applies_near_ending(self) -> None:
        middle = "他不知道的是，门后有人。\n" + "正常叙事。" * 100
        ending = "正常叙事。" * 100 + "\n他不知道的是，门后有人。"
        self.assertEqual(trailer_findings(middle), [])
        self.assertEqual(len(trailer_findings(ending)), 1)

    def test_blocking_codes_cover_leaks_and_reverse_contrast(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chapter.md"
            path.write_text(
                "第一章\n是胜利，不是失败。没有退路，没有借口。这一夜注定改变一切。细纲写着高潮。TODO 补对话。"
                + ("这段话被原样重复了。" * 2),
                encoding="utf-8",
            )
            report = lint(path)
        codes = {item["code"] for item in report["findings"]}
        self.assertTrue(report["has_blocking"])
        self.assertTrue({"reverse-not-is", "negation-parade", "trailer-summary", "meta-leak", "placeholder-leak"} <= codes)


class LegacyInventoryTests(unittest.TestCase):
    def test_chinese_chapter_numbers(self) -> None:
        self.assertEqual(chinese_number("十二"), 12)
        self.assertEqual(chinese_number("一百零二"), 102)
        self.assertEqual(chinese_number("一万零一"), 10001)

    def test_inventory_reports_gaps_duplicates_and_empty_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "第1章.md").write_text("# 第1章\n甲。", encoding="utf-8")
            (root / "第01章_副本.txt").write_text("第01章 副本\n甲。", encoding="utf-8")
            (root / "第3章.md").write_text("# 第3章\n丙。", encoding="utf-8")
            (root / "第4章.md").write_text("# 第4章\n", encoding="utf-8")
            report = build_inventory(root)

        self.assertEqual(report["status"], "review")
        self.assertEqual(report["summary"]["file_count"], 4)
        self.assertEqual(report["issues"]["missing_chapter_ranges"], [{"start": 2, "end": 2}])
        self.assertEqual(report["issues"]["empty_body_files"], ["第4章.md"])
        self.assertEqual(
            report["issues"]["duplicate_chapter_numbers"][0]["paths"],
            ["第1章.md", "第01章_副本.txt"],
        )
        self.assertEqual(len(report["issues"]["duplicate_contents"]), 1)

    def test_inventory_detects_multiple_chapters_in_one_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "合订本.txt").write_text(
                "第一章 起点\n甲。\n\n第二章 转折\n乙。\n\n第四章 抵达\n丁。",
                encoding="utf-8",
            )
            report = build_inventory(root)

        record = report["files"][0]
        self.assertEqual(record["chapter_sequences"], [1, 2, 4])
        self.assertEqual(record["chapter_count"], 3)
        self.assertEqual([item["line"] for item in record["chapter_candidates"]], [1, 4, 7])
        self.assertEqual(report["issues"]["missing_chapter_ranges"], [{"start": 3, "end": 3}])

    def test_inventory_reports_filename_content_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "第2章.txt").write_text("第一章 起点\n正文。", encoding="utf-8")
            report = build_inventory(root)

        self.assertEqual(
            report["issues"]["filename_content_conflicts"],
            [{"path": "第2章.txt", "filename_chapter_seq": 2, "content_chapter_sequences": [1]}],
        )

    def test_inventory_reports_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "第1章.txt").write_bytes(b"\xff\xfe\x00")
            report = build_inventory(root)

        self.assertEqual(report["issues"]["encoding_or_read_errors"], ["第1章.txt"])
        self.assertEqual(report["files"][0]["status"], "invalid_encoding")

    def test_inventory_cli_exit_codes(self) -> None:
        script = SCRIPTS / "legacy_inventory.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "第1章.txt").write_text("第1章 测试\n正文。", encoding="utf-8")
            clear = subprocess.run(
                [sys.executable, str(script), str(root), "--json"],
                check=False,
                capture_output=True,
                text=True,
            )
            (root / "notes.txt").write_text("作者便笺", encoding="utf-8")
            review = subprocess.run(
                [sys.executable, str(script), str(root)],
                check=False,
                capture_output=True,
                text=True,
            )
            invalid = subprocess.run(
                [sys.executable, str(script), str(root / "missing")],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(clear.returncode, 0, clear.stdout + clear.stderr)
        self.assertEqual(json.loads(clear.stdout)["status"], "clear")
        self.assertEqual(review.returncode, 1, review.stdout + review.stderr)
        self.assertEqual(invalid.returncode, 2, invalid.stdout + invalid.stderr)

    def test_split_staging_cuts_bound_volume_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "合订本.txt"
            original = "第一章 起点\n甲。\n\n第二章 转折\n乙。\n"
            source.write_text(original, encoding="utf-8")
            inventory = build_inventory(root)
            manifest = [
                {
                    "path": "合订本.txt",
                    "sha256": inventory["files"][0]["sha256"],
                    "chapter_seq": 1,
                    "status": "accepted",
                },
                {
                    "path": "合订本.txt",
                    "sha256": inventory["files"][0]["sha256"],
                    "chapter_seq": 2,
                    "status": "accepted",
                },
            ]
            manifest_path = root / "accepted.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            staging = root / "staging"
            report = split_accepted_corpus(root, staging, manifest_path)
            self.assertEqual(len(report["chapters"]), 2)
            first = (staging / report["chapters"][0]["output"]).read_text(encoding="utf-8")
            second = (staging / report["chapters"][1]["output"]).read_text(encoding="utf-8")
            self.assertIn("甲。", first)
            self.assertNotIn("第二章", first)
            self.assertIn("乙。", second)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            with self.assertRaises(InventoryError):
                split_accepted_corpus(root, staging, manifest_path)


class ManuscriptGuardTests(unittest.TestCase):
    def test_multiple_sections_share_one_length_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "01.md"
            second = root / "02.md"
            first.write_text("# 第一节\n甲乙。", encoding="utf-8")
            second.write_text("# 第二节\n丙丁。", encoding="utf-8")
            report = evaluate_manuscript([first, second], 6, 6)

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["actual"], 6)
        self.assertEqual([item["actual"] for item in report["files"]], [3, 3])

    def test_empty_section_and_duplicate_path_are_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            full = root / "01.md"
            empty = root / "02.md"
            full.write_text("第一节\n正文。", encoding="utf-8")
            empty.write_text("第二节\n", encoding="utf-8")
            report = evaluate_manuscript([full, empty], 1, 100)
            with self.assertRaises(ManuscriptError):
                evaluate_manuscript([full, full], 1, 100)

        self.assertEqual(report["status"], "invalid")


class MarketSampleTests(unittest.TestCase):
    def test_live_samples_pass_with_sources_and_multiple_lists(self) -> None:
        records = [
            {
                "platform": "示例平台",
                "list_name": "新书榜" if index < 2 else "畅销榜",
                "captured_at": "2026-09-16T00:00:00+00:00",
                "rank": index + 1,
                "title": f"作品{index}",
                "author": f"作者{index}",
                "url": f"https://example.test/books/{index}",
                "tags": ["悬疑"],
                "metrics": {"heat": 100 - index},
                "source_mode": "live",
            }
            for index in range(4)
        ]
        report = validate_samples(
            records,
            minimum_records=4,
            minimum_lists=2,
            maximum_age_days=30,
            now=datetime(2026, 9, 16, tzinfo=timezone.utc),
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["summary"]["current_eligible_records"], 4)

    def test_historical_or_missing_source_cannot_support_current_claim(self) -> None:
        records = [
            {
                "platform": "示例平台",
                "list_name": "旧榜",
                "captured_at": "2025-01-01T00:00:00+00:00",
                "rank": 1,
                "title": "旧作品",
                "author": None,
                "url": None,
                "tags": [],
                "metrics": {},
                "source_mode": "historical",
            },
            {
                "platform": "示例平台",
                "list_name": "实时榜",
                "captured_at": "2026-09-16T00:00:00+00:00",
                "rank": 2,
                "title": "无来源作品",
                "author": None,
                "url": None,
                "tags": [],
                "metrics": {},
                "source_mode": "live",
            },
        ]
        report = validate_samples(
            records,
            minimum_records=2,
            minimum_lists=1,
            maximum_age_days=30,
            now=datetime(2026, 9, 16, tzinfo=timezone.utc),
        )
        self.assertEqual(report["status"], "block")
        self.assertIn("live-source-without-url", {item["code"] for item in report["errors"]})

    def test_qidian_page_context_fixture_normalizes_to_live_records(self) -> None:
        html = (
            '<script id="vite-plugin-ssr_pageContext">'
            '{"pageContext":{"pageProps":{"pageData":{"records":['
            '{"rankNum":1,"bName":"测试书","bAuth":"作者甲","bid":"1001",'
            '"cat":"玄幻","cnt":"12万","totalRecommend":"3000","desc":"简介"}'
            "]}}}}"
            "</script>"
        )
        records = records_from_page_context(html, list_name="畅销榜", captured_at="2026-09-16T00:00:00+00:00")
        report = validate_samples(
            records,
            minimum_records=1,
            minimum_lists=1,
            maximum_age_days=30,
            now=datetime(2026, 9, 16, tzinfo=timezone.utc),
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(records[0]["url"], "https://m.qidian.com/book/1001/")
        self.assertEqual(records[0]["source_mode"], "live")

    def test_markdown_rank_list_maps_titles_and_urls(self) -> None:
        text = (
            "# 起点 · 月票榜\n"
            "- 抓取时间：2026-09-16T00:00:00+00:00\n"
            "## #1 测试书\n"
            "*作者甲 · 玄幻 · 连载*\n"
            "**标签：** 热血、升级\n"
            "[作品页](https://m.qidian.com/book/1001/)\n"
        )
        records = records_from_markdown(text)
        self.assertEqual(records[0]["title"], "测试书")
        self.assertEqual(records[0]["tags"], ["热血", "升级"])
        self.assertEqual(records[0]["source_mode"], "live")


class AnalysisWorkspaceTests(unittest.TestCase):
    def test_stage_resume_and_required_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            workspace = root / "拆文库" / "测试书"
            source.write_text("第一章 起点\n正文。\n第二章 继续\n正文。", encoding="utf-8")
            initialized = initialize_workspace(workspace, source, "测试书", "long", False)
            self.assertEqual(initialized["status"], "ready")
            self.assertEqual(initialized["next_stage"], 0)

            active = begin_stage(workspace, 0)
            self.assertEqual(active["status"], "in_progress")
            self.assertEqual(workspace_status(workspace)["in_progress_stage"], 0)
            (workspace / "来源.md").write_text("来源与边界。", encoding="utf-8")
            completed = complete_stage(workspace, 0, ["来源.md"])
            self.assertEqual(completed["completed_stages"], [0])
            self.assertEqual(completed["next_stage"], 1)

            begin_stage(workspace, 1)
            (workspace / "wrong.md").write_text("错误产物。", encoding="utf-8")
            with self.assertRaises(WorkspaceError):
                complete_stage(workspace, 1, ["wrong.md"])
            self.assertEqual(workspace_status(workspace)["in_progress_stage"], 1)

    def test_source_drift_blocks_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            workspace = root / "拆文库" / "测试书"
            source.write_text("第一章 起点\n正文。", encoding="utf-8")
            initialize_workspace(workspace, source, "测试书", "long", False)
            source.write_text("第一章 起点\n正文被修改。", encoding="utf-8")
            status = workspace_status(workspace)

        self.assertEqual(status["status"], "block")
        self.assertIn("source hash changed", status["source_error"])

    def test_recall_card_requires_yaml_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            workspace = root / "拆文库" / "测试书"
            source.write_text("第一章 起点\n正文。", encoding="utf-8")
            initialize_workspace(workspace, source, "测试书", "long", False)
            begin_stage(workspace, 0)
            (workspace / "来源.md").write_text("来源。", encoding="utf-8")
            complete_stage(workspace, 0, ["来源.md"])
            begin_stage(workspace, 1)
            (workspace / "开篇.md").write_text("开篇。", encoding="utf-8")
            complete_stage(workspace, 1, ["开篇.md"])
            begin_stage(workspace, 2)
            (workspace / "章节").mkdir()
            (workspace / "章节" / "第1章.md").write_text("入口与选择。", encoding="utf-8")
            complete_stage(workspace, 2, ["章节/第1章.md"])
            begin_stage(workspace, 3)
            (workspace / "剧情").mkdir()
            (workspace / "剧情" / "单元.md").write_text("## 单元一\n冲突。", encoding="utf-8")
            (workspace / "剧情" / "节奏.md").write_text("没有条目的说明。", encoding="utf-8")
            (workspace / "剧情" / "情绪与承诺.md").write_text("## 承诺\n等待答案。", encoding="utf-8")
            with self.assertRaises(WorkspaceError):
                complete_stage(workspace, 3, ["剧情/单元.md", "剧情/节奏.md", "剧情/情绪与承诺.md"])
            (workspace / "剧情" / "节奏.md").write_text("## RH-001\n加压后兑现。", encoding="utf-8")
            complete_stage(workspace, 3, ["剧情/单元.md", "剧情/节奏.md", "剧情/情绪与承诺.md"])
            for stage, files in (
                (4, {"人物与关系.md": "人物。", "设定与机制.md": "设定。", "章法.md": "章法。"}),
                (5, {"文风.md": "文风。"}),
            ):
                begin_stage(workspace, stage)
                artifacts = []
                for name, text in files.items():
                    (workspace / name).write_text(text, encoding="utf-8")
                    artifacts.append(name)
                complete_stage(workspace, stage, artifacts)
            begin_stage(workspace, 6)
            (workspace / "拆文报告.md").write_text("报告。", encoding="utf-8")
            (workspace / "召回卡.md").write_text("只有散文，没有字段。", encoding="utf-8")
            with self.assertRaises(WorkspaceError):
                complete_stage(workspace, 6, ["召回卡.md", "拆文报告.md"])
            (workspace / "召回卡.md").write_text(
                "```yaml\n"
                "id: RC-001\n"
                "function: 用可见代价兑现开篇承诺\n"
                "conditions: 主角已有具体缺口\n"
                "source: 第1章\n"
                "similarity_risk: 换名即可复刻则失败\n"
                "failure_mode: 只宣布金手指没有后果\n"
                "confidence: high\n"
                "```\n",
                encoding="utf-8",
            )
            complete_stage(workspace, 6, ["召回卡.md", "拆文报告.md"])
            recalled = recall_index(workspace, "planning")
            self.assertEqual(recalled["missing_recall"], [])
            self.assertTrue(any(item["path"] == "召回卡.md" for item in recalled["recall"]))


if __name__ == "__main__":
    unittest.main()
