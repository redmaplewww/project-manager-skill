from __future__ import annotations

import argparse
import copy
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

    def test_progress_summary_exposes_action_owner_due_and_acceptance(self) -> None:
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
                            "status": "in_progress",
                            "owner": "项目开发者",
                            "due_at": "2026-10-20",
                            "requirement_ids": ["REQ-001"],
                        },
                    }
                ],
            }
        )
        summary = pm.build_progress_summary(self.root, pm.load_state(self.root))
        action = next(a for a in summary["next_actions"] if a["id"] == "WP-001")
        self.assertEqual(action["id"], "WP-001")
        self.assertEqual(action["owner"], "项目开发者")
        self.assertEqual(action["due_at"], "2026-10-20")
        self.assertEqual(action["acceptance_criteria"], ["提交后主管可查看相同内容"])
        self.assertEqual(action["missing"], [])

    def test_daily_progress_aggregates_state_changes_and_completion(self) -> None:
        state = pm.default_state("PM-TEST", "测试项目", "Asia/Shanghai")
        state["work_packages"] = [{"id": "WP-001", "title": "交付工作包", "status": "done"}]
        state["change_events"] = [
            {
                "revision": 1,
                "timestamp": "2026-10-10T16:30:00Z",
                "summary": "推进工作包",
                "operations": [
                    {"op": "transition", "collection": "work_packages", "id": "WP-001", "from": "in_progress", "to": "done"}
                ],
            }
        ]
        daily = pm.build_daily_progress(state, [])
        self.assertEqual(daily[0]["date"], "2026-10-11")
        self.assertEqual(daily[0]["state_changes"], 1)
        self.assertEqual(daily[0]["completed_items"], ["WP-001 · 交付工作包"])
        self.assertIn("状态变更：WP-001 · 交付工作包 · in_progress → done", daily[0]["change_details"][0]["operations"])
        self.assertIn("完成开发工作包 1 项", daily[0]["daily_summary"])
        self.assertIn("开发工作包", daily[0]["change_groups"])
        self.assertEqual(daily[0]["change_groups"]["开发工作包"][0]["revision"], 1)
        self.assertEqual(daily[0]["progress_delta"]["work_packages_completed"], ["WP-001 · 交付工作包"])
        self.assertEqual(daily[0]["current_snapshot"]["work_packages_done"], 1)
        self.assertIn("开发拆解与纵向交付验证", daily[0]["synthesis"])
        self.assertIn("完成 1 个工作包并形成验证结果", daily[0]["synthesis"])
        self.assertIn("当前累计为需求确认", daily[0]["synthesis"])

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

    def test_build_readiness_requires_feature_shaping_gate(self) -> None:
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
                            "patch": {"readiness": "ready_for_build"},
                        }
                    ],
                }
            )

    def test_build_readiness_accepts_complete_feature_shaping_gate(self) -> None:
        self.seed_requirement()
        self.apply(
            {
                "expected_revision": 1,
                "operations": [
                    {
                        "op": "revise",
                        "collection": "requirements",
                        "id": "REQ-001",
                        "expected_version": 1,
                        "patch": {
                            "product_form": {
                                "user_and_trigger": "confirmed",
                                "main_flow": "confirmed",
                                "non_goals": ["none"],
                                "diagram_refs": ["flow-1"],
                            },
                            "experience_review": {
                                "normal_cases": ["happy"],
                                "edge_cases": ["empty", "error"],
                                "accessibility_and_responsive": "reviewed",
                                "interaction_contract": {
                                    "entry_controls": "documented",
                                    "feedback_states": "documented",
                                    "empty_loading_error": "documented",
                                    "retry_cancel_recovery": "documented",
                                    "accessibility_responsive": "reviewed",
                                },
                            },
                            "technical_design": {
                                "stack": "existing project stack",
                                "architecture_ref": "arch-1",
                                "data_contract": "reviewed",
                                "security_and_operations": "reviewed",
                            },
                            "implementation_contract": {
                                "discovery_method": "repository and runtime inspection",
                                "implementation_surfaces": [
                                    {
                                        "name": "discovered surface",
                                        "evidence": ["src/example.tsx"],
                                        "current_structure": "documented",
                                        "decision": "documented",
                                        "behavior_impact": "assessed",
                                        "verification": "defined",
                                        "task_mapping": "WP-001",
                                        "execution_steps": ["step-1"],
                                        "interfaces_or_data": "documented",
                                        "failure_and_rollback": "documented",
                                        "observability": "documented",
                                        "test_mapping": ["AC-001"],
                                    }
                                ],
                                "functional_decomposition": [
                                    {"id": "FU-001", "title": "提交进展", "actor": "成员", "trigger": "有更新", "preconditions": ["项目存在"], "inputs": ["进展文本"], "steps": ["校验", "保存"], "outputs": ["可查看记录"], "state_transitions": ["draft->saved"], "errors": ["保存失败"], "acceptance": ["AC-001"], "implementation_mapping": ["WP-001"]}
                                ],
                                "state_model": [
                                    {"state": "ready", "entry": "打开项目", "visible_behavior": "可提交", "allowed_actions": ["提交"], "exit": "保存或失败", "retention": "输入保留"}
                                ],
                                "verification_matrix": [
                                    {"id": "VM-001", "scenario": "正常提交", "expected": "记录可查看", "method": "pytest", "evidence_or_test": "test_submit"}
                                ],
                                "decisions": ["DEC-001"],
                                "unresolved_items": [],
                                "task_breakdown": "reviewed",
                            },
                            "acceptance_scenarios": [{"id": "AC-001", "given": "项目存在", "when": "成员提交进展", "then": "主管可查看", "verification": "pytest test_submit"}],
                            "feature_shaping_rounds": {
                                "completion_policy": "五轮均完成且未决项关闭",
                                "rounds": [
                                    {"round": 1, "status": "complete", "questions": ["谁使用"], "conclusions": ["成员"], "evidence": ["SRC-001"], "unresolved_items": [], "confirmation_source": "SRC-001"},
                                    {"round": 2, "status": "complete", "questions": ["失败如何反馈"], "conclusions": ["显示错误"], "evidence": ["SRC-001"], "unresolved_items": [], "confirmation_source": "SRC-001"},
                                    {"round": 3, "status": "complete", "questions": ["数据如何保存"], "conclusions": ["项目隔离"], "evidence": ["SRC-001"], "unresolved_items": [], "confirmation_source": "SRC-001"},
                                    {"round": 4, "status": "complete", "questions": ["如何实现"], "conclusions": ["复用现有结构"], "evidence": ["SRC-001"], "unresolved_items": [], "confirmation_source": "SRC-001"},
                                    {"round": 5, "status": "confirmed", "questions": ["如何验收"], "conclusions": ["测试通过"], "evidence": ["SRC-001"], "unresolved_items": [], "confirmation_source": "SRC-001"}
                                ]
                            },
                            "open_questions": [],
                            "gate": {
                                "status": "approved",
                                "review_lenses": [
                                    {
                                        "name": "discovered-lens",
                                        "question": "documented",
                                        "conclusion": "documented",
                                        "evidence": ["SRC-001"],
                                        "unresolved": [],
                                    }
                                ],
                                "confirmed_source": "SRC-001",
                                "confirmation_protocol": "explicit_scope_confirmation",
                                "confirmed_scope_statement": "用户明确确认成员提交进展并由主管查看。",
                            },
                        },
                    },
                    {
                        "op": "transition",
                        "collection": "requirements",
                        "id": "REQ-001",
                        "to": "confirmed",
                        "reason": "feature shaping complete",
                        "source_ref": "SRC-001",
                    },
                    {
                        "op": "revise",
                        "collection": "requirements",
                        "id": "REQ-001",
                        "expected_version": 2,
                        "patch": {"confirmation_lock": {
                            "status": "locked",
                            "locked_version": 3,
                            "locked_source_ref": "SRC-001",
                            "locked_at": "2026-10-11T00:00:00Z",
                            "reason": "冻结确认范围",
                            "reopen_policy": "only_explicit_scope_change_request",
                        }},
                        "reason": "冻结已确认产品范围",
                        "source_ref": "SRC-001",
                        "change_class": "wording_only",
                    },
                    {
                        "op": "revise",
                        "collection": "requirements",
                        "id": "REQ-001",
                        "expected_version": 3,
                        "patch": {"readiness": "ready_for_build"},
                        "reason": "feature gate approved",
                        "source_ref": "SRC-001",
                        "change_class": "wording_only",
                    },
                ],
            }
        )
        state = pm.load_state(self.root)
        self.assertEqual(state["requirements"][0]["readiness"], "ready_for_build")
        self.assertEqual(state["requirements"][0]["version"], 3)
        ai_probe = copy.deepcopy(state["requirements"][0])
        ai_probe.pop("confirmation_lock", None)
        ai_probe.update(status="ai_confirmed_unverified", human_verification="unverified",
                        code_mapping=[{"file": "app.py", "behavior": "实际入口映射"}])
        ai_probe["gate"]["confirmation_protocol"] = "ai_review_unverified"
        pm.validate_record("requirements", ai_probe)
        ai_probe["status"] = "confirmed"
        with self.assertRaisesRegex(pm.PMError, "explicit scope confirmation protocol"):
            pm.validate_record("requirements", ai_probe)
        with self.assertRaises(pm.PMError):
            self.apply({
                "expected_revision": 2,
                "operations": [{
                    "op": "revise",
                    "collection": "requirements",
                    "id": "REQ-001",
                    "expected_version": 3,
                    "patch": {"behavior": "updated behavior"},
                    "reason": "new product behavior",
                    "source_ref": "SRC-001",
                    "change_class": "addition",
                }],
            })
        with self.assertRaisesRegex(pm.PMError, "readiness=ready_for_build"):
            self.apply({
                "expected_revision": 2,
                "operations": [{
                    "op": "revise",
                    "collection": "requirements",
                    "id": "REQ-001",
                    "expected_version": 3,
                    "patch": {"readiness": "ready_for_acceptance"},
                    "reason": "未经实施验收的推进",
                    "source_ref": "SRC-001",
                    "change_class": "wording_only",
                }],
            })
        locked = pm.load_state(self.root)["requirements"][0]
        changed_gate = dict(locked["gate"], confirmed_scope_statement="扩大后的产品范围")
        with self.assertRaisesRegex(pm.PMError, "confirmed_scope_statement is protected"):
            self.apply({
                "expected_revision": 2,
                "operations": [{
                    "op": "revise",
                    "collection": "requirements",
                    "id": "REQ-001",
                    "expected_version": 3,
                    "patch": {"gate": changed_gate},
                    "reason": "尝试修改已确认范围",
                    "source_ref": "SRC-001",
                    "change_class": "wording_only",
                }],
            })
        changed_gate_status = dict(locked["gate"], status="rejected")
        with self.assertRaisesRegex(pm.PMError, "gate.status is protected"):
            self.apply({
                "expected_revision": 2,
                "operations": [{
                    "op": "revise",
                    "collection": "requirements",
                    "id": "REQ-001",
                    "expected_version": 3,
                    "patch": {"gate": changed_gate_status},
                    "reason": "尝试重开已锁定门禁",
                    "source_ref": "SRC-001",
                    "change_class": "wording_only",
                }],
            })
        state = pm.load_state(self.root)
        self.assertEqual(state["requirements"][0]["readiness"], "ready_for_build")
        self.assertEqual(state["requirements"][0]["version"], 3)

        # Engineering preparation does not require implementation/test results first.
        self.apply({"expected_revision": 2, "operations": [{"op": "add", "collection": "work_packages", "record": {
            "id": "WP-001", "title": "实现提交", "kind": "implementation", "status": "not_started",
            "requirement_ids": ["REQ-001"], "next_action": "实现已确认的保存接口",
        }}]})
        state = pm.load_state(self.root)
        self.make_package(state, version=3)
        report = pm.delivery_diagnostics(self.root, state)
        self.assertTrue(report["requirements"][0]["can_start_full_build"])
        self.assertEqual(report["requirements"][0]["verification"], "not_assessed")
        # Completing an independent gate can advance metadata without unlocking scope.
        probe = copy.deepcopy(state)
        probe["requirements"][0]["gate"]["status"] = "confirmed_locked"
        probe["requirements"][0]["readiness"] = "ready_for_design"
        pm.apply_operation(probe, {"op": "revise", "collection": "requirements", "id": "REQ-001", "expected_version": 3,
                                  "reason": "independent engineering review", "source_ref": "SRC-001", "change_class": "wording_only",
                                  "patch": {"gate": state["requirements"][0]["gate"], "readiness": "ready_for_build"}}, pm.now_iso())
        self.assertEqual(probe["requirements"][0]["version"], 3)
        self.assertEqual(probe["requirements"][0]["gate"]["status"], "approved")

    def test_shallow_feature_package_is_rejected(self) -> None:
        self.seed_requirement()
        self.apply({
            "expected_revision": 1,
            "operations": [
                {
                    "op": "revise",
                    "collection": "requirements",
                    "id": "REQ-001",
                    "expected_version": 1,
                    "patch": {
                        "product_form": {"user_and_trigger": "x", "main_flow": "x", "non_goals": ["x"], "diagram_refs": ["x"]},
                        "experience_review": {"normal_cases": ["x"], "edge_cases": ["x"], "accessibility_and_responsive": "x", "interaction_contract": {"entry_controls": "x", "feedback_states": "x", "empty_loading_error": "x", "retry_cancel_recovery": "x", "accessibility_responsive": "x"}},
                        "technical_design": {"stack": "x", "architecture_ref": "x", "data_contract": "x", "security_and_operations": "x"},
                        "implementation_contract": {"discovery_method": "x", "implementation_surfaces": [{"name": "x", "evidence": ["x"], "current_structure": "x", "decision": "x", "behavior_impact": "x", "verification": "x", "task_mapping": "x", "execution_steps": ["x"], "interfaces_or_data": "x", "failure_and_rollback": "x", "observability": "x", "test_mapping": ["x"]}], "functional_decomposition": [], "state_model": [], "verification_matrix": [], "decisions": [], "unresolved_items": [], "task_breakdown": "x"},
                        "acceptance_scenarios": [], "feature_shaping_rounds": {"completion_policy": "x", "rounds": []}, "open_questions": [], "gate": {"status": "approved", "review_lenses": [], "confirmed_source": "SRC-001", "confirmation_protocol": "explicit_scope_confirmation", "confirmed_scope_statement": "x"}
                    },
                }
            ],
        })
        with self.assertRaises(pm.PMError):
            self.apply({"expected_revision": 2, "operations": [{"op": "revise", "collection": "requirements", "id": "REQ-001", "expected_version": 2, "patch": {"readiness": "ready_for_build"}, "reason": "尝试浅门禁", "source_ref": "SRC-001", "change_class": "wording_only"}]})

    def test_confirmation_package_requires_per_question_recommendations(self) -> None:
        self.seed_requirement()
        invalid_package = {
            "requirement_id": "REQ-001",
            "requirement_version": 1,
            "protocol": "single_umbrella_question",
            "presented_in_conversation": True,
            "bulk_accept_phrase": "按本 REQ 推荐全部采纳",
            "questions": [{"id": "DQ-001", "question": "是否确认全部？"}],
        }
        with self.assertRaises(pm.PMError):
            self.apply({
                "expected_revision": 1,
                "operations": [{
                    "op": "revise", "collection": "requirements", "id": "REQ-001",
                    "expected_version": 1, "patch": {"confirmation_package": invalid_package},
                    "reason": "验证确认协议", "source_ref": "SRC-001", "change_class": "addition",
                }],
            })

    def test_confirmation_package_accepts_separate_questions_with_recommendations(self) -> None:
        self.seed_requirement()
        question = {
            "id": "DQ-001", "question": "本期是否保留撤销？", "current_conclusion": "撤销会影响状态回滚",
            "recommendation": "保留撤销并限制为最近一次操作", "alternatives": ["不提供撤销"],
            "basis": "用户路径调查", "behavior_impact": "用户可撤销最近一次操作",
            "implementation_impact": "增加操作日志和回滚接口", "acceptance_impact": "验收撤销后状态一致",
            "status": "open", "answer": None, "requirement_version": 2,
            "source_ref": "SRC-001", "asked_at": "2026-10-11T00:00:00Z",
        }
        package = {
            "requirement_id": "REQ-001", "requirement_version": 2,
            "protocol": "per_question_with_recommendations", "presented_in_conversation": True,
            "bulk_accept_phrase": "按本 REQ 推荐全部采纳", "questions": [question],
        }
        result = self.apply({
            "expected_revision": 1,
            "operations": [{
                "op": "revise", "collection": "requirements", "id": "REQ-001",
                "expected_version": 1, "patch": {"confirmation_package": package, "decision_questions": [question]},
                "reason": "保存逐项确认包", "source_ref": "SRC-001", "change_class": "addition",
            }],
        })
        self.assertEqual(result["revision"], 2)

    def test_confirmation_lock_blocks_reopening_scope(self) -> None:
        self.seed_requirement()
        self.apply({
            "expected_revision": 1,
            "operations": [{
                "op": "revise", "collection": "requirements", "id": "REQ-001",
                "expected_version": 1,
                "patch": {"confirmation_lock": {
                    "status": "locked", "locked_version": 2, "locked_source_ref": "SRC-001",
                    "locked_at": "2026-10-11T00:00:00Z", "reason": "冻结确认范围",
                    "reopen_policy": "only_explicit_scope_change_request",
                }},
                "reason": "冻结确认范围", "source_ref": "SRC-001", "change_class": "correction",
            }],
        })
        with self.assertRaises(pm.PMError):
            self.apply({
                "expected_revision": 2,
                "operations": [{
                    "op": "revise", "collection": "requirements", "id": "REQ-001",
                    "expected_version": 2, "patch": {"open_questions": ["不应重新打开"]},
                    "reason": "尝试重开", "source_ref": "SRC-001", "change_class": "correction",
                }],
            })

    def test_confirmation_lock_has_explicit_unlock_path(self) -> None:
        self.seed_requirement()
        self.apply({
            "expected_revision": 1,
            "operations": [{
                "op": "revise", "collection": "requirements", "id": "REQ-001",
                "expected_version": 1,
                "patch": {"confirmation_lock": {
                    "status": "locked", "locked_version": 2, "locked_source_ref": "SRC-001",
                    "locked_at": "2026-10-11T00:00:00Z", "reason": "冻结确认范围",
                    "reopen_policy": "only_explicit_scope_change_request",
                }},
                "reason": "冻结确认范围", "source_ref": "SRC-001", "change_class": "correction",
            }],
        })
        result = self.apply({
            "expected_revision": 2,
            "operations": [{
                "op": "unlock_confirmation", "collection": "requirements", "id": "REQ-001",
                "reason": "用户明确提出范围变更", "source_ref": "SRC-001",
            }],
        })
        self.assertEqual(result["revision"], 3)
        state = pm.load_state(self.root)
        self.assertEqual(state["requirements"][0]["status"], "pending_confirmation")
        self.assertNotIn("confirmation_lock", state["requirements"][0])

    def test_unlock_archives_version_bound_package_without_new_confirmation_claim(self) -> None:
        self.seed_requirement()
        question = {
            "id": "DQ-001", "question": "保留撤销吗？", "current_conclusion": "需要决定恢复边界",
            "recommendation": "保留最近一次撤销", "alternatives": ["不提供撤销"],
            "basis": "已有用户路径", "behavior_impact": "可撤销最近操作", "implementation_impact": "记录历史",
            "acceptance_impact": "恢复一致", "status": "accepted", "answer": "保留", "source_ref": "SRC-001",
            "requirement_version": 2, "asked_at": "2026-11-01T00:00:00Z",
        }
        package = {"requirement_id": "REQ-001", "requirement_version": 2,
            "protocol": "per_question_with_recommendations", "presented_in_conversation": True,
            "bulk_accept_phrase": "按本 REQ 推荐全部采纳", "questions": [question]}
        self.apply({"expected_revision": 1, "operations": [{"op": "revise", "collection": "requirements", "id": "REQ-001",
            "expected_version": 1, "patch": {"confirmation_package": package, "confirmation_lock": {
                "status": "locked", "locked_version": 2, "locked_source_ref": "SRC-001", "locked_at": "2026-11-01T00:00:00Z",
                "reason": "已确认旧范围", "reopen_policy": "only_explicit_scope_change_request"}},
            "reason": "保存旧版本的确认材料", "source_ref": "SRC-001", "change_class": "correction"}]})
        change = {"expected_revision": 2, "operations": [{"op": "unlock_confirmation", "collection": "requirements",
            "id": "REQ-001", "source_ref": "SRC-001", "reason": "用户明确扩大实施范围"}]}
        before = pm.state_path(self.root).read_bytes()
        self.apply(change, dry_run=True)
        self.assertEqual(before, pm.state_path(self.root).read_bytes())
        self.apply(change)
        req = pm.load_state(self.root)["requirements"][0]
        self.assertEqual(req["version"], 3)
        self.assertEqual(req["status"], "pending_confirmation")
        self.assertEqual(req["readiness"], "ready_for_design")
        self.assertNotIn("confirmation_package", req)
        self.assertEqual(req["confirmation_history"][-1]["artifacts"]["confirmation_package"], package)
        self.assertEqual(req["confirmation_history"][-1]["artifacts"]["confirmation_lock"]["locked_version"], 2)

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


    def confirmed_snapshot(self) -> dict:
        self.seed_requirement()
        state = pm.load_state(self.root)
        state["requirements"][0]["status"] = "confirmed"
        return state

    def make_package(self, state: dict, version: int = 1, draft: bool = False) -> Path:
        docs = pm.engineering_docs_root(self.root)
        folder = docs / "project-development" / "packages" / "REQ-001" / f"v{version}"
        folder.mkdir(parents=True, exist_ok=True)
        package = folder / "ENGINEERING-PACKAGE.md"
        package.write_text(f"# REQ-001 v{version}\n" + ("<!-- pm:package-draft -->\n" if draft else "工程设计与执行契约\n"), encoding="utf-8")
        (folder / "INDEX.md").write_text("[工程包](ENGINEERING-PACKAGE.md)", encoding="utf-8")
        (docs / "project-development" / "INDEX.md").write_text(f"[工程包](packages/REQ-001/v{version}/ENGINEERING-PACKAGE.md)", encoding="utf-8")
        (docs / "README.md").write_text("[开发区](project-development/INDEX.md)", encoding="utf-8")
        return package

    def test_doctor_finds_missing_and_outdated_packages_without_mutation(self) -> None:
        state = self.confirmed_snapshot()
        before = json.dumps(state, sort_keys=True)
        disk_before = pm.state_path(self.root).read_bytes()
        report = pm.delivery_diagnostics(self.root, state)
        self.assertIn("ENGINEERING_PACKAGE_MISSING", [f["code"] for f in report["findings"]])
        self.make_package(state, version=1)
        state["requirements"][0]["version"] = 2
        report = pm.delivery_diagnostics(self.root, state)
        self.assertIn("ENGINEERING_PACKAGE_STALE", [f["code"] for f in report["findings"]])
        state["requirements"][0]["version"] = 1
        self.assertEqual(json.dumps(state, sort_keys=True), before)
        self.assertEqual(pm.state_path(self.root).read_bytes(), disk_before)

    def test_cancelled_and_broad_governance_work_do_not_satisfy_handoff(self) -> None:
        state = self.confirmed_snapshot()
        state["work_packages"] = [
            {"id": "WP-OLD", "title": "旧任务", "status": "cancelled", "requirement_ids": ["REQ-001"]},
            {"id": "WP-ALL", "title": "通用治理", "status": "in_progress", "requirement_ids": ["REQ-001", "REQ-002"]},
        ]
        report = pm.delivery_diagnostics(self.root, state)
        self.assertIn("CONFIRMED_WITHOUT_WORK_PACKAGE", [f["code"] for f in report["findings"]])
        state["work_packages"][1]["kind"] = "implementation"
        report = pm.delivery_diagnostics(self.root, state)
        self.assertNotIn("CONFIRMED_WITHOUT_WORK_PACKAGE", [f["code"] for f in report["findings"]])

    def test_missing_design_is_ai_work_and_existing_closure_is_preserved(self) -> None:
        self.seed_requirement()
        state = pm.load_state(self.root)
        state["requirements"][0]["closure_attempt"] = {"source_ref": "SRC-001"}
        report = pm.delivery_diagnostics(self.root, state)
        self.assertTrue(report["requirements"][0]["closure_signal_received"])
        self.assertEqual(report["user_decision_ids"], [])
        shape = next(f for f in report["findings"] if f["code"] == "FEATURE_SHAPING_INCOMPLETE")
        self.assertIn("implementation_contract", shape["missing_fields"])
        self.assertFalse(shape["needs_user_decision"])
        self.assertTrue(shape["steps"] and shape["acceptance_criteria"] and shape["target"])

    def test_scope_filter_and_stage_checks_do_not_report_global_delivery_success(self) -> None:
        self.seed_requirement()
        structure, code = pm.cmd_check(argparse.Namespace(project_root=self.root, strict=True, stage="structure", requirement=["REQ-001"]))
        self.assertEqual(code, 0)
        engineering, code = pm.cmd_check(argparse.Namespace(project_root=self.root, strict=True, stage="engineering", requirement=["REQ-001"]))
        self.assertEqual(code, 1)
        self.assertTrue(engineering["structure_valid"])
        self.assertEqual(engineering["stage_results"]["verification"], "not_assessed")
        self.assertEqual(engineering["stage_results"]["acceptance"], "not_assessed")
        with self.assertRaises(pm.PMError):
            pm.delivery_diagnostics(self.root, pm.load_state(self.root), ["REQ-DOES-NOT-EXIST"])

    def test_draft_and_missing_index_do_not_count_as_ready_packages(self) -> None:
        state = self.confirmed_snapshot()
        package = self.make_package(state, draft=True)
        (package.parent / "INDEX.md").unlink()
        report = pm.delivery_diagnostics(self.root, state)
        codes = {f["code"] for f in report["findings"]}
        self.assertTrue({"ENGINEERING_PACKAGE_DRAFT", "ENGINEERING_INDEX_INCOMPLETE"}.issubset(codes))
        self.assertFalse(report["requirements"][0]["can_start_full_build"])

    def test_custom_docs_root_and_ignored_superseded_requirement(self) -> None:
        state = self.confirmed_snapshot()
        config = self.root / ".project-to-act" / "PROJECT_CONFIG.json"
        config.write_text(json.dumps({"schema_version": 2, "mode": "managed", "canonical_docs_root": "project-docs"}), encoding="utf-8")
        package = self.make_package(state)
        report = pm.delivery_diagnostics(self.root, state)
        self.assertEqual(report["requirements"][0]["package_path"], str(package))
        self.assertNotIn("ENGINEERING_PACKAGE_MISSING", [f["code"] for f in report["findings"]])
        state["requirements"][0]["status"] = "superseded"
        self.assertEqual(pm.delivery_diagnostics(self.root, state)["requirements"], [])

    def test_filtered_report_write_preserves_full_project_and_state(self) -> None:
        self.seed_requirement()
        self.apply({"expected_revision": 1, "operations": [{"op": "add", "collection": "requirements", "record": {
            "id": "REQ-002", "title": "另一项需求", "source_refs": ["SRC-001"], "status": "pending_confirmation",
        }}]})
        before = pm.state_path(self.root).read_bytes()
        report = pm.cmd_doctor(argparse.Namespace(project_root=self.root, requirement=["REQ-001"], write_report=True))
        self.assertEqual([r["id"] for r in report["requirements"]], ["REQ-001"])
        saved = Path(report["report_path"]).read_text(encoding="utf-8")
        self.assertIn("REQ-002", saved)
        self.assertEqual(pm.state_path(self.root).read_bytes(), before)

    def test_locked_contract_enrichment_preserves_existing_behavior(self) -> None:
        contract = {"functional_decomposition": ["locked behavior"], "implementation_surfaces": [{"name": "surface", "decision": "locked"}]}
        enriched = copy.deepcopy(contract)
        enriched["implementation_surfaces"][0]["execution_steps"] = ["inspect", "implement"]
        self.assertTrue(pm.additive_implementation_contract_update(contract, enriched))
        changed = copy.deepcopy(enriched)
        changed["functional_decomposition"] = ["new behavior"]
        self.assertFalse(pm.additive_implementation_contract_update(contract, changed))
        changed = copy.deepcopy(enriched)
        changed["new_product_rule"] = "not authorized"
        self.assertFalse(pm.additive_implementation_contract_update(contract, changed))
        changed = copy.deepcopy(enriched)
        changed["implementation_surfaces"][0]["new_behavior"] = "not authorized"
        self.assertFalse(pm.additive_implementation_contract_update(contract, changed))

    def test_package_copied_into_new_directory_requires_identity_review(self) -> None:
        state = self.confirmed_snapshot()
        package = self.make_package(state)
        package.write_text("# REQ-001 v0\n旧版本包未经重新调查", encoding="utf-8")
        report = pm.delivery_diagnostics(self.root, state)
        self.assertIn("ENGINEERING_PACKAGE_IDENTITY_UNVERIFIED", [f["code"] for f in report["findings"]])

    def test_gate_promotion_waits_for_package_and_work_packages(self) -> None:
        from unittest.mock import patch
        state = self.confirmed_snapshot()
        with patch.object(pm, "shaping_gaps", return_value=[]):
            report = pm.delivery_diagnostics(self.root, state)
        action = next(f for f in report["findings"] if f["code"] == "BUILD_GATE_REVIEW_PENDING")
        self.assertTrue(action["depends_on"])
        self.assertFalse(action["ready_to_execute"])
        self.assertNotIn(action["id"], report["ready_action_ids"])
        self.assertIn("REQ-001:ENGINEERING_PACKAGE_MISSING", report["ready_action_ids"])


if __name__ == "__main__":
    unittest.main()
