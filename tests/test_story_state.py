#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
TOOL = SCRIPTS / "story_state.py"
sys.path.insert(0, str(SCRIPTS))

from story_state import style_drift_findings  # noqa: E402


class StoryStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "百万字测试书"
        (self.project / "正文").mkdir(parents=True)
        (self.project / "追踪" / "逐章记录").mkdir(parents=True)
        self.run_tool("init", "--title", "百万字测试书")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @property
    def database(self) -> Path:
        return self.project / "追踪" / "story-state.sqlite3"

    def run_tool(
        self,
        command: str,
        *extra: str,
        expect: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [sys.executable, str(TOOL), command, "--project", str(self.project), "--json", *extra],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            expect,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        return completed

    def write_inputs(self, seq: int, body: str, delta: dict[str, object], record: str | None = None) -> tuple[Path, Path, Path]:
        body_path = self.project / "正文" / f"第{seq:03d}章_测试.md"
        record_path = self.project / "追踪" / "逐章记录" / f"第{seq:03d}章.md"
        delta_path = Path(self.temporary.name) / f"delta-{seq}-{len(list(Path(self.temporary.name).glob('delta-*')))}.json"
        body_path.write_text(body, encoding="utf-8")
        record_path.write_text(record if record is not None else f"# 第{seq}章\n- 已发生：测试事件。\n", encoding="utf-8")
        delta_path.write_text(json.dumps(delta, ensure_ascii=False), encoding="utf-8")
        return body_path, record_path, delta_path

    def commit(self, inputs: tuple[Path, Path, Path], *, expect: int = 0) -> subprocess.CompletedProcess[str]:
        body, record, delta = inputs
        return self.run_tool(
            "commit",
            "--body",
            str(body),
            "--record",
            str(record),
            "--delta",
            str(delta),
            expect=expect,
        )

    def base_delta(self, seq: int, revision: int, *, mode: str = "append") -> dict[str, object]:
        return {
            "schema_version": 1,
            "expected_revision": revision,
            "length_contract": {"metric": "visible_chars_v1", "minimum": 1, "maximum": 1000},
            "chapter": {
                "id": f"ch-{seq:03d}",
                "seq": seq,
                "title": f"测试第{seq}章",
                "mode": mode,
                "summary": f"第{seq}章发生了可验证的变化。",
                "volume": {
                    "id": "vol-01",
                    "seq": 1,
                    "title": "第一卷",
                    "planned_end_chapter": 10,
                    "status": "active",
                    "state": {},
                },
            },
            "entity_changes": [],
            "thread_changes": [],
            "timeline_changes": [],
            "references": [],
            "arc_beats": [],
        }

    def first_chapter(self, *, due_chapter: int = 5) -> tuple[Path, Path, Path]:
        evidence = "林舟站在旧宅门前，摸了摸口袋里的铜钥匙。"
        body = f"# 第一章 归来\n\n{evidence}\n"
        delta = self.base_delta(1, 0)
        delta["entity_changes"] = [
            {"op": "create", "id": "loc.old-home", "type": "location", "name": "旧宅", "state": {}, "evidence": evidence},
            {
                "op": "create",
                "id": "char.lin-zhou",
                "type": "character",
                "name": "林舟",
                "state": {"role": "main", "location_id": "loc.old-home", "goal": "查明父亲失踪真相"},
                "evidence": evidence,
            },
            {
                "op": "create",
                "id": "item.copper-key",
                "type": "item",
                "name": "铜钥匙",
                "state": {"holder_id": "char.lin-zhou", "location_id": "loc.old-home"},
                "evidence": evidence,
            },
        ]
        delta["thread_changes"] = [
            {
                "op": "create",
                "id": "F001",
                "state": {
                    "type": "foreshadow",
                    "summary": "铜钥匙能打开父亲留下的密室",
                    "status": "open",
                    "importance": "high",
                    "introduced_chapter": 1,
                    "due_chapter": due_chapter,
                    "due_volume_id": "vol-01",
                    "last_progress_chapter": 1,
                    "state": {"related_ids": ["item.copper-key", "char.lin-zhou"]},
                },
                "evidence": evidence,
            }
        ]
        delta["timeline_changes"] = [
            {
                "op": "create",
                "id": "E001",
                "state": {
                    "sequence": 1,
                    "story_time": "故事第一天清晨",
                    "mode": "present",
                    "fact": "林舟抵达旧宅",
                    "location_id": "loc.old-home",
                    "participant_ids": ["char.lin-zhou"],
                    "state": {},
                },
                "evidence": evidence,
            }
        ]
        delta["arc_beats"] = [
            {
                "character_id": "char.lin-zhou",
                "dimension": "goal",
                "beat": "从逃避旧宅转为主动调查",
                "before": "逃避",
                "after": "调查",
                "evidence": evidence,
            }
        ]
        return self.write_inputs(1, body, delta)

    def test_commit_stores_structured_state_and_exact_artifacts(self) -> None:
        inputs = self.first_chapter()
        body, record, delta = inputs
        checked = self.run_tool(
            "check", "--body", str(body), "--record", str(record), "--delta", str(delta)
        )
        checked_payload = json.loads(checked.stdout)
        self.assertEqual(checked_payload["changes"].__len__(), 5)
        self.assertIsNone(checked_payload["changes"][0]["before"])
        self.assertEqual(checked_payload["changes"][0]["after"]["name"], "旧宅")
        committed = json.loads(self.commit(inputs).stdout)
        self.assertEqual(committed["state_revision"], 1)

        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM story_threads").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0], 1)
            snapshot = connection.execute("SELECT body_content, record_content FROM chapter_versions").fetchone()
        self.assertEqual(snapshot[0], body.read_text(encoding="utf-8"))
        self.assertEqual(snapshot[1], record.read_text(encoding="utf-8"))
        verified = json.loads(self.run_tool("verify").stdout)
        self.assertEqual(verified["status"], "pass")

    def test_stale_revision_and_failed_change_leave_no_partial_state(self) -> None:
        self.commit(self.first_chapter())
        evidence = "林舟把钥匙收进内袋。"
        delta = self.base_delta(2, 0)
        delta["entity_changes"] = [
            {"op": "create", "id": "item.new", "type": "item", "name": "新物品", "state": {}, "evidence": evidence}
        ]
        stale = self.write_inputs(2, evidence, delta)
        self.commit(stale, expect=2)

        delta["expected_revision"] = 1
        delta["entity_changes"].append(
            {
                "op": "set",
                "id": "char.lin-zhou",
                "path": "/state/location_id",
                "expect": "loc.somewhere-else",
                "value": "loc.old-home",
                "evidence": evidence,
            }
        )
        conflict = self.write_inputs(2, evidence, delta)
        self.commit(conflict, expect=2)
        with sqlite3.connect(self.database) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM entities WHERE entity_id = 'item.new'").fetchone())
            self.assertEqual(json.loads(connection.execute("SELECT value FROM project_meta WHERE key = 'state_revision'").fetchone()[0]), 1)

    def test_dependency_impact_revision_and_rollback(self) -> None:
        self.commit(self.first_chapter(due_chapter=9))
        evidence = "林舟离开旧宅，走进县城档案馆。"
        delta = self.base_delta(2, 1)
        delta["entity_changes"] = [
            {"op": "create", "id": "loc.archive", "type": "location", "name": "档案馆", "state": {}, "evidence": evidence},
            {
                "op": "set",
                "id": "char.lin-zhou",
                "path": "/state/location_id",
                "expect": "loc.old-home",
                "value": "loc.archive",
                "evidence": evidence,
            },
            {
                "op": "set",
                "id": "item.copper-key",
                "path": "/state/location_id",
                "expect": "loc.old-home",
                "value": "loc.archive",
                "evidence": evidence,
            },
        ]
        delta["references"] = [
            {"kind": "entity", "id": "char.lin-zhou", "fields": ["/state/location_id"], "reason": "人物移动承接上一章", "evidence": evidence}
        ]
        self.commit(self.write_inputs(2, evidence, delta))

        impact = json.loads(self.run_tool("impact", "--chapter", "ch-001").stdout)
        self.assertEqual([item["chapter_id"] for item in impact["impacted_chapters"]], ["ch-002"])

        revised_body = "# 第一章 归来\n\n林舟站在旧宅门前，决定当天就查清父亲的下落。\n"
        revised = self.base_delta(1, 2, mode="revision")
        revised["chapter"]["continuity_changed"] = True
        revision_inputs = self.write_inputs(1, revised_body, revised)
        committed = json.loads(self.commit(revision_inputs).stdout)
        self.assertEqual(committed["impacted_chapters"], ["ch-002"])
        self.run_tool("verify", expect=1)

        rolled_back = json.loads(self.run_tool("rollback", "--commit", str(committed["commit_id"])).stdout)
        self.assertEqual(rolled_back["status"], "rolled_back")
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT needs_review FROM chapters WHERE chapter_id = 'ch-002'").fetchone()[0], 0)

    def test_timeline_double_location_is_rejected(self) -> None:
        self.commit(self.first_chapter())
        evidence = "同一时刻，林舟已经出现在县城。"
        delta = self.base_delta(2, 1)
        delta["entity_changes"] = [
            {"op": "create", "id": "loc.county", "type": "location", "name": "县城", "state": {}, "evidence": evidence}
        ]
        delta["timeline_changes"] = [
            {
                "op": "create",
                "id": "E002",
                "state": {
                    "sequence": 1,
                    "story_time": "故事第一天清晨",
                    "mode": "present",
                    "fact": "林舟出现在县城",
                    "location_id": "loc.county",
                    "participant_ids": ["char.lin-zhou"],
                    "state": {},
                },
                "evidence": evidence,
            }
        ]
        self.commit(self.write_inputs(2, evidence, delta), expect=2)

    def test_present_timeline_regression_is_rejected(self) -> None:
        self.commit(self.first_chapter())
        evidence = "林舟仍站在旧宅门口。"
        delta = self.base_delta(2, 1)
        delta["timeline_changes"] = [
            {
                "op": "create",
                "id": "E002",
                "state": {
                    "sequence": 0.5,
                    "story_time": "故事开始前半小时",
                    "mode": "present",
                    "fact": "林舟站在旧宅门口",
                    "location_id": "loc.old-home",
                    "participant_ids": ["char.lin-zhou"],
                    "state": {},
                },
                "evidence": evidence,
            }
        ]
        self.commit(self.write_inputs(2, evidence, delta), expect=2)

    def test_rollback_restores_global_state_and_accepted_chapter_pointer(self) -> None:
        self.commit(self.first_chapter())
        evidence = "林舟带着铜钥匙走进县城档案馆。"
        delta = self.base_delta(2, 1)
        delta["entity_changes"] = [
            {"op": "create", "id": "loc.archive", "type": "location", "name": "档案馆", "state": {}, "evidence": evidence},
            {"op": "set", "id": "char.lin-zhou", "path": "/state/location_id", "expect": "loc.old-home", "value": "loc.archive", "evidence": evidence},
            {"op": "set", "id": "item.copper-key", "path": "/state/location_id", "expect": "loc.old-home", "value": "loc.archive", "evidence": evidence},
        ]
        committed = json.loads(self.commit(self.write_inputs(2, evidence, delta)).stdout)
        self.run_tool("rollback", "--commit", str(committed["commit_id"]))
        with sqlite3.connect(self.database) as connection:
            state = json.loads(connection.execute("SELECT state_json FROM entities WHERE entity_id = 'char.lin-zhou'").fetchone()[0])
            self.assertEqual(state["location_id"], "loc.old-home")
            self.assertIsNone(connection.execute("SELECT 1 FROM entities WHERE entity_id = 'loc.archive'").fetchone())
            self.assertIsNone(connection.execute("SELECT 1 FROM chapters WHERE chapter_id = 'ch-002'").fetchone())

    def test_checkout_restores_snapshot_and_preserves_working_copy_backup(self) -> None:
        inputs = self.first_chapter()
        body, _, _ = inputs
        original = body.read_text(encoding="utf-8")
        self.commit(inputs)
        body.write_text("未提交的修改", encoding="utf-8")
        self.run_tool("verify", expect=1)
        checked_out = json.loads(self.run_tool("checkout", "--chapter", "ch-001").stdout)
        self.assertEqual(body.read_text(encoding="utf-8"), original)
        self.assertEqual(len(checked_out["backups"]), 1)
        self.assertTrue(Path(checked_out["backups"][0]).is_file())

    def test_length_contract_rejects_empty_and_out_of_band_artifacts(self) -> None:
        delta = self.base_delta(1, 0)
        self.commit(self.write_inputs(1, "", delta), expect=2)
        self.commit(self.write_inputs(1, "正文", delta, record=""), expect=2)

        delta["length_contract"] = {"metric": "visible_chars_v1", "minimum": 3, "maximum": 4}
        self.commit(self.write_inputs(1, "正文", delta), expect=2)
        self.commit(self.write_inputs(1, "正文过长了", delta), expect=2)

        accepted = self.write_inputs(1, "正文正好", delta)
        committed = json.loads(self.commit(accepted).stdout)
        self.assertEqual(committed["length"]["actual"], 4)
        with sqlite3.connect(self.database) as connection:
            stored = json.loads(connection.execute("SELECT length_json FROM chapter_versions").fetchone()[0])
        self.assertEqual(stored["status"], "pass")
        self.assertEqual(stored["source_body_sha256"], committed["body_sha256"])

    def test_commit_rechecks_length_after_check(self) -> None:
        delta = self.base_delta(1, 0)
        delta["length_contract"] = {"metric": "visible_chars_v1", "minimum": 2, "maximum": 2}
        inputs = self.write_inputs(1, "正文", delta)
        self.run_tool("check", "--body", str(inputs[0]), "--record", str(inputs[1]), "--delta", str(inputs[2]))
        inputs[0].write_text("正文已经变化", encoding="utf-8")
        self.commit(inputs, expect=2)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM commits").fetchone()[0], 0)

    def test_volume_audit_blocks_overdue_plot_debt(self) -> None:
        inputs = self.first_chapter(due_chapter=1)
        delta = json.loads(inputs[2].read_text(encoding="utf-8"))
        delta["chapter"]["volume"]["planned_end_chapter"] = 1
        inputs[2].write_text(json.dumps(delta, ensure_ascii=False), encoding="utf-8")
        committed = json.loads(self.commit(inputs).stdout)
        self.assertEqual(committed["status"], "committed_with_audit_blockers")
        self.assertEqual(committed["volume_audit"]["status"], "block")
        audit = json.loads(self.run_tool("audit-volume", "--volume", "vol-01", expect=1).stdout)
        self.assertEqual(audit["status"], "block")
        self.assertIn("overdue-thread", {item["code"] for item in audit["findings"]})
        self.assertIn("char.lin-zhou", audit["character_arcs"])

    def test_volume_audit_blocker_prevents_next_volume_until_reaudit_passes(self) -> None:
        inputs = self.first_chapter(due_chapter=1)
        delta = json.loads(inputs[2].read_text(encoding="utf-8"))
        delta["chapter"]["volume"]["planned_end_chapter"] = 1
        inputs[2].write_text(json.dumps(delta, ensure_ascii=False), encoding="utf-8")
        blocked = json.loads(self.commit(inputs).stdout)
        self.assertEqual(blocked["volume_audit"]["status"], "block")
        blocked_audit_id = blocked["volume_audit"]["audit"]["id"]

        next_delta = self.base_delta(2, 1)
        next_delta["chapter"]["volume"] = {
            "id": "vol-02", "seq": 2, "title": "第二卷", "planned_end_chapter": 20,
            "status": "active", "state": {},
        }
        self.commit(self.write_inputs(2, "林舟走进第二卷的清晨。", next_delta), expect=2)

        evidence = "铜钥匙已经打开密室，旧日疑问在此得到回答。"
        revision = self.base_delta(1, 1, mode="revision")
        revision["chapter"]["volume"]["planned_end_chapter"] = 1
        revision["thread_changes"] = [
            {"op": "set", "id": "F001", "path": "/status", "expect": "open", "value": "resolved", "evidence": evidence}
        ]
        resolved = json.loads(self.commit(self.write_inputs(1, evidence, revision)).stdout)
        self.assertEqual(resolved["volume_audit"]["status"], "pass")
        historical = json.loads(
            self.run_tool(
                "audit-volume", "--volume", "vol-01", "--audit-id", str(blocked_audit_id), expect=1
            ).stdout
        )
        self.assertEqual(historical["status"], "block")
        self.assertEqual(historical["audit"]["id"], blocked_audit_id)
        refreshed = json.loads(
            self.run_tool("audit-volume", "--volume", "vol-01", "--refresh").stdout
        )
        self.assertEqual(refreshed["status"], "pass")
        self.assertNotEqual(refreshed["audit"]["id"], resolved["volume_audit"]["audit"]["id"])
        self.assertEqual(json.loads(self.commit(self.write_inputs(2, "林舟走进第二卷的清晨。", self.base_delta_for_volume(2, 2))).stdout)["status"], "committed")

    def base_delta_for_volume(self, seq: int, revision: int) -> dict[str, object]:
        delta = self.base_delta(seq, revision)
        delta["chapter"]["volume"] = {
            "id": "vol-02", "seq": 2, "title": "第二卷", "planned_end_chapter": 20,
            "status": "active", "state": {},
        }
        return delta

    def test_stored_volume_audit_is_not_rewritten_by_future_state(self) -> None:
        inputs = self.first_chapter(due_chapter=99)
        delta = json.loads(inputs[2].read_text(encoding="utf-8"))
        delta["chapter"]["volume"]["planned_end_chapter"] = 1
        delta["thread_changes"][0]["state"]["due_volume_id"] = None
        inputs[2].write_text(json.dumps(delta, ensure_ascii=False), encoding="utf-8")
        first = json.loads(self.commit(inputs).stdout)["volume_audit"]
        self.assertEqual(first["plot_threads"][0]["status"], "open")

        evidence = "铜钥匙的秘密已经在第二卷开端得到回答。"
        second = self.base_delta_for_volume(2, 1)
        second["thread_changes"] = [
            {"op": "set", "id": "F001", "path": "/status", "expect": "open", "value": "resolved", "evidence": evidence}
        ]
        self.commit(self.write_inputs(2, evidence, second))
        historical = json.loads(self.run_tool("audit-volume", "--volume", "vol-01").stdout)
        self.assertEqual(historical["audit"]["id"], first["audit"]["id"])
        self.assertEqual(historical["plot_threads"][0]["status"], "open")
        refreshed = json.loads(self.run_tool("audit-volume", "--volume", "vol-01", "--refresh").stdout)
        self.assertNotEqual(refreshed["audit"]["id"], first["audit"]["id"])
        self.assertEqual(refreshed["audit"]["end_commit_id"], first["audit"]["end_commit_id"])
        self.assertEqual(refreshed["plot_threads"][0]["status"], "open")

    def test_deactivate_preserves_historical_timeline_references(self) -> None:
        self.commit(self.first_chapter(due_chapter=9))
        evidence = "林舟把铜钥匙留在旧宅，自此退出众人的视野。"
        delta = self.base_delta(2, 1)
        delta["entity_changes"] = [
            {"op": "unset", "id": "item.copper-key", "path": "/state/holder_id", "expect": "char.lin-zhou", "evidence": evidence},
            {"op": "deactivate", "id": "char.lin-zhou", "evidence": evidence},
        ]
        self.commit(self.write_inputs(2, evidence, delta))
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT active FROM entities WHERE entity_id = 'char.lin-zhou'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM timeline_events WHERE event_id = 'E001'").fetchone()[0], 1)

    def test_timeline_delete_is_recorded_and_rollback_restores_event(self) -> None:
        self.commit(self.first_chapter(due_chapter=9))
        evidence = "林舟并未在那个清晨抵达旧宅。"
        delta = self.base_delta(1, 1, mode="revision")
        delta["timeline_changes"] = [
            {
                "op": "delete",
                "id": "E001",
                "expect": {
                    "sequence": 1.0,
                    "story_time": "故事第一天清晨",
                    "mode": "present",
                    "fact": "林舟抵达旧宅",
                    "location_id": "loc.old-home",
                    "participant_ids": ["char.lin-zhou"],
                    "state": {},
                },
                "evidence": evidence,
            }
        ]
        committed = json.loads(self.commit(self.write_inputs(1, evidence, delta)).stdout)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0], 0)
            tombstone = connection.execute("SELECT before_json, after_json FROM resource_changes WHERE commit_id = ?", (committed["commit_id"],)).fetchone()
            self.assertIsNotNone(tombstone[0])
            self.assertIsNone(tombstone[1])
        self.run_tool("rollback", "--commit", str(committed["commit_id"]))
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM timeline_events WHERE event_id = 'E001'").fetchone()[0], 1)

    def test_revision_supersedes_old_dependencies_and_arc_beats(self) -> None:
        self.commit(self.first_chapter(due_chapter=9))
        evidence = "第二章明确承接第一章的决定。"
        second = self.base_delta(2, 1)
        second["references"] = [
            {"kind": "chapter", "id": "ch-001", "fields": ["*"], "reason": "承接决定", "evidence": evidence}
        ]
        self.commit(self.write_inputs(2, evidence, second))
        self.assertEqual(len(json.loads(self.run_tool("impact", "--chapter", "ch-001").stdout)["impacted_chapters"]), 1)

        revised_second = self.base_delta(2, 2, mode="revision")
        self.commit(self.write_inputs(2, "第二章改为独立事件。", revised_second))
        self.assertEqual(json.loads(self.run_tool("impact", "--chapter", "ch-001").stdout)["impacted_chapters"], [])

        revised_first = self.base_delta(1, 3, mode="revision")
        self.commit(self.write_inputs(1, "第一章不再包含人物弧转折。", revised_first))
        with sqlite3.connect(self.database) as connection:
            current_beats = connection.execute(
                "SELECT COUNT(*) FROM arc_beats a JOIN chapters ch ON ch.chapter_id = a.chapter_id AND ch.current_commit_id = a.commit_id"
            ).fetchone()[0]
            superseded = connection.execute("SELECT COUNT(*) FROM commits WHERE status = 'superseded'").fetchone()[0]
        self.assertEqual(current_beats, 0)
        self.assertGreaterEqual(superseded, 2)

    def test_immutable_types_future_threads_and_malformed_relationships_are_rejected(self) -> None:
        self.commit(self.first_chapter(due_chapter=9))
        evidence = "林舟仍站在旧宅门前。"

        entity_type = self.base_delta(1, 1, mode="revision")
        entity_type["entity_changes"] = [
            {"op": "set", "id": "char.lin-zhou", "path": "/type", "expect": "character", "value": "item", "evidence": evidence}
        ]
        self.commit(self.write_inputs(1, evidence, entity_type), expect=2)

        thread_type = self.base_delta(1, 1, mode="revision")
        thread_type["thread_changes"] = [
            {"op": "set", "id": "F001", "path": "/type", "expect": "foreshadow", "value": "debt", "evidence": evidence}
        ]
        self.commit(self.write_inputs(1, evidence, thread_type), expect=2)

        future = self.base_delta(2, 1)
        future["thread_changes"] = [
            {
                "op": "create", "id": "F002",
                "state": {"type": "question", "summary": "未来问题", "status": "open", "importance": "low", "introduced_chapter": 3, "due_chapter": None, "due_volume_id": None, "last_progress_chapter": 3, "state": {}},
                "evidence": evidence,
            }
        ]
        self.commit(self.write_inputs(2, evidence, future), expect=2)

        future_progress = self.base_delta(2, 1)
        future_progress["thread_changes"] = [
            {
                "op": "set", "id": "F001", "path": "/last_progress_chapter",
                "expect": 1, "value": 3, "evidence": evidence,
            }
        ]
        self.commit(self.write_inputs(2, evidence, future_progress), expect=2)

        malformed = self.base_delta(2, 1)
        malformed["entity_changes"] = [
            {"op": "create", "id": "char.bad", "type": "character", "name": "坏数据", "state": {"relationships": "char.lin-zhou"}, "evidence": evidence}
        ]
        self.commit(self.write_inputs(2, evidence, malformed), expect=2)

    def test_schema_v1_database_migrates_without_losing_snapshots(self) -> None:
        self.commit(self.first_chapter(due_chapter=9))
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                """
                ALTER TABLE chapter_versions RENAME TO chapter_versions_v2;
                CREATE TABLE chapter_versions (
                    commit_id INTEGER PRIMARY KEY,
                    chapter_id TEXT NOT NULL,
                    body_content TEXT NOT NULL,
                    record_content TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL,
                    record_sha256 TEXT NOT NULL,
                    body_path TEXT NOT NULL,
                    record_path TEXT NOT NULL,
                    style_json TEXT NOT NULL
                );
                INSERT INTO chapter_versions
                    SELECT commit_id, chapter_id, body_content, record_content, body_sha256,
                           record_sha256, body_path, record_path, style_json
                    FROM chapter_versions_v2;
                DROP TABLE chapter_versions_v2;
                DROP TABLE volume_audits;
                UPDATE project_meta SET value = '1' WHERE key = 'schema_version';
                """
            )
        verified = json.loads(self.run_tool("verify").stdout)
        self.assertEqual(verified["status"], "warn")
        with sqlite3.connect(self.database) as connection:
            schema = json.loads(connection.execute("SELECT value FROM project_meta WHERE key = 'schema_version'").fetchone()[0])
            columns = {row[1] for row in connection.execute("PRAGMA table_info(chapter_versions)")}
            legacy = json.loads(connection.execute("SELECT length_json FROM chapter_versions").fetchone()[0])
            audit_table = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'volume_audits'").fetchone()
        self.assertEqual(schema, 2)
        self.assertIn("length_json", columns)
        self.assertTrue(legacy["legacy_unverified"])
        self.assertIsNotNone(audit_table)

    def test_volume_metadata_change_is_visible_and_rollbackable(self) -> None:
        self.commit(self.first_chapter(due_chapter=9))
        body = "第二章只推进既定调查。"
        delta = self.base_delta(2, 1)
        delta["chapter"]["volume"]["planned_end_chapter"] = 12
        inputs = self.write_inputs(2, body, delta)
        checked = json.loads(
            self.run_tool(
                "check",
                "--body",
                str(inputs[0]),
                "--record",
                str(inputs[1]),
                "--delta",
                str(inputs[2]),
            ).stdout
        )
        self.assertEqual(checked["volume_change"]["before"]["planned_end_chapter"], 10)
        self.assertEqual(checked["volume_change"]["after"]["planned_end_chapter"], 12)
        committed = json.loads(self.commit(inputs).stdout)
        self.run_tool("rollback", "--commit", str(committed["commit_id"]))
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT planned_end_chapter FROM volumes WHERE volume_id = 'vol-01'").fetchone()[0], 10)

    def test_style_drift_heuristic_compares_early_and_late_chapters(self) -> None:
        rows = [
            {"style_json": json.dumps({"sentence_mean": 10, "paragraph_mean": 30, "sentence_cv": 0.8, "paragraph_cv": 0.9, "dialogue_ratio": 0.2, "formula_findings_per_1000": 1})}
            for _ in range(3)
        ] + [
            {"style_json": json.dumps({"sentence_mean": 30, "paragraph_mean": 90, "sentence_cv": 0.8, "paragraph_cv": 0.9, "dialogue_ratio": 0.2, "formula_findings_per_1000": 1})}
            for _ in range(3)
        ]
        findings = style_drift_findings(rows)  # type: ignore[arg-type]
        metrics = {item.get("metric") for item in findings if item["code"] == "style-drift"}
        self.assertEqual(metrics, {"sentence_mean", "paragraph_mean"})

    def test_batch_status_resumes_after_each_serial_commit(self) -> None:
        ready = json.loads(self.run_tool("batch-status", "--start", "1", "--end", "3").stdout)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["next_chapter_seq"], 1)

        self.commit(self.first_chapter(due_chapter=9))
        first_checkpoint = json.loads(
            self.run_tool("batch-status", "--start", "1", "--end", "3").stdout
        )
        self.assertEqual(first_checkpoint["status"], "in_progress")
        self.assertEqual(first_checkpoint["next_chapter_seq"], 2)
        self.assertEqual([item["seq"] for item in first_checkpoint["completed_chapters"]], [1])

        second = self.base_delta(2, 1)
        self.commit(self.write_inputs(2, "第二章完成新的选择。", second))
        second_checkpoint = json.loads(
            self.run_tool("batch-status", "--start", "1", "--end", "3").stdout
        )
        self.assertEqual(second_checkpoint["next_chapter_seq"], 3)
        self.assertEqual(second_checkpoint["remaining_chapter_sequences"], [3])

        third = self.base_delta(3, 2)
        self.commit(self.write_inputs(3, "第三章完成本批目标。", third))
        complete = json.loads(self.run_tool("batch-status", "--start", "1", "--end", "3").stdout)
        self.assertEqual(complete["status"], "complete")
        self.assertIsNone(complete["next_chapter_seq"])

    def test_batch_status_blocks_on_working_copy_drift(self) -> None:
        body, record, delta = self.first_chapter(due_chapter=9)
        self.commit((body, record, delta))
        body.write_text("第一章工作副本发生了未提交修改。", encoding="utf-8")
        blocked = json.loads(
            self.run_tool("batch-status", "--start", "1", "--end", "3", expect=1).stdout
        )
        self.assertEqual(blocked["status"], "block")
        self.assertIn("working-copy-drift", {item["code"] for item in blocked["findings"]})

    def test_context_infers_hot_set_and_keeps_hidden_events_off_reader_view(self) -> None:
        self.commit(self.first_chapter())
        evidence = "林舟把钥匙收进内袋，并不知道密室里还有另一把锁。"
        delta = self.base_delta(2, 1)
        delta["timeline_changes"] = [
            {
                "op": "create",
                "id": "E002",
                "state": {
                    "sequence": 2,
                    "story_time": "故事第一天夜",
                    "mode": "present",
                    "fact": "密室另有一把锁，林舟尚未发现",
                    "location_id": "loc.old-home",
                    "participant_ids": ["char.lin-zhou"],
                    "state": {
                        "reveal_status": "hidden",
                        "reader_knowledge": "",
                    },
                },
                "evidence": evidence,
            }
        ]
        self.commit(self.write_inputs(2, evidence, delta))
        packed = json.loads(self.run_tool("context", "--recent", "2").stdout)
        self.assertTrue(packed["selection"]["automatic"])
        self.assertIn("char.lin-zhou", {item["id"] for item in packed["entities"]})
        self.assertIn("F001", {item["id"] for item in packed["open_threads"]})
        self.assertEqual(
            {item["id"] for item in packed["information_boundary"]["author_timeline"]},
            {"E001", "E002"},
        )
        self.assertEqual(
            [item["id"] for item in packed["information_boundary"]["reader_timeline"]],
            ["E001"],
        )
        self.assertNotIn("密室另有一把锁", json.dumps(packed["information_boundary"]["reader_timeline"], ensure_ascii=False))

    def test_legacy_timeline_without_reader_knowledge_is_treated_as_revealed(self) -> None:
        self.commit(self.first_chapter())
        packed = json.loads(self.run_tool("context").stdout)
        event = next(item for item in packed["timeline"] if item["id"] == "E001")
        self.assertEqual(event["reveal_status"], "revealed")
        self.assertEqual(event["reader_knowledge"], "林舟抵达旧宅")
        self.assertIn("E001", {item["id"] for item in packed["information_boundary"]["reader_timeline"]})

    def test_derived_view_drift_blocks_verify(self) -> None:
        self.commit(self.first_chapter())
        view = self.project / "追踪" / "视图" / "上下文.md"
        self.assertTrue(view.is_file())
        view.write_text(view.read_text(encoding="utf-8") + "\n手改派生视图。\n", encoding="utf-8")
        blocked = json.loads(self.run_tool("verify", expect=1).stdout)
        self.assertEqual(blocked["status"], "block")
        self.assertIn("derived-view-drift", {item["code"] for item in blocked["findings"]})


if __name__ == "__main__":
    unittest.main()
