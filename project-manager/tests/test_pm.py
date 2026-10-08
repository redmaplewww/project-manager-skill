from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "pm.py"
SPEC = importlib.util.spec_from_file_location("project_manager_pm", MODULE_PATH)
assert SPEC and SPEC.loader
pm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pm)


class ProjectManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        ledger = self.root / ".project-to-act"
        ledger.mkdir()
        (ledger / "PROJECT_CONFIG.json").write_text(
            json.dumps({"schema_version": 2, "mode": "managed"}), encoding="utf-8"
        )
        args = argparse.Namespace(
            project_root=self.root,
            project_id="PM-TEST",
            name="测试项目",
            timezone="Asia/Shanghai",
            dry_run=False,
        )
        result = pm.cmd_init(args)
        self.assertEqual(result["status"], "initialized")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_changes(self, payload: dict) -> Path:
        path = self.root / "changes.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def apply(self, payload: dict, dry_run: bool = False) -> dict:
        return pm.cmd_apply(
            argparse.Namespace(project_root=self.root, changes=self.write_changes(payload), dry_run=dry_run)
        )

    def seed_requirement(self) -> None:
        result = self.apply(
            {
                "expected_revision": 0,
                "summary": "initial decomposition",
                "source_ref": "SRC-001",
                "operations": [
                    {
                        "op": "add",
                        "collection": "sources",
                        "record": {
                            "id": "SRC-001",
                            "kind": "user_message",
                            "title": "用户原始诉求",
                            "locator": "session-1:message-1",
                        },
                    },
                    {
                        "op": "add",
                        "collection": "intake_items",
                        "record": {
                            "id": "ITM-001",
                            "title": "希望成员共享项目进展",
                            "kind": "goal",
                            "disposition": "requirement",
                            "target_ids": ["REQ-001"],
                            "source_refs": ["SRC-001"],
                        },
                    },
                    {
                        "op": "add",
                        "collection": "scenarios",
                        "record": {
                            "id": "SCN-001",
                            "title": "用户提交进展",
                            "actor": "项目成员",
                            "trigger": "工作有更新",
                            "success_signal": "主管可以读取",
                            "source_refs": ["SRC-001"],
                            "intake_item_ids": ["ITM-001"],
                        },
                    },
                    {
                        "op": "add",
                        "collection": "requirements",
                        "record": {
                            "id": "REQ-001",
                            "title": "提交工作进展",
                            "scenario_id": "SCN-001",
                            "user_value": "共享项目事实",
                            "trigger": "工作有更新",
                            "behavior": "成员提交进展及阻塞",
                            "outcome": "信息可供主管查看",
                            "boundaries": ["不进行绩效评分"],
                            "acceptance_criteria": ["提交后主管可查看相同内容"],
                            "source_refs": ["SRC-001"],
                            "status": "pending_confirmation",
                            "readiness": "ready_for_design",
                            "priority": "must",
                        },
                    },
                ],
            }
        )
        self.assertEqual(result["revision"], 1)

    def test_incremental_requirement_keeps_stable_id(self) -> None:
        self.seed_requirement()
        result = self.apply(
            {
                "expected_revision": 1,
                "summary": "confirm one requirement",
                "source_ref": "SRC-001",
                "operations": [
                    {
                        "op": "transition",
                        "collection": "requirements",
                        "id": "REQ-001",
                        "to": "confirmed",
                        "reason": "用户明确确认",
                        "source_ref": "SRC-001",
                    }
                ],
            }
        )
        self.assertEqual(result["revision"], 2)
        state = pm.load_state(self.root)
        req = state["requirements"][0]
        self.assertEqual(req["id"], "REQ-001")
        self.assertEqual(req["version"], 1)
        self.assertEqual(req["status"], "confirmed")

    def test_confirmed_semantic_revision_requires_change_metadata(self) -> None:
        self.seed_requirement()
        self.apply(
            {
                "expected_revision": 1,
                "operations": [
                    {
                        "op": "transition",
                        "collection": "requirements",
                        "id": "REQ-001",
                        "to": "confirmed",
                        "reason": "确认",
                        "source_ref": "SRC-001",
                    }
                ],
            }
        )
        with self.assertRaises(pm.PMError):
            self.apply(
                {
                    "expected_revision": 2,
                    "operations": [
                        {
                            "op": "revise",
                            "collection": "requirements",
                            "id": "REQ-001",
                            "expected_version": 1,
                            "patch": {"behavior": "改变后的产品行为"},
                        }
                    ],
                }
            )
        self.assertEqual(pm.load_state(self.root)["revision"], 2)

    def test_confirmed_revision_invalidates_related_evidence_and_flags_work(self) -> None:
        self.seed_requirement()
        self.apply(
            {
                "expected_revision": 1,
                "operations": [
                    {
                        "op": "add",
                        "collection": "work_packages",
                        "record": {
                            "id": "WP-001",
                            "title": "实现进展提交闭环",
                            "status": "done",
                            "requirement_ids": ["REQ-001"],
                        },
                    },
                    {
                        "op": "add",
                        "collection": "evidence",
                        "record": {
                            "id": "EVD-001",
                            "kind": "test",
                            "related_ids": ["REQ-001"],
                            "status": "valid",
                        },
                    },
                    {
                        "op": "transition",
                        "collection": "requirements",
                        "id": "REQ-001",
                        "to": "confirmed",
                        "reason": "用户确认",
                        "source_ref": "SRC-001",
                    },
                ],
            }
        )
        result = self.apply(
            {
                "expected_revision": 2,
                "operations": [
                    {
                        "op": "revise",
                        "collection": "requirements",
                        "id": "REQ-001",
                        "expected_version": 1,
                        "patch": {"behavior": "成员提交进展、阻塞和下一步"},
                        "reason": "用户补充了下一步字段",
                        "source_ref": "SRC-001",
                        "change_class": "addition",
                    }
                ],
            }
        )
        self.assertEqual(result["revision"], 3)
        state = pm.load_state(self.root)
        self.assertEqual(state["requirements"][0]["id"], "REQ-001")
        self.assertEqual(state["requirements"][0]["version"], 2)
        self.assertEqual(state["evidence"][0]["status"], "needs_review")
        self.assertEqual(state["work_packages"][0]["impact_review"], "needed")

    def test_optimistic_revision_rejects_stale_writer(self) -> None:
        self.seed_requirement()
        with self.assertRaises(pm.PMError):
            self.apply(
                {
                    "expected_revision": 0,
                    "operations": [{"op": "set_project", "patch": {"active_mode": "initiation"}}],
                }
            )

    def test_broken_reference_aborts_whole_change(self) -> None:
        with self.assertRaises(pm.PMError):
            self.apply(
                {
                    "expected_revision": 0,
                    "operations": [
                        {
                            "op": "add",
                            "collection": "requirements",
                            "record": {
                                "id": "REQ-001",
                                "title": "无来源需求",
                                "source_refs": ["SRC-MISSING"],
                                "status": "candidate",
                                "readiness": "not_ready",
                                "priority": "could",
                            },
                        }
                    ],
                }
            )
        self.assertEqual(pm.load_state(self.root)["requirements"], [])

    def test_session_import_is_public_redacted_and_idempotent(self) -> None:
        session = self.root / "session.jsonl"
        rows = [
            {"type": "message", "role": "system", "content": "hidden"},
            {"type": "message", "role": "user", "content": "token=abc123 联系 a@example.com"},
            {"type": "response_item", "payload": {"type": "reasoning", "content": "hidden chain"}},
            {
                "timestamp": "2026-10-04T10:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "公开答复"}],
                },
            },
        ]
        session.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
        args = argparse.Namespace(
            project_root=self.root,
            project_id="PM-TEST",
            session_id="session-1",
            session_file=session,
            dry_run=False,
        )
        first = pm.cmd_import_session(args)
        second = pm.cmd_import_session(args)
        self.assertEqual(first["messages_added"], 2)
        self.assertEqual(second["messages_added"], 0)
        archive = Path(first["output"]).read_text(encoding="utf-8")
        self.assertIn("[REDACTED]", archive)
        self.assertIn("[REDACTED_EMAIL]", archive)
        self.assertNotIn("hidden chain", archive)
        self.assertNotIn("system", archive)

    def test_session_id_cannot_escape_archive_directory(self) -> None:
        session = self.root / "session.jsonl"
        session.write_text(json.dumps({"type": "message", "role": "user", "content": "hello"}), encoding="utf-8")
        args = argparse.Namespace(
            project_root=self.root,
            project_id="PM-TEST",
            session_id="../escape",
            session_file=session,
            dry_run=False,
        )
        with self.assertRaises(pm.PMError):
            pm.cmd_import_session(args)
        self.assertFalse((self.root / ".project-to-act" / "local" / "escape.jsonl").exists())

    def test_check_warns_when_confirmed_requirement_has_no_work_package(self) -> None:
        self.seed_requirement()
        self.apply(
            {
                "expected_revision": 1,
                "operations": [
                    {
                        "op": "transition",
                        "collection": "requirements",
                        "id": "REQ-001",
                        "to": "confirmed",
                        "reason": "确认",
                        "source_ref": "SRC-001",
                    }
                ],
            }
        )
        result, code = pm.cmd_check(argparse.Namespace(project_root=self.root, strict=False))
        self.assertEqual(code, 0)
        self.assertTrue(any(item["code"] == "CONFIRMED_WITHOUT_WORK_PACKAGE" for item in result["warnings"]))

    def test_deadline_uses_project_timezone_and_surfaces_overdue(self) -> None:
        self.apply(
            {
                "expected_revision": 0,
                "operations": [
                    {
                        "op": "add",
                        "collection": "milestones",
                        "record": {
                            "id": "MS-001",
                            "title": "探索结果复盘",
                            "deadline_type": "exploration",
                            "status": "planned",
                            "due_at": "2000-01-01",
                        },
                    }
                ],
            }
        )
        result, code = pm.cmd_check(argparse.Namespace(project_root=self.root, strict=False))
        self.assertEqual(code, 0)
        self.assertTrue(any(item["code"] == "OVERDUE" for item in result["warnings"]))
        resumed = pm.cmd_resume(argparse.Namespace(project_root=self.root, hours=72))
        self.assertEqual(resumed["deadline_alerts"][0]["id"], "MS-001")

    def test_unclassified_intake_item_is_visible(self) -> None:
        self.apply(
            {
                "expected_revision": 0,
                "operations": [
                    {
                        "op": "add",
                        "collection": "sources",
                        "record": {"id": "SRC-001", "kind": "user_message", "title": "原始输入"},
                    },
                    {
                        "op": "add",
                        "collection": "intake_items",
                        "record": {
                            "id": "ITM-001",
                            "title": "也许以后加入排行榜",
                            "kind": "candidate_idea",
                            "disposition": "unclassified",
                            "source_refs": ["SRC-001"],
                        },
                    },
                ],
            }
        )
        result, _ = pm.cmd_check(argparse.Namespace(project_root=self.root, strict=False))
        self.assertTrue(any(item["code"] == "UNCLASSIFIED_INTAKE_ITEM" for item in result["warnings"]))

    def test_review_and_evidence_status_change_via_revise(self) -> None:
        self.apply(
            {
                "expected_revision": 0,
                "operations": [
                    {
                        "op": "add",
                        "collection": "sources",
                        "record": {"id": "SRC-001", "kind": "user_message", "title": "原始输入"},
                    },
                    {
                        "op": "add",
                        "collection": "requirements",
                        "record": {
                            "id": "REQ-001",
                            "title": "示例需求",
                            "source_refs": ["SRC-001"],
                            "status": "candidate",
                        },
                    },
                    {
                        "op": "add",
                        "collection": "reviews",
                        "record": {"id": "REV-001", "title": "示例评审", "kind": "risk", "status": "open"},
                    },
                    {
                        "op": "add",
                        "collection": "evidence",
                        "record": {"id": "EVD-001", "kind": "test", "status": "valid", "related_ids": ["REQ-001"]},
                    },
                ],
            }
        )
        self.apply(
            {
                "expected_revision": 1,
                "operations": [
                    {
                        "op": "revise",
                        "collection": "reviews",
                        "id": "REV-001",
                        "expected_version": 1,
                        "patch": {"status": "closed", "resolution": "用户拒绝该提议"},
                    },
                    {
                        "op": "revise",
                        "collection": "evidence",
                        "id": "EVD-001",
                        "expected_version": 1,
                        "patch": {"status": "needs_review"},
                    },
                ],
            }
        )
        state = pm.load_state(self.root)
        self.assertEqual(state["reviews"][0]["status"], "closed")
        self.assertEqual(state["reviews"][0]["version"], 2)
        self.assertEqual(state["evidence"][0]["status"], "needs_review")
        self.assertEqual(state["evidence"][0]["version"], 2)

    def test_governed_collections_still_require_transition_for_status(self) -> None:
        self.seed_requirement()
        with self.assertRaises(pm.PMError):
            self.apply(
                {
                    "expected_revision": 1,
                    "operations": [
                        {
                            "op": "revise",
                            "collection": "requirements",
                            "id": "REQ-001",
                            "expected_version": 1,
                            "patch": {"status": "confirmed"},
                        }
                    ],
                }
            )

    def test_risk_occurred_can_move_to_mitigating_then_closed(self) -> None:
        self.apply(
            {
                "expected_revision": 0,
                "operations": [
                    {
                        "op": "add",
                        "collection": "risks",
                        "record": {"id": "RSK-001", "title": "已发生风险", "status": "occurred"},
                    }
                ],
            }
        )
        self.apply(
            {
                "expected_revision": 1,
                "operations": [
                    {"op": "transition", "collection": "risks", "id": "RSK-001", "to": "mitigating"},
                    {"op": "transition", "collection": "risks", "id": "RSK-001", "to": "closed"},
                ],
            }
        )
        self.assertEqual(pm.load_state(self.root)["risks"][0]["status"], "closed")


if __name__ == "__main__":
    unittest.main()
