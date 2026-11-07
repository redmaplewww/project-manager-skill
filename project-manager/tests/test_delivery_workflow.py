from __future__ import annotations

import argparse
import copy
import json
import unittest

import test_pm as legacy

pm = legacy.pm


class DeliveryWorkflowTests(unittest.TestCase):
    setUp_base = legacy.ProjectManagerTests.setUp
    tearDown = legacy.ProjectManagerTests.tearDown
    seed_requirement = legacy.ProjectManagerTests.seed_requirement
    apply = legacy.ProjectManagerTests.apply
    write_changes = legacy.ProjectManagerTests.write_changes
    make_package = legacy.ProjectManagerTests.make_package

    def setUp(self):
        self.setUp_base()
        legacy.ProjectManagerTests.test_build_readiness_accepts_complete_feature_shaping_gate(self)
        self.req = pm.load_state(self.root)["requirements"][0]
        self.original_lock = copy.deepcopy(self.req["confirmation_lock"])
        work = pm.load_state(self.root)["work_packages"][0]
        self.operations([{"op": "revise", "collection": "work_packages", "id": work["id"], "expected_version": work["version"],
                          "patch": {"owner": "Codex", "completion_conditions": ["submitted records are saved and visible"]}}])
        for name, content in {
            "app.py": "def submit(): return 'saved'\n", "ui.js": "function submit() { return 'saved'; }\n",
            "test_submit.py": "assert True\n", "review.md": "Reviewed saved result, error recovery, isolation and keyboard behavior.\n",
            "result.json": '{"passed": 1}\n',
        }.items():
            (self.root / name).write_text(content, encoding="utf-8")
        self.configure()
        self.create_manifest()

    def operations(self, ops, dry_run=False):
        return self.apply({"expected_revision": pm.load_state(self.root)["revision"], "operations": ops}, dry_run)

    def configure(self, extra=None):
        cfg = {"profile": "full_product", "scope": [{"id": "REQ-001", "version": 3, "mode": "full", "ui_required": True}]}
        if extra:
            cfg.update(extra)
        return self.operations([{"op": "configure_delivery", "source_ref": "SRC-001", "config": cfg}])

    def create_manifest(self, mode="automated"):
        package, self.manifest_path = pm.workflow.package_files(self.root, self.req)
        ref = package.relative_to(self.root).as_posix()
        self.manifest = {
            "requirement_id": "REQ-001", "requirement_version": 3, "package_revision": 1,
            "design": {k: {"summary": f"Reviewed concrete {k} design for submitting and restoring records", "document_refs": [ref]}
                       for k in ("architecture", "interfaces_data", "frontend", "execution", "verification", "rollback")},
            "traceability": [{"ac_id": "AC-001", "design_section": "submit and view", "test_plan": "submit saved record, view it and recover failure", "work_package_ids": ["WP-001"], "required_evidence": mode}],
        }
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def review_op(self, stage, **overrides):
        op = {"op": "review_stage", "requirement_id": "REQ-001", "expected_version": 3, "source_ref": "SRC-001",
              "stage": stage, "result": "passed", "reviewed_by": "Codex acting as receiving reviewer",
              "handoff_from": "producer", "handoff_to": "receiving reviewer", "conclusion": "Reviewed the scoped content and recorded evidence",
              "next_action": "Continue the next authorized stage", "artifact_refs": ["review.md"],
              "checks": {k: {"result": "passed", "reason": "Observed the specific expected behavior and failure recovery", "artifact_refs": ["review.md"]} for k in pm.workflow.CHECKS[stage]}}
        return {**op, **overrides}

    def review(self, stage, **overrides):
        return self.operations([self.review_op(stage, **overrides)])

    def prepare(self):
        self.review("specification")
        self.review("engineering")

    def coverage_op(self, **changes):
        row = {"ac_id": "AC-001", "implementation": "complete", "frontend": "complete", "verification": "not_run",
               "code_refs": ["app.py", "ui.js"], "entry_points": "submit(), UI submit action", "test_refs": ["test_submit.py"]}
        row.update(changes)
        return {"op": "record_coverage", "requirement_id": "REQ-001", "expected_version": 3, "source_ref": "SRC-001", "rows": [row]}

    def add_evidence(self, mode="automated"):
        evidence = {"id": "EVD-DELIVERY", "kind": "test", "status": "passed", "related_ids": ["REQ-001"],
                    "delivery": {"requirement_id": "REQ-001", "requirement_version": 3, "ac_ids": ["AC-001"],
                                 "command": "python test_submit.py", "environment": "isolated local test fixture", "executed_at": pm.now_iso(), "exit_code": 0, "mode": mode,
                                 "artifact_fingerprints": pm.workflow.pin(self.root, ["result.json"]),
                                 "code_fingerprints": pm.workflow.pin(self.root, ["app.py", "ui.js"]),
                                 "test_fingerprints": pm.workflow.pin(self.root, ["test_submit.py"])}}
        self.operations([{"op": "add", "collection": "evidence", "record": evidence}])

    def implement(self):
        self.prepare()
        self.operations([self.coverage_op()])
        self.review("implementation")

    def verify(self):
        self.implement()
        self.add_evidence()
        self.operations([self.coverage_op(verification="passed", evidence_ids=["EVD-DELIVERY"])])
        self.review("verification")

    def report(self):
        return pm.workflow.report(self.root, pm.load_state(self.root))

    def test_locked_requirement_reaches_acceptance_without_unlocking_or_version_change(self):
        self.verify()
        req = pm.load_state(self.root)["requirements"][0]
        self.assertEqual(req["readiness"], "ready_for_acceptance")
        self.assertEqual(req["confirmation_lock"], self.original_lock)
        self.assertEqual(req["version"], 3)
        self.assertEqual(req["status"], "confirmed")
        self.operations([{"op": "add", "collection": "sources", "record": {"id": "SRC-ACCEPT", "kind": "user_message", "title": "Product acceptance", "acceptance_scope": {"REQ-001": 3}}}])
        self.review("acceptance", source_ref="SRC-ACCEPT", acceptance_statement="I accept the submitted and viewed record behavior for REQ-001 v3")
        self.assertTrue(all(v == "passed" for v in self.report()["stage_results"].values()))

    def test_legacy_green_records_are_unassessed_until_reviewed(self):
        report = self.report()
        self.assertEqual(report["totals"]["implementation"], {"not_assessed": 1})
        self.assertEqual(report["requirements"][0]["next_stage"], "specification")
        self.assertEqual(report["totals"]["unassessed_stage_reviews"], 5)

    def test_engineering_handoff_requires_content_manifest(self):
        self.review("specification")
        self.manifest_path.write_text('{}', encoding="utf-8")
        with self.assertRaisesRegex(pm.PMError, "manifest"):
            self.review("engineering")

    def test_package_does_not_require_product_code_before_handoff(self):
        (self.root / "app.py").unlink()
        (self.root / "ui.js").unlink()
        self.prepare()
        self.assertEqual(self.report()["stage_results"]["engineering"], "passed")

    def test_missing_frontend_blocks_full_implementation(self):
        self.prepare()
        self.operations([self.coverage_op(frontend="missing")])
        with self.assertRaisesRegex(pm.PMError, "frontend incomplete"):
            self.review("implementation")

    def test_frontend_not_applicable_requires_reason(self):
        with self.assertRaisesRegex(pm.PMError, "requires a reason"):
            self.operations([self.coverage_op(frontend="not_required")])

    def test_partial_slice_cannot_complete_whole_requirement(self):
        self.prepare()
        self.operations([self.coverage_op(implementation="partial")])
        with self.assertRaisesRegex(pm.PMError, "implementation=partial"):
            self.review("implementation")

    def test_stage_order_is_enforced(self):
        with self.assertRaisesRegex(pm.PMError, "prerequisite"):
            self.review("engineering")

    def test_code_change_invalidates_implementation_and_verification(self):
        self.verify()
        (self.root / "app.py").write_text("changed code", encoding="utf-8")
        row = self.report()["requirements"][0]
        self.assertEqual(row["stages"]["engineering"]["result"], "passed")
        self.assertEqual(row["stages"]["implementation"]["result"], "needs_review")
        self.assertEqual(row["coverage"][0]["verification"], "needs_review")

    def test_old_green_test_cannot_be_rebound_to_new_code(self):
        self.verify()
        (self.root / "app.py").write_text("new code", encoding="utf-8")
        with self.assertRaisesRegex(pm.PMError, "executed delivery evidence"):
            self.operations([self.coverage_op(verification="passed", evidence_ids=["EVD-DELIVERY"])])

    def test_report_changes_invalidate_evidence(self):
        self.verify()
        (self.root / "result.json").write_text('{"passed":0}', encoding="utf-8")
        self.assertEqual(self.report()["requirements"][0]["coverage"][0]["verification"], "needs_review")

    def test_simulated_evidence_does_not_pass_real_external_plan(self):
        self.create_manifest(mode="real_external")
        self.implement()
        self.add_evidence(mode="simulated")
        self.operations([self.coverage_op(verification="passed", evidence_ids=["EVD-DELIVERY"])])
        with self.assertRaisesRegex(pm.PMError, "verification=needs_review"):
            self.review("verification")

    def test_requirement_confirmation_is_not_product_acceptance(self):
        self.verify()
        with self.assertRaisesRegex(pm.PMError, "acceptance authority"):
            self.review("acceptance", acceptance_statement="reuse requirement confirmation")

    def test_unknown_verification_is_a_failing_stage_check(self):
        result, code = pm.cmd_check(argparse.Namespace(project_root=self.root, strict=True, stage="verification", requirement=["REQ-001"]))
        self.assertEqual(code, 1)
        self.assertEqual(result["stage_results"]["verification"], "not_passed")
        self.assertTrue(result["structure_valid"])

    def test_coverage_cannot_invent_or_hide_acceptance_scenarios(self):
        with self.assertRaisesRegex(pm.PMError, "unknown acceptance"):
            self.operations([self.coverage_op(ac_id="AC-NONEXISTENT")])
        self.manifest["traceability"] = []
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.review("specification")
        with self.assertRaisesRegex(pm.PMError, "every AC"):
            self.review("engineering")

    def test_generic_revise_cannot_write_stage_success(self):
        with self.assertRaisesRegex(pm.PMError, "use delivery operations"):
            self.operations([{"op": "revise", "collection": "requirements", "id": "REQ-001", "expected_version": 3, "patch": {"delivery": {"receipts": {"verification": {"result": "passed"}}}}}])

    def test_explicit_gap_blocks_its_stage_and_closing_needs_evidence(self):
        self.prepare()
        gap = {"id": "GAP-001", "stage": "implementation", "kind": "external", "status": "open", "reason": "external fixture unavailable", "owner": "Codex", "action": "restore fixture and retry", "target": "app.py", "done_when": "fixture executes successfully"}
        self.operations([{"op": "record_delivery_gap", "requirement_id": "REQ-001", "expected_version": 3, "source_ref": "SRC-001", "gap": gap}])
        self.operations([self.coverage_op()])
        with self.assertRaisesRegex(pm.PMError, "GAP-001"):
            self.review("implementation")
        self.assertEqual(self.report()["stage_results"]["engineering"], "passed")
        gap["status"] = "resolved"
        with self.assertRaisesRegex(pm.PMError, "nonempty list"):
            self.operations([{"op": "record_delivery_gap", "requirement_id": "REQ-001", "expected_version": 3, "source_ref": "SRC-001", "gap": gap}])

    def test_dry_run_and_failed_transaction_do_not_write_receipts(self):
        state_before = pm.state_path(self.root).read_bytes()
        self.operations([self.review_op("specification")], dry_run=True)
        self.assertEqual(pm.state_path(self.root).read_bytes(), state_before)
        with self.assertRaises(pm.PMError):
            self.operations([self.review_op("specification"), self.review_op("verification")])
        self.assertEqual(pm.state_path(self.root).read_bytes(), state_before)

    def test_unrelated_revision_does_not_invalidate_receipts(self):
        self.prepare()
        self.operations([{"op": "add", "collection": "sources", "record": {"id": "SRC-UNRELATED", "kind": "note", "title": "Unrelated work"}}])
        self.assertEqual(self.report()["stage_results"]["engineering"], "passed")

    def test_added_quality_check_requires_reassessment(self):
        self.prepare()
        self.configure({"additional_checks": {"engineering": ["custom_latency_budget"]}})
        self.assertEqual(self.report()["requirements"][0]["stages"]["engineering"]["result"], "needs_review")

    def test_path_traversal_is_rejected_without_state_change(self):
        before = pm.state_path(self.root).read_bytes()
        with self.assertRaisesRegex(pm.PMError, "escapes project"):
            self.review("specification", artifact_refs=["../external.md"])
        self.assertEqual(pm.state_path(self.root).read_bytes(), before)

    def test_work_package_cannot_be_done_with_partial_or_unverified_implementation(self):
        self.prepare()
        self.operations([{"op": "transition", "collection": "work_packages", "id": "WP-001", "to": "in_progress"}])
        self.operations([self.coverage_op()])
        with self.assertRaisesRegex(pm.PMError, "current passing verification"):
            self.operations([{"op": "transition", "collection": "work_packages", "id": "WP-001", "to": "done"}])

    def test_failed_receipt_does_not_discard_coverage_and_recovery_can_continue(self):
        self.prepare()
        self.operations([self.coverage_op()])
        self.review("implementation", result="failed", conclusion="Found a specific defect", next_action="Repair the implementation defect")
        self.assertEqual(self.report()["totals"]["implementation"], {"complete": 1})
        self.review("implementation")
        self.assertEqual(self.report()["stage_results"]["implementation"], "passed")

    def test_bad_manifest_shape_is_a_reportable_gap_not_an_unhandled_exception(self):
        self.manifest_path.write_text('[]', encoding="utf-8")
        data = self.report()
        self.assertIn("manifest", ";".join(data["requirements"][0]["stages"]["engineering"]["gaps"]))

    def test_doctor_exposes_next_stage_even_when_legacy_structure_is_green(self):
        data = pm.cmd_doctor(argparse.Namespace(project_root=self.root, requirement=["REQ-001"], write_report=False))
        self.assertTrue(any(f["code"] == "DELIVERY_STAGE_SPECIFICATION" for f in data["findings"]))
        self.assertTrue(data["ready_action_ids"])

    def test_scope_selection_preserves_independent_ready_requirement(self):
        self.prepare()
        state = pm.load_state(self.root)
        clone = copy.deepcopy(state["requirements"][0])
        clone["id"] = "REQ-002"
        clone.pop("delivery", None)
        state["requirements"].append(clone)
        state["project"]["delivery_workflow"]["scope"].append({"id": "REQ-002", "version": 3, "mode": "full", "ui_required": True})
        all_rows = pm.workflow.report(self.root, state)["requirements"]
        self.assertEqual(all_rows[0]["stages"]["engineering"]["result"], "passed")
        self.assertEqual(all_rows[1]["stages"]["engineering"]["result"], "not_assessed")
        scoped = pm.workflow.report(self.root, state, ["REQ-001"])
        self.assertEqual(scoped["stage_results"]["engineering"], "passed")

    def test_future_or_undated_evidence_does_not_pass(self):
        self.implement()
        self.add_evidence()
        e = pm.load_state(self.root)["evidence"][0]
        payload = copy.deepcopy(e["delivery"])
        payload["executed_at"] = "2999-01-01T00:00:00Z"
        self.operations([{"op": "revise", "collection": "evidence", "id": e["id"], "expected_version": e["version"], "patch": {"delivery": payload}}])
        with self.assertRaisesRegex(pm.PMError, "executed delivery evidence"):
            self.operations([self.coverage_op(verification="passed", evidence_ids=[e["id"]])])

    def test_project_closure_requires_real_product_acceptance(self):
        self.verify()
        with self.assertRaisesRegex(pm.PMError, "project closure requires"):
            self.operations([{"op": "set_project", "patch": {"stage": "closed"}}])

    def test_failed_check_is_not_saved_as_a_successful_review(self):
        op = self.review_op("specification")
        op["checks"]["behavior"]["result"] = "failed"
        before = pm.state_path(self.root).read_bytes()
        with self.assertRaisesRegex(pm.PMError, "behavior"):
            self.operations([op])
        self.assertEqual(before, pm.state_path(self.root).read_bytes())

    def test_coverage_update_cannot_claim_product_acceptance(self):
        with self.assertRaisesRegex(pm.PMError, "acceptance is recorded only"):
            self.operations([self.coverage_op(acceptance="passed")])

    def test_empty_package_cannot_be_signed_even_with_manifest(self):
        self.review("specification")
        package, _ = pm.workflow.package_files(self.root, self.req)
        package.write_text("   \n", encoding="utf-8")
        with self.assertRaisesRegex(pm.PMError, "empty or an unfinished draft"):
            self.review("engineering")

    def test_engineering_backfill_preserves_existing_acceptance_readiness(self):
        state = pm.load_state(self.root)
        state["requirements"][0]["readiness"] = "ready_for_acceptance"
        pm.atomic_json(pm.state_path(self.root), state)
        self.prepare()
        result = pm.load_state(self.root)["requirements"][0]
        self.assertEqual(result["readiness"], "ready_for_acceptance")
        self.assertEqual(result["confirmation_lock"], self.original_lock)
        self.assertEqual(result["version"], 3)


if __name__ == "__main__":
    unittest.main()
