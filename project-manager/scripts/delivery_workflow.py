"""Versioned delivery receipts and coverage; persisted only in PM's existing state.

Checks enforce traceability and freshness, not the truth of a reviewer's claims.
Document content and real user behavior still require an explicit recorded review.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STAGES = ("specification", "engineering", "implementation", "verification", "acceptance")
CHECKS = {
    "specification": ("scope", "behavior", "exceptions", "acceptance_design", "dependencies"),
    "engineering": ("repository", "architecture", "interfaces_data", "frontend_design", "execution_plan", "verification_plan", "rollback"),
    "implementation": ("behavior_coverage", "frontend", "migration_config", "error_recovery"),
    "verification": ("scenario_coverage", "integration", "system_ui", "source_audit", "runtime"),
    "acceptance": ("user_scenarios", "operating_instructions", "known_limits"),
}
OPERATIONS = {"configure_delivery", "record_coverage", "review_stage", "record_delivery_gap"}
REVIEW_RESULTS = {"passed", "failed", "blocked"}
IMPLEMENTATION = {"not_started", "partial", "complete"}
VERIFICATION = {"not_run", "passed", "failed", "blocked"}
KINDS = {"product_decision", "engineering", "implementation", "verification", "external", "acceptance"}
_HASH_CACHE = ContextVar("delivery_hash_cache", default=None)


class WorkflowError(ValueError):
    pass


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require(condition: Any, message: str) -> None:
    if not condition:
        raise WorkflowError(message)


def text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.strip().lower() not in {"todo", "tbd", "unknown", "待补齐", "待定", "..."}


def config(state: dict) -> dict:
    return state["project"].get("delivery_workflow") or {}


def source(state: dict, ref: str) -> dict:
    result = next((s for s in state["sources"] if s["id"] == ref), None)
    require(result, f"unknown delivery source: {ref}")
    return result


def requirement(state: dict, rid: str) -> dict:
    result = next((r for r in state["requirements"] if r["id"] == rid), None)
    require(result, f"unknown requirement: {rid}")
    return result


def scope_entry(state: dict, req: dict) -> dict:
    require(bool(config(state)), "delivery stages are not configured; preserve existing work and register scope before reviewing")
    entry = next((r for r in config(state).get("scope", []) if r["id"] == req["id"]), None)
    require(entry, f"{req['id']} is outside the configured delivery scope")
    require(entry["version"] == req.get("version", 1), f"{req['id']} delivery scope version is stale")
    return entry


def resolve_file(root: Path, ref: str) -> Path:
    require(text(ref), "artifact path must be nonempty")
    require(not Path(ref).is_absolute() and not ref.startswith(("/", "\\")), "artifact paths must be project-relative")
    p = (root / ref).resolve()
    require(p.is_relative_to(root.resolve()), f"artifact escapes project: {ref}")
    require(p.is_file(), f"artifact missing: {ref}")
    return p


def pin(root: Path, refs: list) -> dict[str, str]:
    require(isinstance(refs, list) and bool(refs), "artifact/code/test references must be a nonempty list")
    result = {}
    cache = _HASH_CACHE.get()
    for ref in refs:
        p = resolve_file(root, ref)
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        if cache is not None and key in cache:
            result[ref] = cache[key]
        else:
            result[ref] = hashlib.sha256(p.read_bytes()).hexdigest()
            if cache is not None:
                cache[key] = result[ref]
    return result


def fresh(root: Path, fingerprints: Any) -> bool:
    if not isinstance(fingerprints, dict) or not fingerprints:
        return False
    try:
        return pin(root, list(fingerprints)) == fingerprints
    except (WorkflowError, OSError):
        return False


def spec_hash(req: dict) -> str:
    # Operational metadata, gate labels and code mappings do not change product scope.
    names = ("id", "version", "title", "user_value", "trigger", "behavior", "outcome", "boundaries",
             "acceptance_criteria", "decision_questions", "open_questions", "product_form", "experience_review",
             "technical_design", "implementation_contract", "acceptance_scenarios", "interface_spec", "extension_contract")
    return digest({k: req.get(k) for k in names})


def ac_ids(req: dict) -> list[str]:
    rows = req.get("acceptance_scenarios") or []
    ids = [r.get("id") for r in rows if isinstance(r, dict)]
    require(len(ids) == len(rows) and bool(ids) and all(text(i) for i in ids) and len(set(ids)) == len(ids),
            f"{req['id']} requires nonempty, unique acceptance scenario IDs")
    return ids


def docs_root(root: Path) -> Path:
    p = root / ".project-to-act/PROJECT_CONFIG.json"
    cfg = json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
    folder = (root / (cfg.get("canonical_docs_root") or ".project-to-act/docs")).resolve()
    require(folder.is_relative_to(root.resolve()), "canonical docs root escapes project")
    return folder


def package_files(root: Path, req: dict) -> tuple[Path, Path]:
    folder = docs_root(root) / "project-development/packages" / req["id"] / f"v{req.get('version', 1)}"
    return folder / "ENGINEERING-PACKAGE.md", folder / "ENGINEERING-PACKAGE.json"


def engineering_manifest(root: Path, state: dict, req: dict) -> tuple[dict, list[str]]:
    doc, manifest_path = package_files(root, req)
    require(doc.is_file() and manifest_path.is_file(), f"{req['id']} needs engineering package and content manifest")
    body = doc.read_text(encoding="utf-8-sig")
    require(bool(body.strip()) and "<!-- pm:package-draft -->" not in body, "engineering package is empty or an unfinished draft")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"invalid engineering manifest: {exc}") from exc
    require(isinstance(manifest, dict) and isinstance(manifest.get("design"), dict), "engineering manifest must contain a design object")
    require(manifest.get("requirement_id") == req["id"] and manifest.get("requirement_version") == req.get("version", 1),
            "engineering manifest requirement identity/version mismatch")
    require(isinstance(manifest.get("package_revision"), int) and manifest["package_revision"] > 0, "package_revision must be positive")
    refs = [doc.relative_to(root.resolve()).as_posix(), manifest_path.relative_to(root.resolve()).as_posix()]
    for key in ("architecture", "interfaces_data", "frontend", "execution", "verification", "rollback"):
        section = manifest.get("design", {}).get(key, {})
        require(isinstance(section, dict), f"engineering design.{key} must be an object")
        require(text(section.get("summary")), f"engineering design.{key} content is missing")
        refs.extend(pin(root, section.get("document_refs", [])).keys())
    rows = manifest.get("traceability")
    require(isinstance(rows, list) and len(rows) == len(ac_ids(req)), "engineering traceability must cover every AC exactly once")
    require(all(isinstance(r, dict) for r in rows), "engineering traceability rows must be objects")
    require({r.get("ac_id") for r in rows} == set(ac_ids(req)), "engineering traceability has missing or unknown AC IDs")
    work = {w["id"]: w for w in state["work_packages"]}
    for row in rows:
        require(text(row.get("design_section")) and text(row.get("test_plan")), f"{row.get('ac_id')} lacks design/test plan")
        require(row.get("required_evidence") in {"automated", "real_external", "manual", "simulated"}, "required_evidence must be explicit")
        wids = row.get("work_package_ids")
        require(isinstance(wids, list) and bool(wids), "each AC needs executable work packages")
        for wid in wids:
            w = work.get(wid, {})
            require(req["id"] in w.get("requirement_ids", []) and w.get("status") != "cancelled"
                    and w.get("kind") in {"implementation", "verification", "foundation"}, f"invalid engineering work package: {wid}")
            require(text(w.get("owner")) and text(w.get("next_action")) and bool(w.get("completion_conditions") or w.get("completion_criteria") or w.get("acceptance_criteria")),
                    f"{wid} needs owner, action and completion conditions")
    return manifest, sorted(set(refs))


def evidence_ok(root: Path, state: dict, req: dict, acid: str, ids: Any, mode: str | None = None,
                expected_code: dict | None = None, expected_tests: dict | None = None) -> bool:
    if not isinstance(ids, list) or not ids:
        return False
    evidence = {e["id"]: e for e in state["evidence"]}
    modes = set()
    for eid in ids:
        e = evidence.get(eid, {})
        d = e.get("delivery") or {}
        try:
            executed = datetime.fromisoformat(str(d.get("executed_at", "")).replace("Z", "+00:00"))
            if executed.tzinfo is None or executed > datetime.now(timezone.utc):
                return False
        except ValueError:
            return False
        if (e.get("status") != "passed" or d.get("requirement_id") != req["id"]
                or d.get("requirement_version") != req.get("version", 1) or acid not in d.get("ac_ids", [])
                or type(d.get("exit_code")) is not int or d.get("exit_code") != 0 or not all(text(d.get(k)) for k in ("command", "environment", "executed_at"))
                or d.get("mode") not in {"automated", "real_external", "manual", "simulated"}
                or not fresh(root, d.get("artifact_fingerprints"))
                or not fresh(root, d.get("code_fingerprints")) or not fresh(root, d.get("test_fingerprints"))):
            return False
        if any(d.get("code_fingerprints", {}).get(k) != v for k, v in (expected_code or {}).items()):
            return False
        if any(d.get("test_fingerprints", {}).get(k) != v for k, v in (expected_tests or {}).items()):
            return False
        modes.add(d["mode"])
    return mode is None or mode in modes


def coverage_rows(root: Path, state: dict, req: dict) -> list[dict]:
    stored = req.get("delivery", {}).get("coverage", {})
    rows = []
    try:
        manifest, _ = engineering_manifest(root, state, req)
        modes = {r["ac_id"]: r["required_evidence"] for r in manifest["traceability"]}
    except WorkflowError:
        modes = {}
    for acid in ac_ids(req):
        row = copy.deepcopy(stored.get(acid, {}))
        valid_version = row.get("requirement_version") == req.get("version", 1) and row.get("spec_hash") == spec_hash(req)
        code_fresh = valid_version and fresh(root, row.get("code_fingerprints"))
        tests_fresh = valid_version and fresh(root, row.get("test_fingerprints"))
        implementation = row.get("implementation", "not_assessed") if valid_version else "not_assessed"
        verification = row.get("verification", "not_assessed") if valid_version else "not_assessed"
        if implementation == "complete" and not code_fresh:
            implementation = "needs_review"
        if verification == "passed" and not (code_fresh and tests_fresh and evidence_ok(root, state, req, acid, row.get("evidence_ids"), modes.get(acid), row.get("code_fingerprints"), row.get("test_fingerprints"))):
            verification = "needs_review"
        rows.append({**row, "ac_id": acid, "implementation": implementation, "verification": verification,
                     "frontend": row.get("frontend", "not_assessed") if valid_version else "not_assessed"})
    return rows


def check_gaps(root: Path, state: dict, req: dict, stage: str) -> list[str]:
    gaps = []
    try:
        entry = scope_entry(state, req)
        ids = ac_ids(req)
        require(req.get("status") in {"confirmed", "ai_confirmed_unverified"}, "product scope is not confirmed")
        require(not req.get("open_questions") and not any(q.get("status") in {"open", "deferred"} for q in req.get("decision_questions", [])), "product decisions remain open")
        if stage == "engineering":
            engineering_manifest(root, state, req)
        if stage in {"implementation", "verification", "acceptance"}:
            for row in coverage_rows(root, state, req):
                if row["implementation"] != "complete":
                    gaps.append(f"{row['ac_id']}: implementation={row['implementation']}")
                if entry["ui_required"] and row["frontend"] != "complete" and not (row["frontend"] == "not_required" and text(row.get("frontend_reason"))):
                    gaps.append(f"{row['ac_id']}: frontend incomplete or applicability unexplained")
                if stage in {"verification", "acceptance"} and row["verification"] != "passed":
                    gaps.append(f"{row['ac_id']}: verification={row['verification']}")
        for gap in req.get("delivery", {}).get("gaps", {}).values():
            if gap.get("stage") == stage and (gap.get("status") == "open" or not fresh(root, gap.get("artifact_fingerprints"))):
                gaps.append(f"{gap['id']}: {gap['reason']}")
    except (WorkflowError, KeyError, TypeError) as exc:
        gaps.append(str(exc))
    return gaps


def stage_state(root: Path, state: dict, req: dict, stage: str) -> dict:
    index = STAGES.index(stage)
    previous = stage_state(root, state, req, STAGES[index - 1]) if index else None
    receipt = req.get("delivery", {}).get("receipts", {}).get(stage)
    gaps = check_gaps(root, state, req, stage)
    if previous and previous["result"] != "passed":
        gaps.insert(0, f"prerequisite {STAGES[index - 1]}={previous['result']}")
    if not receipt:
        return {"result": "not_assessed", "gaps": gaps or ["stage content review and handoff receipt missing"]}
    result = receipt["result"]
    if result == "passed" and (gaps or receipt.get("spec_hash") != spec_hash(req)
                               or receipt.get("scope_hash") != digest({"entry": next((e for e in config(state).get("scope", []) if e["id"] == req["id"]), None), "additional_checks": config(state).get("additional_checks", {})})
                               or not fresh(root, receipt.get("artifact_fingerprints"))
                               or (previous and receipt.get("prerequisite_receipt_id") != previous.get("receipt_id"))):
        result = "needs_review"
        if not gaps:
            gaps.append("review inputs/artifacts or prerequisite receipt changed")
    return {"result": result, "receipt_id": receipt["id"], "gaps": gaps,
            "reviewed_by": receipt["reviewed_by"], "next_action": receipt.get("next_action")}


def configure(state: dict, op: dict, stamp: str) -> dict:
    source(state, op.get("source_ref"))
    incoming = op.get("config")
    require(isinstance(incoming, dict) and incoming.get("profile") in {"full_product", "foundation"}, "delivery profile must be explicit")
    scope = incoming.get("scope")
    require(isinstance(scope, list) and bool(scope), "delivery scope cannot be empty")
    require(all(isinstance(r, dict) for r in scope), "delivery scope entries must be objects")
    require(len({r.get("id") for r in scope}) == len(scope), "duplicate requirement in delivery scope")
    for item in scope:
        req = requirement(state, item["id"])
        require(item.get("version") == req.get("version", 1) and req.get("status") not in {"superseded", "rejected"}, "delivery scope must use active current versions")
        require(isinstance(item.get("ui_required"), bool), "ui_required must be explicit for each requirement")
        require(item.get("mode") in {"full", "foundation"}, "scope mode must be full or foundation")
        if item["mode"] == "full":
            require(req.get("readiness") != "confirmed_for_extension", "extension scope only permits foundation delivery")
        if incoming["profile"] == "full_product":
            require(item["mode"] == "full", "full_product profile cannot silently reduce a requirement to foundation scope")
    extra = incoming.get("additional_checks", {})
    require(isinstance(extra, dict) and all(k in STAGES and isinstance(v, list) and all(text(x) for x in v) for k, v in extra.items()), "invalid additional checks")
    # Required checks cannot be disabled by configuration.
    old = copy.deepcopy(config(state))
    if old:
        state["project"].setdefault("delivery_workflow_history", []).append(old)
    state["project"]["delivery_workflow"] = {"schema_version": 1, "profile": incoming["profile"], "scope": scope,
        "stages": list(STAGES), "additional_checks": extra, "source_ref": op["source_ref"], "configured_at": stamp}
    return {"op": "configure_delivery", "scope_count": len(scope)}


def apply(root: Path, state: dict, op: dict, stamp: str) -> dict:
    if op["op"] == "configure_delivery":
        return configure(state, op, stamp)
    require(bool(config(state)), "configure_delivery is required before stage operations")
    req = requirement(state, op.get("requirement_id"))
    scope_entry(state, req)
    require(op.get("expected_version") == req.get("version", 1), "stale requirement version for delivery operation")
    source(state, op.get("source_ref"))
    delivery = req.setdefault("delivery", {})
    if op["op"] == "record_delivery_gap":
        gap = copy.deepcopy(op.get("gap", {}))
        require(text(gap.get("id")) and gap.get("stage") in STAGES and gap.get("kind") in KINDS, "invalid delivery gap identity/stage/kind")
        require(gap.get("status") in {"open", "resolved"} and all(text(gap.get(k)) for k in ("reason", "owner", "action", "target", "done_when")), "gap needs reason, owner, action, target and closing condition")
        if gap["status"] == "resolved":
            gap["artifact_fingerprints"] = pin(root, gap.get("artifact_refs", []))
        old = delivery.setdefault("gaps", {}).get(gap["id"])
        if old:
            delivery.setdefault("history", []).append({"type": "gap", "value": old})
        delivery["gaps"][gap["id"]] = {**gap, "source_ref": op["source_ref"], "updated_at": stamp}
        return {"op": op["op"], "id": req["id"], "gap_id": gap["id"]}
    if op["op"] == "record_coverage":
        rows = op.get("rows")
        require(isinstance(rows, list) and bool(rows), "coverage rows cannot be empty")
        require(all(isinstance(r, dict) for r in rows), "coverage rows must be objects")
        ids = ac_ids(req)
        require(len({r.get("ac_id") for r in rows}) == len(rows), "duplicate coverage rows")
        for incoming in rows:
            row = copy.deepcopy(incoming)
            require(row.get("ac_id") in ids, "coverage refers to unknown acceptance scenario")
            require(row.get("implementation") in IMPLEMENTATION and row.get("verification") in VERIFICATION, "invalid implementation/verification status")
            require(row.get("frontend") in {"complete", "partial", "missing", "not_required"}, "frontend status is required")
            if row["frontend"] == "not_required":
                require(text(row.get("frontend_reason")), "frontend not_required requires a reason")
            if row["implementation"] == "complete":
                row["code_fingerprints"] = pin(root, row.get("code_refs", []))
                require(text(row.get("entry_points")), "complete implementation requires specific entry points")
            if row["verification"] == "passed":
                require(row["implementation"] == "complete", "verification cannot pass an incomplete implementation")
                row["test_fingerprints"] = pin(root, row.get("test_refs", []))
                require(evidence_ok(root, state, req, row["ac_id"], row.get("evidence_ids"), expected_code=row.get("code_fingerprints"), expected_tests=row.get("test_fingerprints")), "verification needs current, executed delivery evidence")
            row.update(requirement_version=req.get("version", 1), spec_hash=spec_hash(req), source_ref=op["source_ref"], recorded_at=stamp)
            require("acceptance" not in row, "acceptance is recorded only through an authorized acceptance stage review")
            old = delivery.setdefault("coverage", {}).get(row["ac_id"])
            if old:
                delivery.setdefault("history", []).append({"type": "coverage", "value": old})
            delivery["coverage"][row["ac_id"]] = row
        return {"op": op["op"], "id": req["id"], "coverage_count": len(rows)}
    require(op["op"] == "review_stage", "unsupported delivery operation")
    stage, result = op.get("stage"), op.get("result")
    require(stage in STAGES and result in REVIEW_RESULTS, "invalid stage/review result")
    require(all(text(op.get(k)) for k in ("reviewed_by", "handoff_from", "handoff_to", "conclusion", "next_action")), "stage review requires reviewer, both handoff roles, conclusion and next action")
    require(op["handoff_from"] != op["handoff_to"], "handoff must distinguish producing and receiving roles")
    fingerprints = pin(root, op.get("artifact_refs", []))
    idx = STAGES.index(stage)
    previous = stage_state(root, state, req, STAGES[idx - 1]) if idx else None
    if result == "passed":
        if previous:
            require(previous["result"] == "passed", f"prerequisite {STAGES[idx-1]} has not passed")
        gaps = check_gaps(root, state, req, stage)
        require(not gaps, "; ".join(gaps))
        checks = op.get("checks", {})
        require(isinstance(checks, dict), "stage checks must be an object")
        keys = list(CHECKS[stage]) + config(state).get("additional_checks", {}).get(stage, [])
        for key in keys:
            check = checks.get(key, {})
            require(isinstance(check, dict), f"stage check {key} must be an object")
            require(check.get("result") in {"passed", "not_applicable"} and text(check.get("reason")), f"stage check {key} is missing or unresolved")
            fingerprints.update(pin(root, check.get("artifact_refs", [])))
        if stage == "engineering":
            _, refs = engineering_manifest(root, state, req)
            fingerprints.update(pin(root, refs))
        if stage in {"implementation", "verification", "acceptance"}:
            for row in coverage_rows(root, state, req):
                fingerprints.update(row.get("code_fingerprints", {}))
                if stage in {"verification", "acceptance"}:
                    fingerprints.update(row.get("test_fingerprints", {}))
        if stage == "acceptance":
            src = source(state, op["source_ref"])
            require(src.get("kind") in {"user_message", "user_confirmation", "acceptor_decision"}
                    and src.get("acceptance_scope", {}).get(req["id"]) == req.get("version", 1),
                    "product acceptance needs explicit version-scoped acceptance authority; requirement confirmation is insufficient")
            require(text(op.get("acceptance_statement")), "explicit product acceptance statement required")
            if src["kind"] == "acceptor_decision":
                acceptor = state["project"].get("acceptor")
                require(text(acceptor) and src.get("acceptor") == acceptor, "acceptor_decision must identify the project's designated acceptor")
        # Engineering maturity moves through this operation, never by unlocking scope.
        if stage == "engineering":
            if req.get("readiness") not in {"confirmed_for_extension", "ready_for_acceptance"}:
                req["readiness"] = "ready_for_build"
        elif stage == "verification" and scope_entry(state, req)["mode"] == "full":
            req["readiness"] = "ready_for_acceptance"
    old = delivery.setdefault("receipts", {}).get(stage)
    if old:
        delivery.setdefault("history", []).append({"type": "receipt", "value": old})
    receipt = {k: copy.deepcopy(v) for k, v in op.items() if k not in {"op", "expected_version"}}
    receipt.update(id=f"{req['id']}:{stage}:r{state['revision']+1}:{len(delivery.get('history', []))+1}",
                   requirement_version=req.get("version", 1), spec_hash=spec_hash(req),
                   scope_hash=digest({"entry": scope_entry(state, req), "additional_checks": config(state).get("additional_checks", {})}),
                   artifact_fingerprints=fingerprints, reviewed_at=stamp,
                   prerequisite_receipt_id=previous.get("receipt_id") if previous else None)
    delivery["receipts"][stage] = receipt
    req["updated_at"] = stamp
    return {"op": op["op"], "id": req["id"], "stage": stage, "result": result, "receipt_id": receipt["id"]}


def report(root: Path, state: dict, selected: list[str] | None = None) -> dict:
    token = _HASH_CACHE.set({})
    try:
        return _report(root, state, selected)
    finally:
        _HASH_CACHE.reset(token)


def _report(root: Path, state: dict, selected: list[str] | None = None) -> dict:
    cfg = config(state)
    selected_set = set(selected or [])
    active = [r for r in state["requirements"] if r.get("status") not in {"rejected", "superseded", "candidate"}]
    require(not selected_set - {r["id"] for r in state["requirements"]}, "unknown requirement in workflow selection")
    rows, findings = [], []
    for req in active:
        if selected_set and req["id"] not in selected_set:
            continue
        if cfg and not selected_set and req["id"] not in {x["id"] for x in cfg["scope"]}:
            continue
        try:
            coverage = coverage_rows(root, state, req)
            stages = {stage: stage_state(root, state, req, stage) for stage in STAGES}
        except WorkflowError as exc:
            coverage = []
            stages = {stage: {"result": "not_assessed", "gaps": [str(exc)]} for stage in STAGES}
        next_stage = next((k for k in STAGES if stages[k]["result"] != "passed"), None)
        counts = {key: dict(Counter(r.get(key, "not_assessed") for r in coverage)) for key in ("implementation", "frontend", "verification")}
        receipt = req.get("delivery", {}).get("receipts", {}).get("acceptance", {})
        for c in coverage:
            c["acceptance"] = stages["acceptance"]["result"] if receipt else "pending"
        rows.append({"id": req["id"], "version": req.get("version", 1), "human_verification": req.get("human_verification"),
                     "stages": stages, "coverage": coverage, "counts": counts, "next_stage": next_stage,
                     "delivery_mode": next((e["mode"] for e in cfg.get("scope", []) if e["id"] == req["id"]), "not_configured"),
                     "open_gaps": [g for g in req.get("delivery", {}).get("gaps", {}).values() if g["status"] == "open" or not fresh(root, g.get("artifact_fingerprints"))]})
        for stage in STAGES:
            if stages[stage]["result"] != "passed":
                findings.append({"id": f"{req['id']}:{stage}", "record": req["id"], "stage": stage,
                                 "result": stages[stage]["result"], "reason": "; ".join(stages[stage]["gaps"][:6]) or "review not passed",
                                 "action": f"完成 {stage} 的具体缺口并记录阶段验收/交接回执", "owner": "AI（阶段执行与补齐）；产品验收按指定验收者", "actionable": stage == next_stage})
    return {"configured": bool(cfg), "revision": state["revision"], "profile": cfg.get("profile"), "requirements": rows,
            "excluded_requirements": [r["id"] for r in active if cfg and r["id"] not in {e["id"] for e in cfg["scope"]}],
            "totals": {"requirements": len(rows), "acceptance_scenarios": sum(len(r["coverage"]) for r in rows),
                       "implementation": dict(Counter(c["implementation"] for r in rows for c in r["coverage"])),
                       "verification": dict(Counter(c["verification"] for r in rows for c in r["coverage"])),
                       "acceptance": dict(Counter(c["acceptance"] for r in rows for c in r["coverage"])),
                       "unassessed_stage_reviews": sum(s["result"] == "not_assessed" for r in rows for s in r["stages"].values()),
                       "open_recorded_gaps": sum(len(r["open_gaps"]) for r in rows)},
            "stage_results": {k: ("passed" if rows and all(r["stages"][k]["result"] == "passed" for r in rows) else "not_passed") for k in STAGES},
            "findings": findings, "notice": "未登记是未评估，不表示未实现；结构通过不替代内容评审、真实验证或人工产品验收。"}


def markdown(data: dict) -> str:
    lines = ["# 阶段验收与完整交付", "", "> 从 PM state.json、文档和证据指纹派生；请勿手工编辑。", "",
             f"revision：{data['revision']}；阶段配置：{'已启用' if data['configured'] else '尚未配置'}", "",
             "```json", json.dumps(data["totals"], ensure_ascii=False, indent=2), "```", "",
             "未评估表示尚未核对已有代码/证据，不代表这些功能未实现。已知缺口和未知覆盖分别统计。", "",
             "| REQ / 版本 | 规格固化 | 工程交接 | 完整实现 | 技术验证 | 产品验收 | 下一阶段 |",
             "|---|---|---|---|---|---|---|"]
    for row in data["requirements"]:
        lines.append(f"| {row['id']} v{row['version']} | " + " | ".join(row["stages"][s]["result"] for s in STAGES) + f" | {row['next_stage'] or '已交付'} |")
    for row in data["requirements"]:
        lines.extend(["", f"## {row['id']} v{row['version']}（{row['delivery_mode']}）", "", "| AC | 实现 | 前端 | 验证 | 产品验收 |", "|---|---|---|---|---|"])
        lines.extend(f"| {c['ac_id']} | {c['implementation']} | {c['frontend']} | {c['verification']} | {c['acceptance']} |" for c in row["coverage"])
        for g in row["open_gaps"]:
            lines.append(f"- {g['id']}（{g['stage']}/{g['kind']}）：{g['reason']}；{g['owner']}；{g['action']}；位置 {g['target']}；关闭条件 {g['done_when']}")
        for stage, detail in row["stages"].items():
            if detail["result"] != "passed":
                lines.append(f"- {stage} / {detail['result']}：" + "; ".join(detail["gaps"]).replace("\n", " "))
    return "\n".join(lines) + "\n"
