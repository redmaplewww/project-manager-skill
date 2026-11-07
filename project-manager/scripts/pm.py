#!/usr/bin/env python3
"""Deterministic state, archive, and index helper for the project-manager skill."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
from html import escape as html_escape
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_workflow_spec = importlib.util.spec_from_file_location("project_delivery_workflow", Path(__file__).with_name("delivery_workflow.py"))
assert _workflow_spec and _workflow_spec.loader
workflow = importlib.util.module_from_spec(_workflow_spec)
_workflow_spec.loader.exec_module(workflow)


SCHEMA_VERSION = 1
DOCS_REL = Path(".project-to-act/docs/project-manager")
LOCAL_REL = Path(".project-to-act/local/project-manager")
STATE_NAME = "state.json"
PROJECT_OVERVIEW_NAME = "PROJECT_OVERVIEW.html"
COLLECTIONS = {
    "sources": "SRC-",
    "intake_items": "ITM-",
    "scenarios": "SCN-",
    "requirements": "REQ-",
    "work_packages": "WP-",
    "milestones": "MS-",
    "decisions": "DEC-",
    "risks": "RSK-",
    "reviews": "REV-",
    "evidence": "EVD-",
}
PROJECT_STAGES = {
    "uninitiated", "discovery", "development", "validation", "closed", "paused", "cancelled"
}
MODES = {"daily", "initiation", "midterm", "closure", "adjustment"}
REQUIREMENT_STATUSES = {
    "candidate", "clarification_needed", "pending_confirmation", "ai_confirmed_unverified", "confirmed", "rejected", "superseded"
}
DECISION_QUESTION_STATUSES = {"open", "accepted", "overridden", "deferred"}
DECISION_QUESTION_FIELDS = {
    "id", "question", "current_conclusion", "recommendation", "alternatives",
    "basis", "behavior_impact", "implementation_impact", "acceptance_impact",
    "status", "answer", "requirement_version", "source_ref", "asked_at",
}
CONFIRMATION_PACKAGE_FIELDS = {
    "requirement_id", "requirement_version", "protocol", "presented_in_conversation",
    "bulk_accept_phrase", "questions",
}
CONFIRMATION_LOCK_FIELDS = {
    "status", "locked_version", "locked_source_ref", "locked_at", "reason", "reopen_policy",
}
CONFIRMATION_PROTECTED_FIELDS = {
    "title", "scenario_id", "user_value", "trigger", "behavior", "outcome", "boundaries",
    "acceptance_criteria", "source_refs", "status", "readiness", "priority",
    "decision_questions", "confirmation_package", "open_questions", "product_form",
    "experience_review", "technical_design", "implementation_contract", "acceptance_scenarios",
    "feature_shaping_rounds", "gate", "failure_policy", "search_policy", "deletion_policy",
    "switch_policy", "startup_policy", "transfer_policy", "creation_template", "module_id",
    "module_title", "related_requirement_ids", "coupling_level", "interaction_note",
    "interaction_contracts", "interface_spec", "extension_module_id", "extension_module_title",
    "extension_contract", "human_verification",
}
READINESS = {"not_ready", "ready_for_design", "ready_for_build", "ready_for_acceptance", "confirmed_for_extension"}
BUILD_READINESS = {"ready_for_build", "ready_for_acceptance"}
BUILD_GATE_SECTIONS = {
    "product_form",
    "experience_review",
    "technical_design",
    "implementation_contract",
    "acceptance_scenarios",
    "feature_shaping_rounds",
    "gate",
}
BUILD_GATE_FIELDS = {
    "product_form": {"user_and_trigger", "main_flow", "non_goals", "diagram_refs"},
    "experience_review": {"normal_cases", "edge_cases", "accessibility_and_responsive", "interaction_contract"},
    "technical_design": {"stack", "architecture_ref", "data_contract", "security_and_operations"},
    "implementation_contract": {
        "discovery_method",
        "implementation_surfaces",
        "functional_decomposition",
        "state_model",
        "verification_matrix",
        "decisions",
        "unresolved_items",
        "task_breakdown",
    },
    "feature_shaping_rounds": {"rounds", "completion_policy"},
    "gate": {"status", "review_lenses", "confirmed_source", "confirmation_protocol", "confirmed_scope_statement"},
}
BUILD_GATE_ONLY_FIELDS = {"readiness", "feature_shaping_rounds", "gate", "gate_audit"}
WORK_STATUSES = {"not_started", "in_progress", "blocked", "done", "cancelled"}
MILESTONE_STATUSES = {"planned", "in_progress", "blocked", "reached", "missed", "cancelled"}
RISK_STATUSES = {"open", "mitigating", "accepted", "closed", "occurred"}
REVIEW_KINDS = {"blocker", "risk", "suggestion", "hypothesis"}
PRIORITIES = {"must", "should", "could", "wont"}
INTAKE_KINDS = {
    "problem", "goal", "actor", "scenario", "behavior", "business_rule", "quality",
    "constraint", "solution_preference", "assumption", "candidate_idea"
}
INTAKE_DISPOSITIONS = {
    "unclassified", "requirement", "constraint", "candidate", "rejected", "clarification", "duplicate"
}
CHANGE_CLASSES = {"clarification", "correction", "addition", "reduction", "replacement", "wording_only"}
DEADLINE_TYPES = {"hard", "target", "forecast", "exploration", "review"}
ALLOWED_TRANSITIONS = {
    "requirements": {
        "candidate": {"clarification_needed", "pending_confirmation", "rejected"},
        "clarification_needed": {"candidate", "pending_confirmation", "rejected"},
        "pending_confirmation": {"clarification_needed", "ai_confirmed_unverified", "confirmed", "rejected"},
        "ai_confirmed_unverified": {"pending_confirmation", "confirmed", "superseded", "rejected"},
        "confirmed": {"pending_confirmation", "superseded", "rejected"},
        "rejected": {"candidate"},
        "superseded": set(),
    },
    "work_packages": {
        "not_started": {"in_progress", "blocked", "cancelled"},
        "in_progress": {"blocked", "done", "cancelled"},
        "blocked": {"in_progress", "cancelled"},
        "done": {"in_progress"},
        "cancelled": {"not_started"},
    },
    "milestones": {
        "planned": {"in_progress", "blocked", "reached", "missed", "cancelled"},
        "in_progress": {"blocked", "reached", "missed", "cancelled"},
        "blocked": {"in_progress", "missed", "cancelled"},
        "missed": {"planned", "cancelled"},
        "reached": set(),
        "cancelled": {"planned"},
    },
    "risks": {
        "open": {"mitigating", "accepted", "closed", "occurred"},
        "mitigating": {"open", "accepted", "closed", "occurred"},
        "accepted": {"open", "closed", "occurred"},
        "occurred": {"mitigating", "closed"},
        "closed": {"open"},
    },
}


class PMError(RuntimeError):
    pass


@contextmanager
def project_lock(root: Path, stale_after_seconds: int = 1800) -> Iterable[None]:
    """Use an atomic lock file; preserve stale locks for diagnosis before reacquiring."""
    lock = root / LOCAL_REL / "write.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        age = datetime.now(timezone.utc).timestamp() - lock.stat().st_mtime
        if age <= stale_after_seconds:
            raise PMError(f"project-manager is locked by another writer: {lock}")
        stale = lock.with_name(f"write.lock.stale-{int(lock.stat().st_mtime)}")
        os.replace(lock, stale)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise PMError(f"project-manager is locked by another writer: {lock}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "created_at": now_iso()}))
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        lock.unlink(missing_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PMError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PMError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PMError(f"expected JSON object in {path}")
    return data


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def state_path(root: Path) -> Path:
    return root / DOCS_REL / STATE_NAME


def load_state(root: Path) -> dict[str, Any]:
    state = read_json(state_path(root))
    if state.get("schema_version") != SCHEMA_VERSION:
        raise PMError(f"unsupported schema_version: {state.get('schema_version')}")
    return state


def default_state(project_id: str, name: str, tz_name: str) -> dict[str, Any]:
    stamp = now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "project": {
            "id": project_id,
            "name": name,
            "stage": "uninitiated",
            "active_mode": "daily",
            "timezone": tz_name,
            "goal": "",
            "success_outcomes": [],
            "in_scope": [],
            "out_of_scope": [],
            "owner": "unknown",
            "acceptor": "unknown",
            "closure_status": "open",
            "created_at": stamp,
            "updated_at": stamp,
        },
        **{name: [] for name in COLLECTIONS},
        "change_events": [],
    }


def ensure_pta(root: Path) -> None:
    config = root / ".project-to-act" / "PROJECT_CONFIG.json"
    if not config.exists():
        raise PMError(
            "project-to-act is not configured; initialize or select the canonical ledger before project-manager init"
        )


def ensure_gitignore(root: Path) -> bool:
    path = root / ".gitignore"
    marker = ".project-to-act/local/"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = [line.strip() for line in old.splitlines()]
    if marker in lines:
        return False
    prefix = old
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += "\n"
    atomic_text(path, prefix + marker + "\n")
    return True


def collection_map(state: dict[str, Any], collection: str) -> dict[str, dict[str, Any]]:
    if collection not in COLLECTIONS:
        raise PMError(f"unsupported collection: {collection}")
    records = state.get(collection)
    if not isinstance(records, list):
        raise PMError(f"collection is not a list: {collection}")
    result: dict[str, dict[str, Any]] = {}
    for item in records:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise PMError(f"invalid record in {collection}")
        if item["id"] in result:
            raise PMError(f"duplicate id {item['id']} in {collection}")
        result[item["id"]] = item
    return result


def get_record(state: dict[str, Any], collection: str, record_id: str) -> dict[str, Any]:
    record = collection_map(state, collection).get(record_id)
    if record is None:
        raise PMError(f"unknown {collection} id: {record_id}")
    return record


def additive_implementation_contract_update(existing: Any, incoming: Any) -> bool:
    """Enrich existing surfaces without changing locked product or contract values."""
    if not isinstance(existing, dict) or not isinstance(incoming, dict) or set(existing) != set(incoming):
        return False
    allowed_additions = {"execution_steps", "interfaces_or_data", "failure_and_rollback", "observability", "test_mapping"}
    for key, old_value in existing.items():
        new_value = incoming[key]
        if key == "implementation_surfaces" and isinstance(old_value, list) and isinstance(new_value, list):
            if len(old_value) != len(new_value):
                return False
            for old, new in zip(old_value, new_value):
                if not isinstance(old, dict) or not isinstance(new, dict):
                    return False
                if any(k not in new or new[k] != value for k, value in old.items()):
                    return False
                added = set(new) - set(old)
                if added - allowed_additions or any(new[k] in (None, "", [], {}) for k in added):
                    return False
        elif new_value != old_value:
            return False
    return True


def validate_record(collection: str, record: dict[str, Any]) -> None:
    record_id = record.get("id")
    if not isinstance(record_id, str) or not record_id.startswith(COLLECTIONS[collection]):
        raise PMError(f"{collection} id must start with {COLLECTIONS[collection]}")
    if not str(record.get("title", "")).strip() and collection not in {"sources", "evidence"}:
        raise PMError(f"{record_id} requires title")
    if collection == "requirements":
        if record.get("status", "candidate") not in REQUIREMENT_STATUSES:
            raise PMError(f"invalid requirement status for {record_id}")
        if record.get("readiness", "not_ready") not in READINESS:
            raise PMError(f"invalid readiness for {record_id}")
        if record.get("priority", "could") not in PRIORITIES:
            raise PMError(f"invalid priority for {record_id}")
        if not record.get("source_refs"):
            raise PMError(f"{record_id} requires source_refs")
        confirmation_lock = record.get("confirmation_lock")
        if confirmation_lock is not None:
            if not isinstance(confirmation_lock, dict):
                raise PMError(f"{record_id}.confirmation_lock must be an object")
            missing_lock_fields = sorted(CONFIRMATION_LOCK_FIELDS - set(confirmation_lock))
            if missing_lock_fields:
                raise PMError(
                    f"{record_id}.confirmation_lock is missing: {', '.join(missing_lock_fields)}"
                )
            if confirmation_lock.get("status") != "locked":
                raise PMError(f"{record_id}.confirmation_lock.status must be locked")
            if confirmation_lock.get("locked_version") != record.get("version"):
                raise PMError(f"{record_id}.confirmation_lock.locked_version must match the requirement version")
            if not str(confirmation_lock.get("locked_source_ref", "")).startswith("SRC-"):
                raise PMError(f"{record_id}.confirmation_lock.locked_source_ref must reference a source")
            if confirmation_lock.get("reopen_policy") != "only_explicit_scope_change_request":
                raise PMError(f"{record_id}.confirmation_lock.reopen_policy is invalid")

        confirmation_package = record.get("confirmation_package")
        if confirmation_package is not None:
            if not isinstance(confirmation_package, dict):
                raise PMError(f"{record_id}.confirmation_package must be an object")
            missing_package_fields = sorted(CONFIRMATION_PACKAGE_FIELDS - set(confirmation_package))
            if missing_package_fields:
                raise PMError(
                    f"{record_id}.confirmation_package is missing: {', '.join(missing_package_fields)}"
                )
            if confirmation_package.get("requirement_id") != record_id:
                raise PMError(f"{record_id}.confirmation_package.requirement_id must match the requirement")
            if confirmation_package.get("requirement_version") != record.get("version"):
                raise PMError(f"{record_id}.confirmation_package.requirement_version must match the requirement version")
            if confirmation_package.get("protocol") != "per_question_with_recommendations":
                raise PMError(f"{record_id}.confirmation_package.protocol must be per_question_with_recommendations")
            if confirmation_package.get("presented_in_conversation") is not True:
                raise PMError(f"{record_id}.confirmation_package must be presented in the current conversation")
            if confirmation_package.get("bulk_accept_phrase") != "按本 REQ 推荐全部采纳":
                raise PMError(f"{record_id}.confirmation_package.bulk_accept_phrase is invalid")
            package_questions = confirmation_package.get("questions")
            if not isinstance(package_questions, list) or not package_questions:
                raise PMError(f"{record_id}.confirmation_package.questions must be a non-empty list")
            package_ids = set()
            for index, question in enumerate(package_questions):
                if not isinstance(question, dict):
                    raise PMError(f"{record_id}.confirmation_package.questions[{index}] must be an object")
                missing = sorted(DECISION_QUESTION_FIELDS - set(question))
                if missing:
                    raise PMError(
                        f"{record_id}.confirmation_package.questions[{index}] is missing: {', '.join(missing)}"
                    )
                qid = question.get("id")
                if not isinstance(qid, str) or not qid.startswith("DQ-") or qid in package_ids:
                    raise PMError(f"{record_id}.confirmation_package.questions[{index}].id is invalid or duplicated")
                package_ids.add(qid)
                if not str(question.get("question", "")).strip() or not str(question.get("current_conclusion", "")).strip():
                    raise PMError(f"{record_id}.confirmation_package.questions[{index}] requires a question and current_conclusion")
                if not str(question.get("recommendation", "")).strip() or not isinstance(question.get("alternatives"), list) or not question.get("alternatives"):
                    raise PMError(f"{record_id}.confirmation_package.questions[{index}] requires recommendation and alternatives")
                if question.get("status") not in DECISION_QUESTION_STATUSES:
                    raise PMError(f"invalid confirmation question status for {record_id}: {qid}")

        decision_questions = record.get("decision_questions")
        if decision_questions is not None:
            if not isinstance(decision_questions, list):
                raise PMError(f"{record_id}.decision_questions must be a list")
            seen_question_ids: set[str] = set()
            for index, question in enumerate(decision_questions):
                if not isinstance(question, dict):
                    raise PMError(f"{record_id}.decision_questions[{index}] must be an object")
                missing_question_fields = sorted(DECISION_QUESTION_FIELDS - set(question))
                if missing_question_fields:
                    raise PMError(
                        f"{record_id}.decision_questions[{index}] is missing: {', '.join(missing_question_fields)}"
                    )
                question_id = question.get("id")
                if not isinstance(question_id, str) or not question_id.startswith("DQ-"):
                    raise PMError(f"{record_id}.decision_questions[{index}].id must start with DQ-")
                if question_id in seen_question_ids:
                    raise PMError(f"duplicate decision question id {question_id} in {record_id}")
                seen_question_ids.add(question_id)
                if question.get("status") not in DECISION_QUESTION_STATUSES:
                    raise PMError(f"invalid decision question status for {record_id}: {question_id}")
                if not str(question.get("question", "")).strip():
                    raise PMError(f"{record_id}.decision_questions[{index}].question must not be empty")
                if not str(question.get("recommendation", "")).strip():
                    raise PMError(f"{record_id}.decision_questions[{index}].recommendation must not be empty")
                if not isinstance(question.get("alternatives"), list):
                    raise PMError(f"{record_id}.decision_questions[{index}].alternatives must be a list")
                if question.get("status") in {"accepted", "overridden"}:
                    if question.get("answer") in (None, "") or not question.get("source_ref"):
                        raise PMError(
                            f"answered decision question {record_id}:{question_id} requires answer and source_ref"
                        )
        if record.get("status") in {"confirmed", "ai_confirmed_unverified"} and not record.get("acceptance_criteria"):
            raise PMError(f"{record.get('status')} requirement {record_id} requires acceptance_criteria")
        if record.get("status") == "ai_confirmed_unverified":
            if record.get("human_verification") != "unverified":
                raise PMError(f"{record_id} AI-confirmed requirement must set human_verification=unverified")
            mapping = record.get("code_mapping")
            if not isinstance(mapping, list) or not mapping:
                raise PMError(f"{record_id} AI-confirmed requirement requires code_mapping")
        if record.get("readiness") in BUILD_READINESS:
            missing = sorted(BUILD_GATE_SECTIONS - set(record))
            if missing:
                raise PMError(
                    f"{record_id} at {record['readiness']} requires feature-shaping sections: {', '.join(missing)}"
                )
            if record.get("status") not in {"confirmed", "ai_confirmed_unverified"}:
                raise PMError(f"{record_id} must be confirmed or AI-confirmed before {record['readiness']}")
            for section, fields in BUILD_GATE_FIELDS.items():
                value = record.get(section)
                if not isinstance(value, dict):
                    raise PMError(f"{record_id} requires {section} review")
                missing_fields = sorted(fields - set(value))
                if missing_fields:
                    raise PMError(
                        f"{record_id}.{section} is missing: {', '.join(missing_fields)}"
                    )
            implementation = record["implementation_contract"]
            surfaces = implementation["implementation_surfaces"]
            if not isinstance(surfaces, list) or not surfaces:
                raise PMError(f"{record_id}.implementation_contract.implementation_surfaces must be non-empty")
            surface_fields = {
                "name",
                "evidence",
                "current_structure",
                "decision",
                "behavior_impact",
                "verification",
                "task_mapping",
                "execution_steps",
                "interfaces_or_data",
                "failure_and_rollback",
                "observability",
                "test_mapping",
            }
            for index, surface in enumerate(surfaces):
                if not isinstance(surface, dict):
                    raise PMError(f"{record_id}.implementation_surfaces[{index}] must be an object")
                missing_surface_fields = sorted(surface_fields - set(surface))
                if missing_surface_fields:
                    raise PMError(
                        f"{record_id}.implementation_surfaces[{index}] is missing: {', '.join(missing_surface_fields)}"
                    )
            decomposition = implementation["functional_decomposition"]
            if not isinstance(decomposition, list) or not decomposition:
                raise PMError(f"{record_id}.implementation_contract.functional_decomposition must be non-empty")
            unit_fields = {"id", "title", "actor", "trigger", "preconditions", "inputs", "steps", "outputs", "state_transitions", "errors", "acceptance", "implementation_mapping"}
            for index, unit in enumerate(decomposition):
                if not isinstance(unit, dict) or unit_fields - set(unit):
                    missing = sorted(unit_fields - set(unit)) if isinstance(unit, dict) else sorted(unit_fields)
                    raise PMError(f"{record_id}.functional_decomposition[{index}] is missing: {', '.join(missing)}")
                if any(unit.get(field) in (None, "", [], {}) for field in unit_fields):
                    raise PMError(f"{record_id}.functional_decomposition[{index}] contains empty detail")
            state_model = implementation["state_model"]
            if not isinstance(state_model, list) or not state_model:
                raise PMError(f"{record_id}.implementation_contract.state_model must be non-empty")
            state_fields = {"state", "entry", "visible_behavior", "allowed_actions", "exit", "retention"}
            for index, item in enumerate(state_model):
                if not isinstance(item, dict) or state_fields - set(item):
                    missing = sorted(state_fields - set(item)) if isinstance(item, dict) else sorted(state_fields)
                    raise PMError(f"{record_id}.state_model[{index}] is missing: {', '.join(missing)}")
            verification_matrix = implementation["verification_matrix"]
            if not isinstance(verification_matrix, list) or not verification_matrix:
                raise PMError(f"{record_id}.implementation_contract.verification_matrix must be non-empty")
            verification_fields = {"id", "scenario", "expected", "method", "evidence_or_test"}
            for index, item in enumerate(verification_matrix):
                if not isinstance(item, dict) or verification_fields - set(item):
                    missing = sorted(verification_fields - set(item)) if isinstance(item, dict) else sorted(verification_fields)
                    raise PMError(f"{record_id}.verification_matrix[{index}] is missing: {', '.join(missing)}")
            rounds = record["feature_shaping_rounds"].get("rounds")
            if not isinstance(rounds, list) or len(rounds) != 5:
                raise PMError(f"{record_id}.feature_shaping_rounds.rounds must contain all five rounds")
            round_fields = {"round", "status", "questions", "conclusions", "evidence", "unresolved_items", "confirmation_source"}
            if {item.get("round") for item in rounds if isinstance(item, dict)} != {1, 2, 3, 4, 5}:
                raise PMError(f"{record_id}.feature_shaping_rounds must include rounds 1 through 5")
            for index, item in enumerate(rounds):
                if not isinstance(item, dict) or round_fields - set(item):
                    missing = sorted(round_fields - set(item)) if isinstance(item, dict) else sorted(round_fields)
                    raise PMError(f"{record_id}.feature_shaping_rounds.rounds[{index}] is missing: {', '.join(missing)}")
                if item.get("status") not in {"complete", "confirmed"} or item.get("unresolved_items") not in (None, []):
                    raise PMError(f"{record_id}.feature_shaping_rounds.rounds[{index}] is incomplete")
            if not isinstance(implementation["decisions"], list):
                raise PMError(f"{record_id}.implementation_contract.decisions must be a list")
            if implementation["unresolved_items"] not in (None, []):
                raise PMError(f"{record_id} has unresolved implementation items and cannot enter {record['readiness']}")
            if not isinstance(record.get("acceptance_scenarios"), list) or not record.get("acceptance_scenarios"):
                raise PMError(f"{record_id} requires acceptance_scenarios")
            acceptance_fields = {"id", "given", "when", "then", "verification"}
            for index, scenario in enumerate(record["acceptance_scenarios"]):
                if not isinstance(scenario, dict) or acceptance_fields - set(scenario):
                    missing = sorted(acceptance_fields - set(scenario)) if isinstance(scenario, dict) else sorted(acceptance_fields)
                    raise PMError(f"{record_id}.acceptance_scenarios[{index}] is missing: {', '.join(missing)}")
            if record.get("open_questions") not in (None, []):
                raise PMError(f"{record_id} has open_questions and cannot enter {record['readiness']}")
            if any(
                question.get("status") in {"open", "deferred"}
                for question in (record.get("decision_questions") or [])
            ):
                raise PMError(f"{record_id} has unresolved decision_questions and cannot enter {record['readiness']}")
            gate = record.get("gate")
            if gate.get("status") != "approved":
                raise PMError(f"{record_id} requires an approved feature-shaping gate")
            if not gate.get("confirmed_source"):
                raise PMError(f"{record_id} requires gate.confirmed_source")
            protocols = {"explicit_scope_confirmation"}
            if record.get("status") == "ai_confirmed_unverified":
                protocols.add("ai_review_unverified")
            if gate.get("confirmation_protocol") not in protocols:
                raise PMError(f"{record_id} requires explicit scope confirmation protocol")
            if not str(gate.get("confirmed_scope_statement", "")).strip():
                raise PMError(f"{record_id} requires a confirmed scope statement")
            review_lenses = gate.get("review_lenses")
            if not isinstance(review_lenses, list) or not review_lenses:
                raise PMError(f"{record_id} gate.review_lenses must be a non-empty list")
            lens_fields = {"name", "question", "conclusion", "evidence", "unresolved"}
            for index, lens in enumerate(review_lenses):
                if not isinstance(lens, dict):
                    raise PMError(f"{record_id}.gate.review_lenses[{index}] must be an object")
                missing_lens_fields = sorted(lens_fields - set(lens))
                if missing_lens_fields:
                    raise PMError(
                        f"{record_id}.gate.review_lenses[{index}] is missing: {', '.join(missing_lens_fields)}"
                    )
                if lens.get("unresolved") not in (None, []):
                    raise PMError(f"{record_id}.gate.review_lenses[{index}] has unresolved items")
    elif collection == "intake_items":
        if record.get("kind") not in INTAKE_KINDS:
            raise PMError(f"invalid intake kind for {record_id}")
        if record.get("disposition", "unclassified") not in INTAKE_DISPOSITIONS:
            raise PMError(f"invalid intake disposition for {record_id}")
        if not record.get("source_refs"):
            raise PMError(f"{record_id} requires source_refs")
        if record.get("disposition") == "requirement" and not record.get("target_ids"):
            raise PMError(f"{record_id} classified as requirement requires target_ids")
    elif collection == "work_packages" and record.get("status", "not_started") not in WORK_STATUSES:
        raise PMError(f"invalid work package status for {record_id}")
    elif collection == "milestones" and record.get("status", "planned") not in MILESTONE_STATUSES:
        raise PMError(f"invalid milestone status for {record_id}")
    elif collection == "risks" and record.get("status", "open") not in RISK_STATUSES:
        raise PMError(f"invalid risk status for {record_id}")
    elif collection == "reviews" and record.get("kind", "suggestion") not in REVIEW_KINDS:
        raise PMError(f"invalid review kind for {record_id}")
    elif collection == "milestones" and record.get("deadline_type") not in DEADLINE_TYPES:
        raise PMError(f"invalid deadline_type for {record_id}")


def canonical_record(record: dict[str, Any]) -> str:
    ignored = {"created_at", "updated_at", "version"}
    return json.dumps({k: v for k, v in record.items() if k not in ignored}, ensure_ascii=False, sort_keys=True)


def apply_operation(state: dict[str, Any], op: dict[str, Any], stamp: str, root: Path | None = None) -> dict[str, Any]:
    if op.get("op") in workflow.OPERATIONS:
        if root is None:
            raise PMError("delivery operations require project root")
        try:
            if op.get("op") == "review_stage" and op.get("result") == "passed":
                req = get_record(state, "requirements", op.get("requirement_id"))
                if op.get("stage") in {"specification", "engineering"}:
                    gaps = shaping_gaps(req)
                    if gaps:
                        raise PMError("specification material incomplete: " + "; ".join(gaps))
            result = workflow.apply(root, state, op, stamp)
            if op.get("op") == "review_stage" and op.get("result") == "passed":
                req = get_record(state, "requirements", op["requirement_id"])
                if op.get("stage") == "engineering" and req.get("gate", {}).get("status") == "confirmed_locked":
                    req["gate"]["status"] = "approved"
                validate_record("requirements", req)
            return result
        except (workflow.WorkflowError, OSError, KeyError, TypeError, ValueError) as exc:
            raise PMError(str(exc)) from exc
    if op.get("op") == "set_project" and {"delivery_workflow", "delivery_workflow_history"} & set(op.get("patch", {}) or op.get("fields", {})):
        raise PMError("use configure_delivery to change stage configuration")
    if op.get("collection") == "requirements" and (
        "delivery" in op.get("patch", {}) or "delivery" in op.get("record", {})
    ):
        raise PMError("use delivery operations for receipts and coverage")
    kind = op.get("op")
    if kind == "set_project":
        patch = op.get("patch")
        if not isinstance(patch, dict):
            raise PMError("set_project requires patch object")
        if workflow.config(state) and (patch.get("stage") == "closed" or patch.get("closure_status") == "closed"):
            if root is None or workflow.report(root, state)["stage_results"]["acceptance"] != "passed":
                raise PMError("project closure requires current product acceptance for the configured scope")
        allowed = {
            "name", "stage", "active_mode", "timezone", "goal", "success_outcomes", "in_scope",
            "out_of_scope", "owner", "acceptor", "closure_status"
        }
        unknown = set(patch) - allowed
        if unknown:
            raise PMError(f"unsupported project fields: {sorted(unknown)}")
        if "stage" in patch and patch["stage"] not in PROJECT_STAGES:
            raise PMError(f"invalid project stage: {patch['stage']}")
        if "active_mode" in patch and patch["active_mode"] not in MODES:
            raise PMError(f"invalid active mode: {patch['active_mode']}")
        state["project"].update(copy.deepcopy(patch))
        state["project"]["updated_at"] = stamp
        return {"op": kind, "fields": sorted(patch)}

    if kind == "unlock_confirmation":
        collection = op.get("collection")
        if collection != "requirements":
            raise PMError("unlock_confirmation only supports requirements")
        record_id = op.get("id")
        record = get_record(state, collection, record_id)
        if record.get("confirmation_lock", {}).get("status") != "locked":
            raise PMError(f"{record_id} confirmation is not locked")
        source_ref = op.get("source_ref")
        reason = op.get("reason")
        if not source_ref or not reason:
            raise PMError("unlock_confirmation requires source_ref and reason")
        get_record(state, "sources", source_ref)
        old = record.get("status")
        # A package was presented and approved for the old version. Keep that
        # evidence without claiming it was presented for the new scope/version.
        historical = {key: copy.deepcopy(record.pop(key)) for key in (
            "confirmation_lock", "confirmation_package", "gate", "gate_audit", "feature_shaping_rounds",
        ) if key in record}
        record.setdefault("confirmation_history", []).append({
            "requirement_version": record.get("version", 1), "status": old,
            "readiness": record.get("readiness", "not_ready"),
            "artifacts": historical, "scope_change_source_ref": source_ref,
            "reason": reason, "timestamp": stamp,
        })
        record["status"] = "pending_confirmation"
        record["readiness"] = "ready_for_design"
        record["version"] = record.get("version", 1) + 1
        record["updated_at"] = stamp
        record.setdefault("status_history", []).append({
            "from": old, "to": "pending_confirmation", "reason": reason,
            "source_ref": source_ref, "timestamp": stamp,
        })
        validate_record(collection, record)
        return {"op": kind, "collection": collection, "id": record_id, "version": record["version"]}

    collection = op.get("collection")
    if collection not in COLLECTIONS:
        raise PMError(f"{kind} requires a supported collection")

    if kind == "add":
        record = copy.deepcopy(op.get("record"))
        if not isinstance(record, dict):
            raise PMError("add requires record object")
        validate_record(collection, record)
        existing = collection_map(state, collection)
        if record["id"] in existing:
            if canonical_record(existing[record["id"]]) == canonical_record(record):
                return {"op": "noop", "collection": collection, "id": record["id"], "reason": "same_record"}
            raise PMError(f"id already exists with different content: {record['id']}")
        record.setdefault("version", 1)
        record.setdefault("created_at", stamp)
        record["updated_at"] = stamp
        state[collection].append(record)
        return {"op": kind, "collection": collection, "id": record["id"]}

    record_id = op.get("id")
    if not isinstance(record_id, str):
        raise PMError(f"{kind} requires id")
    record = get_record(state, collection, record_id)

    if kind == "revise":
        expected = op.get("expected_version")
        if expected != record.get("version", 1):
            raise PMError(f"stale version for {record_id}: expected {expected}, current {record.get('version', 1)}")
        patch = op.get("patch")
        if not isinstance(patch, dict) or not patch:
            raise PMError("revise requires non-empty patch")
        metadata_only = collection == "requirements" and set(patch).issubset({
            "code_mapping", "implementation_evidence", "source_map_status",
            "ownership_contract", "development_manual_ref", "ownership_matrix_ref",
        })
        if (collection == "requirements" and set(patch) == {"implementation_contract"}
                and additive_implementation_contract_update(record.get("implementation_contract"), patch["implementation_contract"])):
            if op.get("change_class") != "wording_only":
                raise PMError("additive implementation metadata requires change_class=wording_only")
            metadata_only = True
        gate_only = collection == "requirements" and bool(patch) and set(patch).issubset(BUILD_GATE_ONLY_FIELDS)
        locked_gate_only = gate_only and record.get("confirmation_lock", {}).get("status") == "locked"
        if locked_gate_only:
            if "readiness" in patch and patch["readiness"] != "ready_for_build":
                raise PMError("locked build-gate updates may only set readiness=ready_for_build")
            existing_gate = record.get("gate")
            incoming_gate = patch.get("gate")
            if existing_gate is not None and "gate" in patch:
                if not isinstance(incoming_gate, dict):
                    raise PMError(f"{record_id}.gate cannot be removed after confirmation is locked")
                old_status, new_status = existing_gate.get("status"), incoming_gate.get("status")
                if new_status != old_status and not (old_status == "confirmed_locked" and new_status == "approved"):
                    raise PMError(f"{record_id}.gate.status is protected; only confirmed_locked to approved is allowed during build-gate completion")
                for field in ("confirmed_source", "confirmation_protocol", "confirmed_scope_statement"):
                    if incoming_gate.get(field) != existing_gate.get(field):
                        raise PMError(f"{record_id}.gate.{field} is protected by confirmation_lock")
        if collection == "requirements" and record.get("confirmation_lock", {}).get("status") == "locked" and not metadata_only and not gate_only:
            if set(op.get("patch", {})) & CONFIRMATION_PROTECTED_FIELDS:
                raise PMError(
                    f"{record_id} confirmation is locked; an explicit scope change request is required before changing: "
                    + ", ".join(sorted(set(op.get("patch", {})) & CONFIRMATION_PROTECTED_FIELDS))
                )
        forbidden = {"id", "version", "created_at", "updated_at"} & set(patch)
        if forbidden:
            raise PMError(f"revision cannot patch: {sorted(forbidden)}")
        if collection == "requirements" and record.get("status") == "confirmed":
            if not all(op.get(key) for key in ("reason", "source_ref", "change_class")):
                raise PMError("revising a confirmed requirement requires reason, source_ref, and change_class")
            if op.get("change_class") not in CHANGE_CLASSES:
                raise PMError(f"invalid change_class: {op.get('change_class')}")
            get_record(state, "sources", op["source_ref"])
        if "status" in patch and collection in ALLOWED_TRANSITIONS:
            raise PMError("use transition to change status")
        record.update(copy.deepcopy(patch))
        if not metadata_only and not gate_only:
            record["version"] = record.get("version", 1) + 1
        record["updated_at"] = stamp
        validate_record(collection, record)
        if collection == "requirements" and op.get("change_class") != "wording_only":
            if record.get("readiness") in BUILD_READINESS:
                record["readiness"] = "ready_for_design"
            for evidence in state["evidence"]:
                if record_id in evidence.get("related_ids", []):
                    evidence["status"] = "needs_review"
                    evidence["updated_at"] = stamp
            for work in state["work_packages"]:
                if record_id in work.get("requirement_ids", []):
                    work["impact_review"] = "needed"
                    work["updated_at"] = stamp
        return {"op": kind, "collection": collection, "id": record_id, "version": record["version"]}

    if kind == "transition":
        if collection == "work_packages" and op.get("to") == "done" and workflow.config(state) and record.get("kind") in {"implementation", "verification", "foundation"}:
            if root is None:
                raise PMError("engineering WP completion requires project root")
            try:
                configured = {r["id"] for r in workflow.config(state)["scope"]}
                for rid in record.get("requirement_ids", []):
                    if rid not in configured:
                        continue
                    req = get_record(state, "requirements", rid)
                    manifest, _ = workflow.engineering_manifest(root, state, req)
                    mapped = {r["ac_id"] for r in manifest["traceability"] if record_id in r["work_package_ids"]}
                    if not mapped:
                        raise PMError(f"{record_id} has no AC traceability for {rid}")
                    coverage = {r["ac_id"]: r for r in workflow.coverage_rows(root, state, req)}
                    for acid in mapped:
                        row = coverage[acid]
                        if row["implementation"] != "complete" or row["verification"] != "passed":
                            raise PMError(f"{record_id}/{acid} completion needs complete implementation and current passing verification")
                        if workflow.scope_entry(state, req)["ui_required"] and row["frontend"] != "complete" and not (row["frontend"] == "not_required" and row.get("frontend_reason")):
                            raise PMError(f"{record_id}/{acid} completion needs frontend implementation or an applicability reason")
            except workflow.WorkflowError as exc:
                raise PMError(str(exc)) from exc
        if collection == "requirements" and record.get("confirmation_lock", {}).get("status") == "locked":
            raise PMError(f"{record_id} confirmation is locked; an explicit scope change request is required before transition")
        old = record.get("status")
        new = op.get("to")
        if new == old:
            return {"op": "noop", "collection": collection, "id": record_id, "reason": "same_status"}
        allowed = ALLOWED_TRANSITIONS.get(collection, {}).get(old, set())
        if new not in allowed:
            raise PMError(f"invalid transition for {record_id}: {old} -> {new}")
        if collection == "requirements" and new in {"confirmed", "ai_confirmed_unverified", "pending_confirmation", "rejected", "superseded"}:
            if not op.get("source_ref") or not op.get("reason"):
                raise PMError(f"transition to {new} requires source_ref and reason")
            if new in {"confirmed", "ai_confirmed_unverified"} and not record.get("acceptance_criteria"):
                raise PMError(f"{new} requirement {record_id} requires acceptance_criteria")
            get_record(state, "sources", op["source_ref"])
        record["status"] = new
        if collection == "requirements" and new in {"confirmed", "ai_confirmed_unverified", "pending_confirmation", "rejected", "superseded"}:
            record.setdefault("status_history", []).append({
                "from": old, "to": new, "reason": op["reason"], "source_ref": op["source_ref"], "timestamp": stamp
            })
        record["updated_at"] = stamp
        return {"op": kind, "collection": collection, "id": record_id, "from": old, "to": new}

    if kind == "add_source_ref":
        if collection == "requirements" and record.get("confirmation_lock", {}).get("status") == "locked":
            raise PMError(f"{record_id} confirmation is locked; an explicit scope change request is required before adding sources")
        source_ref = op.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref:
            raise PMError("add_source_ref requires source_ref")
        refs = record.setdefault("source_refs", [])
        if source_ref in refs:
            return {"op": "noop", "collection": collection, "id": record_id, "reason": "source_exists"}
        refs.append(source_ref)
        record["updated_at"] = stamp
        return {"op": kind, "collection": collection, "id": record_id, "source_ref": source_ref}

    raise PMError(f"unsupported operation: {kind}")


def validate_references(state: dict[str, Any]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    ids = {name: set(collection_map(state, name)) for name in COLLECTIONS}
    all_ids = set().union(*ids.values())
    for item in state["intake_items"]:
        for ref in item.get("source_refs", []):
            if ref not in ids["sources"]:
                errors.append({"code": "BROKEN_SOURCE", "record": item["id"], "reference": ref})
        for ref in item.get("target_ids", []):
            if ref not in all_ids:
                errors.append({"code": "BROKEN_TARGET", "record": item["id"], "reference": ref})
    for scenario in state["scenarios"]:
        for ref in scenario.get("source_refs", []):
            if ref not in ids["sources"]:
                errors.append({"code": "BROKEN_SOURCE", "record": scenario["id"], "reference": ref})
    for req in state["requirements"]:
        for ref in req.get("source_refs", []):
            if ref not in ids["sources"]:
                errors.append({"code": "BROKEN_SOURCE", "record": req["id"], "reference": ref})
        scenario = req.get("scenario_id")
        if scenario and scenario not in ids["scenarios"]:
            errors.append({"code": "BROKEN_SCENARIO", "record": req["id"], "reference": scenario})
        for ref in req.get("intake_item_ids", []):
            if ref not in ids["intake_items"]:
                errors.append({"code": "BROKEN_INTAKE_ITEM", "record": req["id"], "reference": ref})
    for wp in state["work_packages"]:
        for ref in wp.get("requirement_ids", []):
            if ref not in ids["requirements"]:
                errors.append({"code": "BROKEN_REQUIREMENT", "record": wp["id"], "reference": ref})
    for collection in ("decisions", "risks", "reviews", "evidence"):
        for record in state[collection]:
            for ref in record.get("related_ids", []):
                if ref not in all_ids:
                    errors.append({"code": "BROKEN_RELATED_ID", "record": record["id"], "reference": ref})
    return errors


def parse_deadline(value: str, timezone_name: str) -> datetime | None:
    if not value:
        return None
    try:
        local_zone = ZoneInfo(timezone_name)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.combine(date.fromisoformat(value), datetime.max.time(), tzinfo=local_zone)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=local_zone)
    except (ValueError, ZoneInfoNotFoundError):
        return None


def deadline_rows(state: dict[str, Any], hours: int = 72) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    timezone_name = str(state.get("project", {}).get("timezone") or "UTC")
    rows: list[dict[str, Any]] = []
    for collection in ("milestones", "work_packages"):
        for record in state[collection]:
            raw = record.get("due_at") or record.get("target_date")
            if not raw:
                continue
            due = parse_deadline(str(raw), timezone_name)
            if due is None:
                rows.append({"id": record["id"], "title": record.get("title", ""), "deadline": raw, "alert": "invalid"})
                continue
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            delta = due.astimezone(timezone.utc) - now
            alert = "overdue" if delta.total_seconds() < 0 else "due_soon" if delta <= timedelta(hours=hours) else "scheduled"
            rows.append({"id": record["id"], "title": record.get("title", ""), "deadline": raw, "alert": alert})
    return rows


def md(value: Any) -> str:
    if value in (None, "", []):
        return "—"
    if isinstance(value, list):
        return "、".join(str(x).replace("|", "\\|") for x in value)
    return str(value).replace("|", "\\|").replace("\n", " ")


def html_value(value: Any) -> str:
    if value in (None, "", []):
        return "—"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def html_text(value: Any) -> str:
    return html_escape(html_value(value), quote=True)


def html_bullets(values: Any, empty: str = "—") -> str:
    if not values:
        return f'<span class="muted">{html_escape(empty)}</span>'
    if not isinstance(values, list):
        values = [values]
    return "<ul>" + "".join(f"<li>{html_text(item)}</li>" for item in values) + "</ul>"


def html_status_class(value: Any) -> str:
    aliases = {
        "可开发/验收": "ready",
        "待回答": "attention",
        "需求未确认": "attention",
        "设计/实现未完成": "attention",
        "确认待拓展": "confirmed-for-extension",
        "AI已确认/待人工核验": "ai-confirmed-unverified",
        "AI已确认/待人工核验·待回答": "ai-confirmed-unverified",
    }
    if str(value) in aliases:
        return aliases[str(value)]
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(value or "unknown").lower()).strip("-")
    return normalized or "unknown"


def render_action_card(action: dict[str, Any]) -> str:
    missing = action.get("missing") or []
    missing_html = f'<p class="missing">待补齐：{html_text("、".join(missing))}</p>' if missing else ""
    criteria = action.get("acceptance_criteria") or []
    if criteria:
        acceptance_html = f'<details class="action-acceptance"><summary>验收标准（{len(criteria)} 条）</summary>{html_bullets(criteria)}</details>'
    else:
        acceptance_html = '<p class="missing">验收标准：待补充</p>'
    guidance_html = ""
    if action.get("source") == "delivery_diagnostic":
        guidance_html = (f'<p><strong>原因：</strong>{html_text(action.get("reason"))}</p>'
                         f'<p><strong>需要用户决定：</strong>{"是" if action.get("needs_user_decision") else "否，AI 继续处理"}</p>'
                         f'<details><summary>执行步骤与补齐位置</summary>{html_bullets(action.get("steps"))}'
                         f'<p>{html_text(action.get("target"))}</p></details>')
    return "".join([
        f'<article class="action-card {html_status_class(action.get("status"))}">',
        f'<div class="action-heading"><span class="badge">{html_text(action.get("id"))}</span>',
        f'<span class="state {html_status_class(action.get("status"))}">{html_text(action.get("status"))}</span></div>',
        f'<h3>{html_text(action.get("action"))}</h3>',
        f'<div class="action-meta"><span><strong>负责人</strong>{html_text(action.get("owner") or "待指定")}</span>',
        f'<span><strong>截止时间</strong>{html_text(action.get("due_at") or "待设置")}</span>',
        f'<span><strong>关联功能</strong>{html_text(action.get("requirement_ids"))}</span></div>',
        guidance_html,
        acceptance_html,
        missing_html,
        '</article>',
    ])


def render_daily_progress_row(day: dict[str, Any]) -> str:
    progressed = html_bullets(day.get("progressed_items"), "没有可追踪的推进项")
    completed = html_bullets(day.get("completed_items"), "无完成项")
    blocked = html_bullets(day.get("blocked_items"), "无阻塞项")
    product_changes = html_bullets(day.get("product_changes"), "没有识别到需求、决策或开发工作包的产品改动")
    progress_delta = html_bullets(progress_delta_text(day), "没有明确的进度状态变化")
    snapshot = day.get("current_snapshot") or {}
    snapshot_text = (
        f'需求确认 {html_text(snapshot.get("requirements_confirmed"))}/{html_text(snapshot.get("requirements_total"))}；'
        f'构建门禁 {html_text(snapshot.get("build_ready"))}/{html_text(snapshot.get("requirements_total"))}；'
        f'工作包完成 {html_text(snapshot.get("work_packages_done"))}/{html_text(snapshot.get("work_packages_total"))}'
    )
    sessions = html_text(day.get("session_ids")) if day.get("session_ids") else "无导入 session"
    group_order = ["需求与功能", "开发工作包", "产品决策", "证据与来源", "里程碑", "风险", "评审", "项目设置", "Session"]
    groups = day.get("change_groups") or {}
    group_blocks = []
    for group in group_order + [name for name in groups if name not in group_order]:
        entries = groups.get(group) or []
        if not entries:
            continue
        entry_rows = "".join(
            f'<li><strong>{"r" + html_text(entry.get("revision")) if entry.get("revision") else html_text(entry.get("summary"))}</strong> '
            f'{html_text(entry.get("detail"))} '
            f'<span class="muted">来源：{html_text(entry.get("source_ref") or "未记录")}</span></li>'
            for entry in entries
        )
        group_blocks.append(f'<section class="daily-group"><h4>{html_text(group)}（{len(entries)}）</h4><ul>{entry_rows}</ul></section>')
    grouped_details = "".join(group_blocks) or '<p class="muted">没有具体修改明细</p>'
    return "".join([
        '<details class="daily-row">',
        f'<summary><strong>{html_text(day.get("date"))}</strong> · '
        f'工作量 {html_text(day.get("workload"))}（状态操作 {html_text(day.get("operations"))}，公开消息 {html_text(day.get("session_messages"))}）</summary>',
        '<div class="daily-detail">',
        f'<p class="daily-synthesis"><strong>项目经理提炼：</strong>{html_text(day.get("synthesis") or day.get("daily_summary"))}</p>',
        f'<p class="daily-summary"><strong>量化进度：</strong>{html_text(day.get("daily_summary"))}</p>',
        f'<p><strong>具体产品改动：</strong></p><div class="daily-product-changes">{product_changes}</div>',
        f'<p><strong>进度提升：</strong>{progress_delta}</p>',
        f'<p><strong>当前累计进度：</strong>{snapshot_text}</p>',
        f'<p><strong>推进项目：</strong>{progressed}</p>',
        f'<p><strong>完成：</strong>{completed}</p>',
        f'<p><strong>阻塞：</strong>{blocked}</p>',
        f'<p><strong>按类型的具体工作与修改：</strong></p><div class="daily-changes">{grouped_details}</div>',
        f'<p class="muted"><strong>Session：</strong>{sessions}</p>',
        '</div></details>',
    ])


def requirement_overview(req: dict[str, Any]) -> tuple[str, list[str]]:
    questions: list[str] = []
    for question in req.get("decision_questions") or []:
        if question.get("status") in {"open", "deferred"}:
            questions.append(f"{question.get('id', 'DQ')}：{question.get('question', '')}")
    questions.extend(str(item) for item in req.get("open_questions") or [])
    if req.get("status") == "ai_confirmed_unverified":
        if questions:
            return "AI已确认/待人工核验·待回答", questions
        return "AI已确认/待人工核验", questions
    if req.get("status") == "superseded":
        return "已替代", questions
    if req.get("status") != "confirmed":
        return "需求未确认", questions
    if questions:
        return "待回答", questions
    if req.get("readiness") == "confirmed_for_extension":
        return "确认待拓展", questions
    if req.get("readiness") not in BUILD_READINESS:
        return "设计/实现未完成", questions
    return "可开发/验收", questions


def compact_text(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def local_day_key(timestamp_value: Any, timezone_name: str) -> str | None:
    if not timestamp_value:
        return None
    raw = str(timestamp_value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            parsed = parsed.astimezone(ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            pass
        return parsed.date().isoformat()
    except ValueError:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
        return match.group(1) if match else None


def read_session_archives(root: Path, timezone_name: str = "UTC") -> list[dict[str, Any]]:
    conv_dir = root / LOCAL_REL / "conversations"
    if not conv_dir.exists():
        return []
    archives: list[dict[str, Any]] = []
    for path in sorted(conv_dir.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True):
        messages: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = item.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = item.get("content") or item.get("text") or ""
            messages.append({
                "role": role,
                "content": compact_text(content),
                "timestamp": item.get("timestamp"),
            })
        if not messages:
            continue
        daily: dict[str, dict[str, Any]] = {}
        for message in messages:
            day = local_day_key(message.get("timestamp"), timezone_name)
            if not day:
                continue
            bucket = daily.setdefault(day, {
                "date": day,
                "messages": 0,
                "user_messages": 0,
                "assistant_messages": 0,
                "first_message": None,
                "last_message": None,
            })
            bucket["messages"] += 1
            bucket[f'{message["role"]}_messages'] += 1
            bucket["first_message"] = bucket["first_message"] or message
            bucket["last_message"] = message
        archives.append({
            "session_id": path.stem,
            "path": str(path.relative_to(root)).replace("\\", "/"),
            "message_count": len(messages),
            "user_messages": sum(item["role"] == "user" for item in messages),
            "assistant_messages": sum(item["role"] == "assistant" for item in messages),
            "first_message": messages[0],
            "last_message": messages[-1],
            "daily_activity": [daily[day] for day in sorted(daily, reverse=True)],
        })
    return archives


def state_record_label(state: dict[str, Any], collection: Any, record_id: Any) -> str:
    if not record_id:
        return ""
    records = state.get(str(collection), []) if collection else []
    record = next((item for item in records if item.get("id") == record_id), None)
    title = record.get("title") if record else None
    return f"{record_id} · {title}" if title else str(record_id)


def describe_state_operation(state: dict[str, Any], operation: dict[str, Any]) -> str:
    kind = operation.get("op")
    collection = operation.get("collection")
    record_id = operation.get("id")
    label = state_record_label(state, collection, record_id)
    if kind == "transition":
        return f"状态变更：{label} · {operation.get('from')} → {operation.get('to')}"
    if kind == "revise":
        fields = operation.get("fields") or []
        version = operation.get("version")
        suffix = f"；字段：{', '.join(str(field) for field in fields)}" if fields else ""
        if version:
            suffix += f"；版本 v{version}"
        return f"修改：{label}{suffix}"
    if kind == "add":
        return f"新增：{label or (str(collection) if collection else '项目记录')}"
    if kind == "add_source_ref":
        return f"补充来源：{label} · {operation.get('source_ref')}"
    if kind == "import_session":
        return f"导入 session：新增 {operation.get('messages_added', 0)} 条公开消息"
    if kind == "set_project":
        return f"修改项目字段：{', '.join(str(field) for field in operation.get('fields') or [])}"
    return f"{kind or '操作'}：{label or collection or '项目'}"


def daily_group_name(operation: dict[str, Any]) -> str:
    if operation.get("op") == "import_session":
        return "Session"
    collection = operation.get("collection")
    return {
        "requirements": "需求与功能",
        "scenarios": "需求与功能",
        "intake_items": "需求与功能",
        "work_packages": "开发工作包",
        "milestones": "里程碑",
        "decisions": "产品决策",
        "sources": "证据与来源",
        "evidence": "证据与来源",
        "risks": "风险",
        "reviews": "评审",
    }.get(collection, "项目设置")


def current_progress_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    requirements = state.get("requirements", [])
    work_packages = [item for item in state.get("work_packages", []) if item.get("status") != "cancelled"]
    return {
        "requirements_total": len(requirements),
        "requirements_confirmed": sum(item.get("status") in {"confirmed", "ai_confirmed_unverified"} for item in requirements),
        "requirements_ai_unverified": sum(item.get("status") == "ai_confirmed_unverified" for item in requirements),
        "build_ready": sum(item.get("readiness") in BUILD_READINESS for item in requirements),
        "work_packages_total": len(work_packages),
        "work_packages_done": sum(item.get("status") == "done" for item in work_packages),
        "work_packages_in_progress": sum(item.get("status") == "in_progress" for item in work_packages),
        "work_packages_blocked": sum(item.get("status") == "blocked" for item in work_packages),
    }


def progress_delta_text(item: dict[str, Any]) -> list[str]:
    delta = item.get("progress_delta") or {}
    labels = {
        "requirements_confirmed": "确认需求",
        "requirements_readiness_changed": "更新需求就绪状态",
        "requirements_design_updated": "补充产品/实现设计",
        "work_packages_added": "新增开发工作包",
        "work_packages_started": "启动开发工作包",
        "work_packages_completed": "完成开发工作包",
        "work_packages_blocked": "阻塞开发工作包",
        "decisions_added": "新增产品决策",
        "milestones_changed": "推进里程碑",
        "risks_changed": "更新风险",
    }
    rows = []
    for key, label in labels.items():
        values = delta.get(key) or []
        if values:
            examples = "、".join(values[:3])
            suffix = f" 等 {len(values)} 项" if len(values) > 3 else ""
            rows.append(f"{label} {len(values)} 项（{examples}{suffix}）")
    if delta.get("evidence_added"):
        rows.append(f"补充证据/来源 {len(delta['evidence_added'])} 项")
    return rows


def daily_summary_text(item: dict[str, Any]) -> str:
    parts = progress_delta_text(item)
    if not parts:
        parts.append(f"记录 {item.get('operations', 0)} 次结构化操作，但没有识别出明确的产品进度状态变化")
    if item.get("session_messages"):
        parts.append(f"处理 {item['session_messages']} 条公开 session 消息")
    snapshot = item.get("current_snapshot") or {}
    parts.append(
        "当前累计进度：需求确认 "
        f"{snapshot.get('requirements_confirmed', 0)}/{snapshot.get('requirements_total', 0)}，"
        "达到构建门禁 "
        f"{snapshot.get('build_ready', 0)}/{snapshot.get('requirements_total', 0)}，"
        "工作包完成 "
        f"{snapshot.get('work_packages_done', 0)}/{snapshot.get('work_packages_total', 0)}"
    )
    return "；".join(parts) + "。"


def daily_synthesis_text(item: dict[str, Any]) -> str:
    """把当天的结构化变更提炼成项目经理可读的工作结论。

    这段文字故意不复述每条操作，而是把操作合并成「推进主题 → 具体动作 → 当前影响」
    三层信息；完整明细仍保留在下方按类型展开的清单中。
    """
    delta = item.get("progress_delta") or {}
    themes: list[str] = []
    if delta.get("requirements_confirmed") or delta.get("requirements_design_updated"):
        themes.append("需求定稿与产品/实现设计细化")
    if delta.get("work_packages_added") or delta.get("work_packages_started") or delta.get("work_packages_completed"):
        themes.append("开发拆解与纵向交付验证")
    if delta.get("decisions_added"):
        themes.append("产品决策固化")
    if delta.get("milestones_changed"):
        themes.append("里程碑推进")
    if delta.get("evidence_added"):
        themes.append("证据与来源补齐")
    if delta.get("risks_changed"):
        themes.append("风险处理")
    if not themes and item.get("session_messages"):
        themes.append("工作记录整理")

    clauses: list[str] = []
    if delta.get("requirements_confirmed"):
        clauses.append(f"确认了 {len(delta['requirements_confirmed'])} 项需求")
    if delta.get("requirements_design_updated"):
        clauses.append(f"补齐了 {len(delta['requirements_design_updated'])} 项需求的行为、边界、验收或实现契约")
    if delta.get("work_packages_added"):
        clauses.append(f"把需求拆成 {len(delta['work_packages_added'])} 个开发工作包")
    if delta.get("work_packages_completed"):
        clauses.append(f"完成 {len(delta['work_packages_completed'])} 个工作包并形成验证结果")
    if delta.get("decisions_added"):
        clauses.append(f"沉淀 {len(delta['decisions_added'])} 项产品决策，减少后续实现歧义")
    if delta.get("milestones_changed"):
        clauses.append(f"推进 {len(delta['milestones_changed'])} 个里程碑")
    if delta.get("evidence_added"):
        clauses.append(f"补充 {len(delta['evidence_added'])} 项证据或来源")

    # 从事件摘要中提取少量有语义的结论，用来回答“到底改了什么”。
    summaries = []
    for summary in item.get("change_summaries") or []:
        text = compact_text(summary, 100)
        if not text or text.lower() in {"structured update", "状态更新"}:
            continue
        if text not in summaries:
            summaries.append(text)
    evidence = ""
    if summaries:
        evidence = "代表性结论包括：" + "；".join(summaries[:3]) + "。"

    snapshot = item.get("current_snapshot") or {}
    progress_change = (
        f"本日新增确认 {len(delta.get('requirements_confirmed') or [])} 项需求、"
        f"新增达到构建门禁 {len(delta.get('requirements_readiness_changed') or [])} 项、"
        f"完成 {len(delta.get('work_packages_completed') or [])} 个工作包"
    )
    impact = (
        f"进度变化：{progress_change}；当前累计为需求确认 {snapshot.get('requirements_confirmed', 0)}/{snapshot.get('requirements_total', 0)}，"
        f"构建门禁 {snapshot.get('build_ready', 0)}/{snapshot.get('requirements_total', 0)}，"
        f"工作包完成 {snapshot.get('work_packages_done', 0)}/{snapshot.get('work_packages_total', 0)}"
    )
    remaining = (
        f"仍有 {max(snapshot.get('requirements_total', 0) - snapshot.get('requirements_confirmed', 0), 0)} 项需求待确认、"
        f"{max(snapshot.get('requirements_total', 0) - snapshot.get('build_ready', 0), 0)} 项需求未达到构建门禁"
    )
    if not clauses:
        clauses.append(f"整理了 {item.get('operations', 0)} 次结构化变更")
    lead = "今天主要推进" + "、".join(themes) + "：" if themes else "今天主要完成："
    return lead + "，".join(clauses) + "。" + (evidence + " ") + impact + "；" + remaining + "。"


def build_daily_progress(state: dict[str, Any], session_archives: list[dict[str, Any]], limit: int = 30) -> list[dict[str, Any]]:
    timezone_name = state.get("project", {}).get("timezone") or "UTC"
    by_day: dict[str, dict[str, Any]] = {}

    def bucket_for(day: str) -> dict[str, Any]:
        return by_day.setdefault(day, {
            "date": day,
            "state_changes": 0,
            "operations": 0,
            "session_messages": 0,
            "session_ids": [],
            "progressed_items": [],
            "completed_items": [],
            "blocked_items": [],
            "change_summaries": [],
            "change_details": [],
            "change_groups": {},
            "product_changes": [],
            "progress_delta": {
                "requirements_confirmed": [],
                "requirements_readiness_changed": [],
                "requirements_design_updated": [],
                "work_packages_added": [],
                "work_packages_started": [],
                "work_packages_completed": [],
                "work_packages_blocked": [],
                "decisions_added": [],
                "milestones_changed": [],
                "risks_changed": [],
                "evidence_added": [],
            },
            "current_snapshot": current_progress_snapshot(state),
            "synthesis": "",
        })

    def add_unique(values: list[str], value: Any) -> None:
        if value and value not in values:
            values.append(str(value))

    for event in state.get("change_events", []):
        day = local_day_key(event.get("timestamp"), timezone_name)
        if not day:
            continue
        bucket = bucket_for(day)
        bucket["state_changes"] += 1
        operations = event.get("operations") or []
        bucket["operations"] += len(operations)
        summary = compact_text(event.get("summary"), 180)
        if summary:
            bucket["change_summaries"].append(summary)
        bucket["change_details"].append({
            "revision": event.get("revision"),
            "timestamp": event.get("timestamp"),
            "summary": summary,
            "source_ref": event.get("source_ref"),
            "operations": [describe_state_operation(state, operation) for operation in operations],
        })
        if not operations:
            bucket["change_groups"].setdefault("项目设置", []).append({
                "revision": event.get("revision"),
                "summary": summary,
                "source_ref": event.get("source_ref"),
                "detail": "记录了一次项目状态更新，但没有可展开的结构化操作。",
            })
        for operation in operations:
            collection = operation.get("collection")
            record_id = operation.get("id")
            label = state_record_label(state, collection, record_id) or record_id
            detail = describe_state_operation(state, operation)
            if collection in {"requirements", "work_packages", "milestones", "decisions", "risks", "reviews"}:
                add_unique(bucket["product_changes"], detail)
            group = daily_group_name(operation)
            bucket["change_groups"].setdefault(group, []).append({
                "revision": event.get("revision"),
                "summary": summary,
                "source_ref": event.get("source_ref"),
                "detail": detail,
            })
            delta = bucket["progress_delta"]
            kind = operation.get("op")
            if kind == "add" and collection == "requirements":
                add_unique(delta["requirements_design_updated"], label)
            if kind == "add" and collection == "work_packages":
                add_unique(delta["work_packages_added"], label)
            if kind == "add" and collection == "decisions":
                add_unique(delta["decisions_added"], label)
            if kind == "add" and collection in {"sources", "evidence"}:
                add_unique(delta["evidence_added"], label)
            if kind == "transition":
                if collection == "requirements" and operation.get("to") == "confirmed":
                    add_unique(delta["requirements_confirmed"], label)
                if collection == "work_packages" and operation.get("to") == "in_progress":
                    add_unique(delta["work_packages_started"], label)
                if collection == "work_packages" and operation.get("to") == "done":
                    add_unique(delta["work_packages_completed"], label)
                if collection == "work_packages" and operation.get("to") == "blocked":
                    add_unique(delta["work_packages_blocked"], label)
                if collection == "milestones":
                    add_unique(delta["milestones_changed"], label)
                if collection == "risks":
                    add_unique(delta["risks_changed"], label)
            if kind == "revise" and collection == "requirements":
                fields = set(operation.get("fields") or [])
                if "readiness" in fields:
                    add_unique(delta["requirements_readiness_changed"], label)
                if fields & {"behavior", "boundaries", "acceptance_criteria", "technical_design", "implementation_contract", "decision_questions"}:
                    add_unique(delta["requirements_design_updated"], label)
            if collection in {"requirements", "work_packages", "milestones", "risks", "reviews"}:
                add_unique(bucket["progressed_items"], label)
            if operation.get("to") == "done" or (collection == "work_packages" and operation.get("to") == "reached"):
                add_unique(bucket["completed_items"], label)
            if operation.get("to") == "blocked":
                add_unique(bucket["blocked_items"], label)

    for archive in session_archives:
        for activity in archive.get("daily_activity") or []:
            day = activity.get("date")
            if not day:
                continue
            bucket = bucket_for(day)
            bucket["session_messages"] += activity.get("messages", 0)
            add_unique(bucket["session_ids"], archive.get("session_id"))
            bucket["change_groups"].setdefault("Session", []).append({
                "revision": None,
                "summary": archive.get("session_id"),
                "source_ref": archive.get("path"),
                "detail": f"公开消息 {activity.get('messages', 0)} 条（用户 {activity.get('user_messages', 0)}，助手 {activity.get('assistant_messages', 0)}）",
            })

    daily = []
    for day in sorted(by_day, reverse=True)[:limit]:
        item = by_day[day]
        item["workload"] = item["operations"] + item["session_messages"]
        item["change_summaries"] = item["change_summaries"][:8]
        item["change_details"] = item["change_details"][-50:]
        item["daily_summary"] = daily_summary_text(item)
        item["synthesis"] = daily_synthesis_text(item)
        daily.append(item)
    return daily


def engineering_docs_root(root: Path) -> Path:
    """Honor an explicitly configured docs root; never guess or relocate it."""
    config_path = root / ".project-to-act" / "PROJECT_CONFIG.json"
    config = read_json(config_path) if config_path.exists() else {}
    configured = config.get("canonical_docs_root") or str(DOCS_REL.parent)
    return (root / configured).resolve()


def shaping_gaps(req: dict[str, Any]) -> list[str]:
    """Collect missing material before the first-error schema validator runs."""
    gaps = []
    for section, fields in BUILD_GATE_FIELDS.items():
        value = req.get(section)
        if not isinstance(value, dict):
            gaps.append(section)
            continue
        for field in sorted(fields):
            if section == "gate" and field != "review_lenses":
                continue  # Approval fields are supplied by confirmation, not design work.
            if field not in value or (value[field] in (None, "", [], {}) and field not in {
                "unresolved_items", "decisions",
            }):
                gaps.append(f"{section}.{field}")
    if not req.get("acceptance_scenarios"):
        gaps.append("acceptance_scenarios")
    # Check deeper invariants without changing the actual requirement or its lock.
    if not gaps:
        probe = copy.deepcopy(req)
        probe.update(status="confirmed", readiness="ready_for_build")
        probe["gate"].update(status="approved", confirmed_source="SRC-VALIDATION-ONLY",
                             confirmation_protocol="explicit_scope_confirmation",
                             confirmed_scope_statement="validation-only; never persisted")
        try:
            validate_record("requirements", probe)
        except PMError as exc:
            gaps.append(str(exc))
    return gaps


def delivery_diagnostics(root: Path, state: dict[str, Any], requirement_ids: list[str] | None = None) -> dict[str, Any]:
    """Read-only handoff checks. A file or schema pass is never product acceptance."""
    selected = set(requirement_ids or [])
    known = {req["id"] for req in state["requirements"]}
    if selected - known:
        raise PMError(f"unknown requirement IDs: {', '.join(sorted(selected - known))}")
    docs_root = engineering_docs_root(root)
    development_root = docs_root / "project-development"
    findings: list[dict[str, Any]] = []
    rows = []

    def finding(req: dict[str, Any], code: str, stage: str, reason: str, action: str,
                steps: list[str], done: str, *, needs_user: bool = False,
                target: str = "", priority: str = "P1", fields: list[str] | None = None) -> None:
        findings.append({
            "id": f"{req['id']}:{code}", "code": code, "record": req["id"],
            "requirement_version": req.get("version", 1), "stage": stage,
            "priority": priority, "reason": reason, "action": action, "steps": steps,
            "owner": "用户（产品决定）" if needs_user else "AI（项目经理/开发）",
            "needs_user_decision": needs_user, "target": target,
            "acceptance_criteria": [done], "missing_fields": fields or [],
            "due_at": req.get("due_at") or req.get("target_date"),
            "source": "delivery_diagnostic", "requirement_ids": [req["id"]],
            "status": "waiting_user" if needs_user else "not_started",
        })

    for req in state["requirements"]:
        rid = req["id"]
        if (selected and rid not in selected) or req.get("status") in {"rejected", "superseded", "candidate"}:
            continue
        if not re.fullmatch(r"REQ-[A-Za-z0-9][A-Za-z0-9_-]*", rid) or not isinstance(req.get("version", 1), int) or req.get("version", 1) < 1:
            raise PMError(f"unsafe requirement ID or version for engineering package path: {rid}")
        start = len(findings)
        confirmed = req.get("status") in {"confirmed", "ai_confirmed_unverified"}
        extension = req.get("readiness") == "confirmed_for_extension"
        questions = [q for q in req.get("decision_questions") or [] if q.get("status") in {"open", "deferred"}]
        gaps = [] if extension else shaping_gaps(req)
        package_dir = development_root / "packages" / rid / f"v{req.get('version', 1)}"
        package = package_dir / "ENGINEERING-PACKAGE.md"
        older = sorted(package_dir.parent.glob("v*/ENGINEERING-PACKAGE.md")) if package_dir.parent.exists() else []
        linked = [wp for wp in state["work_packages"] if rid in (wp.get("requirement_ids") or []) and wp.get("status") != "cancelled"]
        engineering_work = [wp for wp in linked if wp.get("kind") not in {"discovery", "governance", "confirmation"}
                            and (len(wp.get("requirement_ids") or []) == 1
                                 or wp.get("kind") in {"implementation", "verification", "foundation"})]
        if gaps:
            finding(req, "FEATURE_SHAPING_INCOMPLETE", "engineering" if confirmed else "product",
                    "缺少可交给实现者的结构化设计或审查记录；这属于待补齐材料，不能仅称为用户未确认。",
                    f"补齐 {rid} 的定档材料：{'、'.join(gaps)}",
                    ["读取当前版本已回答问题、确认来源、详细草案和真实代码，复用已有结论。",
                     "按 missing_fields 补结构化记录、原子功能、状态和验证计划；不要求先实现或先验收。",
                     "仅将真正新增的产品行为取舍发给用户；没有新取舍时自主补齐并重新预检。"],
                    "缺失材料有可追溯内容且五类设计检查通过，产品范围与既有决定一致。",
                    target=str(root / DOCS_REL / STATE_NAME), fields=gaps,
                    priority="P0" if req.get("readiness") in BUILD_READINESS else "P1")
        if req.get("readiness") in BUILD_READINESS:
            try:
                validate_record("requirements", req)
            except PMError as exc:
                finding(req, "BUILD_GATE_RECORD_INVALID", "engineering", str(exc),
                        f"修复 {rid} 当前已声明门禁与真实记录的不一致",
                        ["核对缺失字段和有效确认来源，按锁保护规则修复；不得伪造来源或放宽门禁。"],
                        "当前记录本身通过真实门禁校验，未使用诊断预检值代替确认。", priority="P0")
        if questions or req.get("open_questions"):
            finding(req, "PRODUCT_DECISIONS_OPEN", "product", "存在尚未回答或明确延期的产品决定。",
                    f"展示 {rid} 当前未决确认项及推荐方案",
                    ["沿用当前问题 ID、版本和用户已回答内容，不重新询问已关闭问题。",
                     "给出各未决项的推荐、替代、行为影响及回答入口；deferred 项只在新证据或行为变化时重开。"],
                    "当前范围的未决项已明确选择，或有可追溯的延期/非目标处理。", needs_user=True)
        closure_received = bool(req.get("closure_attempt", {}).get("source_ref") or
                                req.get("confirmation_package_status", {}).get("closure_signal_received"))
        if not confirmed and not questions and not req.get("open_questions") and not gaps:
            finding(req, "CONFIRMATION_RECORD_PENDING" if closure_received else "SCOPE_CONFIRMATION_PENDING", "product",
                    "已收到封闭指令，需校验并落盘。" if closure_received else "设计材料齐备，但尚无当前范围的明确需求级确认。",
                    f"核对 {rid} 已有封闭来源并完成状态预检" if closure_received else f"展示 {rid} 最终范围和封闭入口",
                    ["检查来源确实覆盖当前版本；若范围未变，复用已有封闭指令，不要求用户重复确认。" if closure_received
                     else "说明本轮完整范围、非目标、关键行为及确认后的自动工程交接动作，等待明确需求级确认。"],
                    "状态和确认来源准确，AI 确认保留未人工核验标记。", needs_user=not closure_received)
        if not package.is_file():
            code = "ENGINEERING_PACKAGE_STALE" if older else "ENGINEERING_PACKAGE_MISSING"
            finding(req, code, "engineering", "当前版本没有工程包。" + ("已有其他版本工程包，须评估差异后建立新版。" if older else ""),
                    f"为 {rid} v{req.get('version', 1)} 调用 project-development 工程包模式",
                    ["先核对本期范围与模块归属；占位或由其他 REQ 承载时沿用既有决定，不自建实现范围。",
                     "调查真实仓库基线、入口、模块和现有改动；保留旧版并引用替代关系。",
                     "写工程设计、接口/数据契约、执行工作包、验证计划与回滚；输入不足时标 draft/blocked。",
                     "同步版本索引、开发区索引和根索引，通过 PM 操作回写 WP/EVD 引用后重跑 doctor。"],
                    "当前版本工程包、基线、可执行步骤、追踪关系和索引齐备；草稿不算开发就绪。",
                    target=str(package), priority="P0" if req.get("readiness") in BUILD_READINESS else "P1")
        else:
            text = package.read_text(encoding="utf-8-sig")
            if not text.strip() or "<!-- pm:package-draft -->" in text:
                finding(req, "ENGINEERING_PACKAGE_DRAFT", "engineering", "工程包为空或显式标为未完成草稿。",
                        f"完成 {rid} 工程调查与执行契约", ["按工程包缺口完成调查，移除草稿标记前复核真实内容。"],
                        "工程包内容完成且可执行；不得用文件存在替代内容审查。", target=str(package))
            elif rid not in text or not re.search(rf"\bv{req.get('version', 1)}\b", text[:400], flags=re.IGNORECASE):
                finding(req, "ENGINEERING_PACKAGE_IDENTITY_UNVERIFIED", "engineering",
                        "工程包正文未能识别出当前 REQ 与版本，不能只根据目录认为它匹配。",
                        f"核对 {rid} 工程包身份与输入版本",
                        ["核对包头、输入规格、变更差异及确认来源；补正确身份，不能仅改标题冒充已复核。"],
                        "包头清楚标注当前 REQ/版本，内容已按当前契约复核。", target=str(package))
            missing_indexes = []
            for index_path in (package_dir / "INDEX.md", development_root / "INDEX.md", docs_root / "README.md"):
                relative = package.relative_to(index_path.parent).as_posix() if package.is_relative_to(index_path.parent) else ""
                # Root index links to the development index, not every individual package.
                expected = "project-development/INDEX.md" if index_path == docs_root / "README.md" else relative
                if not index_path.is_file() or (expected and expected not in index_path.read_text(encoding="utf-8-sig")):
                    missing_indexes.append(str(index_path))
            if missing_indexes:
                finding(req, "ENGINEERING_INDEX_INCOMPLETE", "engineering", "工程文档存在，但索引缺失或未登记当前版本路径。",
                        f"补齐 {rid} 工程包入口索引", ["在既有索引原位登记当前路径、版本、用途和旧版替代关系。"],
                        "版本索引、开发区索引和根索引均能发现当前版本工程包。", fields=missing_indexes)
        if confirmed and not engineering_work:
            finding(req, "CONFIRMED_WITHOUT_WORK_PACKAGE", "engineering", "没有非取消的专属或明确声明用途的工程工作包；通用治理任务不能替代实现任务。",
                    f"登记 {rid} 的实现/验证工作包", ["按纵向交付划分 WP，写关联 REQ、kind、步骤、依赖、完成条件和验证方法。",
                     "用最新 revision 和有效来源经 apply --dry-run、apply 登记；不直接编辑 state.json。"],
                    "可执行工作包已登记且需求/工程包/WP 的引用一致。")
        if confirmed and not extension and not gaps and req.get("readiness") not in BUILD_READINESS:
            finding(req, "BUILD_GATE_REVIEW_PENDING", "engineering", "产品已确认且结构化设计可预检，开发就绪状态尚未同步。",
                    f"完成 {rid} 独立开发门禁复核并同步 readiness",
                    ["先完成工程包、WP、代码调查和接口审查，对拟提交的 gate-only 更新运行 apply --dry-run。",
                     "沿用有效确认来源，以允许的 gate-only 操作 dry-run 后推进；不解锁或改产品范围，不要求先实现。"],
                    "工程交接复核通过且合法推进 ready_for_build；实现与验收仍分别记录。")
        for wp in engineering_work:
            if wp.get("status") in {"not_started", "in_progress", "blocked"} and not wp.get("next_action"):
                finding(req, "WORK_PACKAGE_ACTION_MISSING", "planning", f"{wp['id']} 只有状态，没有可执行的下一步。",
                        f"为 {wp['id']} 补实施动作和完成条件",
                        ["读取该 WP 的工程包步骤，填写 next_action、负责人、依赖、完成条件及验证方法；日期未知时保留待协商。"],
                        "工作包能说明接下来修改/调查什么、交付什么、如何验证。", target=wp["id"], priority="P2")
        current = findings[start:]
        blockers = [f for f in current if f["stage"] in {"product", "engineering"}]
        rows.append({
            "id": rid, "version": req.get("version", 1), "status": req.get("status"),
            "readiness": req.get("readiness"), "package_path": str(package), "package_exists": package.is_file(),
            "other_package_paths": [str(p) for p in older if p != package],
            "engineering_work_package_ids": [wp["id"] for wp in engineering_work],
            "can_start_full_build": confirmed and not extension and req.get("readiness") == "ready_for_build" and not blockers,
            "extension_only": extension, "closure_signal_received": closure_received,
            "blocking_codes": [f["code"] for f in blockers],
            "verification": "not_assessed", "acceptance": "not_assessed",
        })
    if workflow.config(state):
        lifecycle = workflow.report(root, state, requirement_ids)
        lifecycle_rows = {r["id"]: r for r in lifecycle["requirements"]}
        for row in rows:
            current = lifecycle_rows.get(row["id"])
            if current:
                row["stage_results"] = {k: v["result"] for k, v in current["stages"].items()}
                row["can_start_full_build"] = row["can_start_full_build"] and current["stages"]["engineering"]["result"] == "passed"
                row["verification"] = current["stages"]["verification"]["result"]
                row["acceptance"] = current["stages"]["acceptance"]["result"]
                if current["stages"]["engineering"]["result"] != "passed":
                    row["blocking_codes"].append("STAGE_HANDOFF_NOT_PASSED")
                if current["next_stage"]:
                    stage = current["next_stage"]
                    req = get_record(state, "requirements", row["id"])
                    finding(req, "DELIVERY_STAGE_" + stage.upper(), stage,
                            "; ".join(current["stages"][stage]["gaps"][:6]) or "阶段内容验收尚未通过。",
                            f"完成 {stage} 内容复核并记录交接回执",
                            ["读取 workflow 的逐 AC 覆盖、已有设计/代码/证据，复核复用；未评估不表示未实现。",
                             "自主补齐工程材料、实现或验证缺口，按 DELIVERY-1 记录 review_stage；新增产品决定才询问用户。"],
                            "本阶段内容与当前证据已复核，合法写入回执，继续已授权下一阶段。",
                            needs_user=stage == "acceptance", target=str(root / DOCS_REL / "DELIVERY-WORKFLOW.md"))
    for item in findings:
        item["depends_on"] = ([f["id"] for f in findings if f["record"] == item["record"]
                               and f["id"] != item["id"] and f["stage"] in {"product", "engineering"}]
                              if item["code"] == "BUILD_GATE_REVIEW_PENDING" else [])
        item["ready_to_execute"] = not item["needs_user_decision"] and not item["depends_on"]
    findings.sort(key=lambda f: (f["priority"], bool(f["depends_on"]), f["record"], f["code"]))
    return {"revision": state["revision"], "generated_at": now_iso(), "requirements": rows, "findings": findings,
            "automatic_action_ids": [f["id"] for f in findings if not f["needs_user_decision"]],
            "ready_action_ids": [f["id"] for f in findings if f["ready_to_execute"]],
            "user_decision_ids": [f["id"] for f in findings if f["needs_user_decision"]],
            "notice": "脚本检查结构、文件和引用；工程内容、源码及产品验收仍需对应证据审查。未实现/未执行验收不阻止准备工程包。"}


def render_development_guide(report: dict[str, Any]) -> str:
    lines = ["# 开发衔接与缺口处理", "", "> 由 pm.py 自动生成；不修改需求状态，不代表产品验收。", "",
             f"PM revision：{report['revision']}；生成于 {report['generated_at']}", "", report["notice"], "",
             "## 当前需求", "", "| REQ | 版本 | 状态 / 就绪度 | 当前工程包 | 可开始完整开发 |", "|---|---|---|---|---|"]
    for row in report["requirements"]:
        outcome = (('工程阶段已签收，可按既有授权执行' if row.get('stage_results') else '结构条件已满足，执行前复核契约') if row['can_start_full_build'] else
                   ' / '.join(row['blocking_codes']) or ('仅已授权基础扩展' if row['extension_only'] else '按当前阶段处理'))
        lines.append(f"| {row['id']} | v{row['version']} | {row['status']} / {row['readiness']} | {'存在' if row['package_exists'] else '缺失'} | {outcome} |")
    lines += ["", "## 自动补齐队列与待决定事项", ""]
    for item in report["findings"]:
        lines += [f"### {item['priority']} · {item['record']} · {item['code']}", "",
                  f"- 原因：{item['reason']}", f"- 下一步：{item['action']}", f"- 执行者：{item['owner']}",
                  f"- 是否需要用户决定：{'是' if item['needs_user_decision'] else '否，AI 继续处理'}",
                  f"- 位置：{item['target'] or '当前 REQ 的结构化记录/确认包'}",
                  f"- 截止：{item['due_at'] or '待协商，不虚构日期'}", "",
                  f"- 前置动作：{'、'.join(item['depends_on']) or '无脚本已识别的前置阻塞；执行时核对现有契约'}", "",
                  *[f"{i}. {step}" for i, step in enumerate(item['steps'], 1)], "",
                  f"完成条件：{item['acceptance_criteria'][0]}", ""]
    if not report["findings"]:
        lines += ["当前检查未发现交接结构缺口；继续按工程包执行和验证。", ""]
    return "\n".join(lines)


def cmd_doctor(args: argparse.Namespace) -> dict[str, Any]:
    state = load_state(args.project_root)
    report = delivery_diagnostics(args.project_root, state, getattr(args, "requirement", None))
    report["delivery_workflow"] = workflow.report(args.project_root, state, getattr(args, "requirement", None))
    if getattr(args, "write_report", False):
        # Always persist the full project report so a filtered run cannot hide other gaps.
        with project_lock(args.project_root):
            state = load_state(args.project_root)
            full = delivery_diagnostics(args.project_root, state)
            path = args.project_root / DOCS_REL / "DEVELOPMENT-GUIDE.md"
            atomic_text(path, render_development_guide(full))
            report = delivery_diagnostics(args.project_root, state, getattr(args, "requirement", None))
            report["delivery_workflow"] = workflow.report(args.project_root, state, getattr(args, "requirement", None))
            report["report_path"] = str(path)
    return report


def build_progress_summary(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    requirements = state["requirements"]
    work_packages = state["work_packages"]
    project = state.get("project", {})
    requirements_by_id = {req.get("id"): req for req in requirements}
    pending_requirements = [
        {"id": req["id"], "title": req.get("title"), "status": req.get("status"), "readiness": req.get("readiness")}
        for req in requirements
        if req.get("status") in {"candidate", "clarification_needed", "pending_confirmation"}
        or requirement_overview(req)[0] in {"待回答", "设计/实现未完成"}
    ]
    next_actions: list[dict[str, Any]] = []
    for item in work_packages:
        if item.get("status") not in {"in_progress", "blocked", "not_started"}:
            continue
        requirement_ids = item.get("requirement_ids") or []
        linked_requirements = [requirements_by_id[rid] for rid in requirement_ids if rid in requirements_by_id]
        acceptance = item.get("acceptance_criteria") or [
            criterion
            for req in linked_requirements
            for criterion in (req.get("acceptance_criteria") or [])
        ]
        owner = item.get("owner") or project.get("owner")
        due_at = item.get("due_at") or item.get("target_date")
        missing = []
        if not owner or owner == "unknown":
            missing.append("负责人")
        if not due_at:
            missing.append("截止时间")
        if not acceptance:
            missing.append("验收标准")
        next_actions.append({
            "id": item.get("id"),
            "action": item.get("next_action") or f"从关联工程包补齐 {item.get('id')} 的实施步骤、依赖和完成条件，并登记下一项执行动作",
            "status": item.get("status"),
            "owner": owner,
            "due_at": due_at,
            "acceptance_criteria": acceptance,
            "requirement_ids": requirement_ids,
            "source": "work_package",
            "missing": missing,
        })
    active_work_requirement_ids = {
        rid for item in next_actions for rid in item.get("requirement_ids") or []
    }
    for req in requirements:
        label, _ = requirement_overview(req)
        if label == "可开发/验收" or req.get("id") in active_work_requirement_ids:
            continue
        if req.get("status") not in {"candidate", "clarification_needed", "pending_confirmation", "confirmed"}:
            continue
        owner = req.get("owner") or project.get("owner")
        due_at = req.get("due_at") or req.get("target_date")
        missing = []
        if not owner or owner == "unknown":
            missing.append("负责人")
        if not due_at:
            missing.append("截止时间")
        if not req.get("acceptance_criteria"):
            missing.append("验收标准")
        next_actions.append({
            "id": f"{req.get('id')}-DESIGN",
            "action": req.get("next_action") or f"运行 doctor --requirement {req.get('id')}，逐项处理定档材料、当前工程包与工作包缺口",
            "status": "blocked" if req.get("status") != "confirmed" else "not_started",
            "owner": owner,
            "due_at": due_at,
            "acceptance_criteria": req.get("acceptance_criteria") or [],
            "requirement_ids": [req.get("id")],
            "source": "requirement_gate",
            "missing": missing,
        })
    diagnostic = delivery_diagnostics(root, state)
    # Keep WP-specific execution information, but never let a broad WP hide REQ gaps.
    next_actions = diagnostic["findings"] + next_actions
    session_archives = read_session_archives(root, project.get("timezone") or "UTC")
    return {
        "revision": state.get("revision"),
        "stage": state.get("project", {}).get("stage"),
        "active_mode": state.get("project", {}).get("active_mode"),
        "completed_work": [
            {"id": item["id"], "title": item.get("title"), "status": item.get("status")}
            for item in work_packages if item.get("status") == "done"
        ],
        "in_progress_work": [
            {"id": item["id"], "title": item.get("title"), "status": item.get("status"), "requirement_ids": item.get("requirement_ids")}
            for item in work_packages if item.get("status") in {"in_progress", "blocked", "not_started"}
        ],
        "pending_requirements": pending_requirements,
        "next_actions": next_actions,
        "delivery_diagnostics": {key: value for key, value in diagnostic.items() if key != "findings"},
        "open_risks": [
            {"id": risk["id"], "title": risk.get("title"), "status": risk.get("status"), "impact": risk.get("impact"), "response": risk.get("response")}
            for risk in state["risks"] if risk.get("status") != "closed"
        ],
        "recent_changes": state.get("change_events", [])[-8:],
        "session_archives": session_archives,
        "daily_progress": build_daily_progress(state, session_archives),
    }


def render_project_overview(root: Path, state: dict[str, Any]) -> None:
    docs = root / DOCS_REL
    project = state["project"]
    requirements = state["requirements"]
    work_packages = state["work_packages"]
    milestones = state["milestones"]
    risks = state["risks"]
    reviews = state["reviews"]
    confirmed = sum(req.get("status") in {"confirmed", "ai_confirmed_unverified"} for req in requirements)
    ai_unverified = sum(req.get("status") == "ai_confirmed_unverified" for req in requirements)
    build_ready = sum(req.get("readiness") in BUILD_READINESS for req in requirements)
    extension_ready = sum(req.get("readiness") == "confirmed_for_extension" for req in requirements)
    undecided = []
    for req in requirements:
        label, questions = requirement_overview(req)
        if label != "可开发/验收":
            undecided.append((req, label, questions))
    done_work = sum(item.get("status") == "done" for item in work_packages)
    generated = now_iso()
    lifecycle_html = ""
    if workflow.config(state):
        flow = workflow.report(root, state)
        phase_rows = "".join(
            f'<tr><td>{html_text(row["id"])} v{row["version"]}</td>'
            + "".join(f'<td>{html_text(row["stages"][s]["result"])}</td>' for s in workflow.STAGES)
            + f'<td>{html_text(row["next_stage"] or "已交付")}</td></tr>' for row in flow["requirements"]
        )
        lifecycle_html = ('<section class="panel"><h2>阶段验收与完整交付</h2>'
                          '<p>未评估表示尚未核对现有材料，不表示功能未实现。结构检查与阶段验收分别报告。</p>'
                          f'<p>{html_text(flow["totals"])}</p>'
                          '<div class="table-wrap"><table><thead><tr><th>REQ</th><th>规格固化</th><th>工程交接</th>'
                          '<th>完整实现</th><th>技术验证</th><th>产品验收</th><th>下一阶段</th></tr></thead>'
                          f'<tbody>{phase_rows}</tbody></table></div><p><a href="DELIVERY-WORKFLOW.md">逐条 AC、缺口和责任入口</a></p></section>')
    progress = build_progress_summary(root, state)

    feature_rows: list[str] = []
    feature_cards: list[str] = []
    for req in requirements:
        label, questions = requirement_overview(req)
        req_id = str(req.get("id", "REQ"))
        anchor = html_status_class(req_id)
        search = html_escape(" ".join(str(req.get(key, "")) for key in ("id", "title", "status", "readiness", "behavior")), quote=True)
        feature_rows.append(
            f'<tr data-search="{search}" data-state="{html_status_class(label)}">'
            f'<td><a href="#{anchor}">{html_text(req_id)}</a></td>'
            f'<td>{html_text(req.get("title"))}</td>'
            f'<td><span class="badge {html_status_class(req.get("status"))}">{html_text(req.get("status"))}</span></td>'
            f'<td><span class="badge {html_status_class(req.get("readiness"))}">{html_text(req.get("readiness"))}</span></td>'
            f'<td><span class="state {html_status_class(label)}">{html_text(label)}</span></td>'
            f'<td>{html_text(req.get("priority"))}</td></tr>'
        )
        implementation = req.get("implementation_contract") or {}
        surfaces = implementation.get("implementation_surfaces") or []
        surface_blocks = []
        for surface in surfaces:
            surface_blocks.append(
                "<article class=\"surface\">"
                f"<h4>{html_text(surface.get('name'))}</h4>"
                f"<p><strong>当前结构：</strong>{html_text(surface.get('current_structure'))}</p>"
                f"<p><strong>本次决定：</strong>{html_text(surface.get('decision'))}</p>"
                f"<p><strong>接口/数据：</strong>{html_text(surface.get('interfaces_or_data'))}</p>"
                f"<p><strong>执行步骤：</strong>{html_bullets(surface.get('execution_steps'))}</p>"
                f"<p><strong>失败与回滚：</strong>{html_text(surface.get('failure_and_rollback'))}</p>"
                f"<p><strong>观测与测试：</strong>{html_text(surface.get('observability'))}；{html_text(surface.get('test_mapping'))}</p>"
                f"<p><strong>任务：</strong>{html_text(surface.get('task_mapping'))}</p>"
                "</article>"
            )
        question_html = html_bullets(questions, "没有未决问题")
        surface_html = "".join(surface_blocks) if surface_blocks else '<p class="muted">尚未形成动态实现面清单。</p>'
        feature_cards.append(
            f'<section class="feature-card" id="{anchor}" data-search="{search}">'
            f'<div class="feature-heading"><h3>{html_text(req_id)} · {html_text(req.get("title"))}</h3>'
            f'<span class="state {html_status_class(label)}">{html_text(label)}</span></div>'
            f'<div class="meta"><span>需求状态：{html_text(req.get("status"))}</span><span>就绪度：{html_text(req.get("readiness"))}</span><span>优先级：{html_text(req.get("priority"))}</span><span>版本：v{html_text(req.get("version", 1))}</span></div>'
            f'<p class="clamp-2"><strong>可观察行为：</strong>{html_text(req.get("behavior"))}</p>'
            f'<details class="feature-definition"><summary>产品定义、未决问题与验收</summary>'
            f'<p><strong>用户价值：</strong>{html_text(req.get("user_value"))}</p>'
            f'<p><strong>边界：</strong>{html_text(req.get("boundaries"))}</p>'
            f'<div class="two-col"><div><h4>未决问题</h4>{question_html}</div>'
            f'<div><h4>验收条件</h4>{html_bullets(req.get("acceptance_criteria"))}</div></div></details>'
            f'<details><summary>实现思路与开发契约</summary>'
            f'<p><strong>技术方案：</strong>{html_text((req.get("technical_design") or {}).get("stack"))}</p>'
            f'<p><strong>实现决策：</strong>{html_bullets(implementation.get("decisions"))}</p>'
            f'{surface_html}'
            f'<p><strong>原子功能单元：</strong>{html_bullets([unit.get("id", "") + "：" + str(unit.get("title", "")) for unit in implementation.get("functional_decomposition") or []])}</p>'
            f'<p><strong>状态模型：</strong>{html_bullets([item.get("state", "") for item in implementation.get("state_model") or []])}</p>'
            f'<p><strong>验证矩阵：</strong>{html_bullets([item.get("id", "") + "：" + str(item.get("scenario", "")) for item in implementation.get("verification_matrix") or []])}</p>'
            "</details></section>"
        )

    undecided_html = html_bullets([
        f"{req.get('id')} · {req.get('title')}：{label}"
        for req, label, _ in undecided
    ], "当前没有待敲定功能")
    work_rows = "".join(
        f"<tr><td>{html_text(item.get('id'))}</td><td>{html_text(item.get('title'))}</td>"
        f"<td><span class=\"badge {html_status_class(item.get('status'))}\">{html_text(item.get('status'))}</span></td>"
        f"<td>{html_text(item.get('requirement_ids'))}</td><td>{html_text(item.get('due_at') or item.get('target_date'))}</td></tr>"
        for item in work_packages
    ) or '<tr><td colspan="5" class="muted">尚无工作包</td></tr>'
    milestone_rows = "".join(
        f"<li><strong>{html_text(item.get('id'))}</strong> {html_text(item.get('title'))} · {html_text(item.get('status'))} · {html_text(item.get('due_at') or item.get('target_date'))}</li>"
        for item in milestones
    ) or '<li class="muted">尚无里程碑</li>'
    risk_rows = "".join(
        f"<li><strong>{html_text(item.get('id'))}</strong> {html_text(item.get('title'))} · {html_text(item.get('status'))}<br><span class=\"muted\">{html_text(item.get('impact'))}</span></li>"
        for item in risks
    ) or '<li class="muted">尚无风险</li>'
    review_rows = "".join(
        f"<li><strong>{html_text(item.get('id'))}</strong> {html_text(item.get('title'))} · {html_text(item.get('kind'))}<br><span class=\"muted\">建议：{html_text(item.get('recommendation'))}</span></li>"
        for item in reviews
    ) or '<li class="muted">尚无评审项</li>'

    change_rows = "".join(
        f'<li><strong>r{html_text(event.get("revision"))}</strong> {html_text(event.get("summary"))} · {html_text(event.get("timestamp"))}'
        f'<br><span class="muted">操作：{html_text([op.get("id", op.get("op", "")) for op in event.get("operations") or []])}</span></li>'
        for event in progress["recent_changes"]
    ) or '<li class="muted">尚无状态变更记录</li>'
    archive_rows = "".join(
        f'<li><strong>{html_text(archive.get("session_id"))}</strong> · {html_text(archive.get("message_count"))} 条公开消息'
        f'<br><span class="muted">首条：{html_text((archive.get("first_message") or {}).get("content"))}</span>'
        f'<br><span class="muted">末条：{html_text((archive.get("last_message") or {}).get("content"))}</span></li>'
        for archive in progress["session_archives"][:5]
    ) or '<li class="muted">当前没有已导入的 session 归档；本页根据 state.json 的状态变更生成工作摘要。</li>'
    completed_rows = html_bullets([f'{item.get("id")} · {item.get("title")}' for item in progress["completed_work"]], "本轮没有标记为完成的工作包")
    in_progress_rows = html_bullets([f'{item.get("id")} · {item.get("title")} · {item.get("status")}' for item in progress["in_progress_work"]], "没有活动工作包")
    pending_rows = html_bullets([f'{item.get("id")} · {item.get("title")} · {item.get("readiness")}' for item in progress["pending_requirements"]], "没有待处理需求")
    action_cards = "".join(render_action_card(action) for action in progress["next_actions"]) or '<p class="muted">当前没有活动下一步行动。</p>'
    daily_rows = "".join(render_daily_progress_row(day) for day in progress["daily_progress"]) or '<p class="muted">当前没有可按天汇总的状态变更或 session 活动。</p>'

    page = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_text(project.get("name"))} · 项目进度概览</title>
<style>
:root {{ color-scheme: light; --ink:#172033; --muted:#667085; --line:#e5e7eb; --panel:#fff; --bg:#f5f7fb; --accent:#315efb; --warn:#b54708; --good:#13795b; }}
* {{ box-sizing:border-box; }} body {{ margin:0; font:14px/1.6 Inter,"Segoe UI","Microsoft YaHei",sans-serif; color:var(--ink); background:var(--bg); }}
a {{ color:var(--accent); text-decoration:none; }} a:hover {{ text-decoration:underline; }} .wrap {{ max-width:1440px; margin:0 auto; padding:28px; }}
header {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-start; margin-bottom:22px; }} h1,h2,h3,h4 {{ margin:0 0 8px; line-height:1.25; }} h1 {{ font-size:30px; }} h2 {{ margin-top:28px; font-size:20px; }} h3 {{ font-size:17px; }} h4 {{ font-size:14px; }}
.muted {{ color:var(--muted); }} .stamp {{ color:var(--muted); font-size:12px; text-align:right; }} .panel,.feature-card {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; box-shadow:0 3px 12px #1b2a4510; }}
.panel {{ padding:18px; }} .hero {{ display:grid; grid-template-columns:1.5fr 1fr; gap:16px; }} .overview {{ white-space:pre-wrap; }} .scope {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
.metrics {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:16px 0; }} .metric {{ padding:14px; border:1px solid var(--line); border-radius:12px; background:#fbfcff; }} .metric strong {{ display:block; font-size:25px; }} .metric span {{ color:var(--muted); }}
.attention {{ border-left:4px solid var(--warn); }} .action-board {{ border:2px solid var(--warn); background:#fffaf2; }} .action-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:14px; }} .action-card {{ background:#fff; border:1px solid #fed7aa; border-left:5px solid var(--warn); border-radius:12px; padding:14px; }} .action-card.in-progress {{ border-left-color:var(--accent); }} .action-card.done {{ border-left-color:var(--good); }} .action-heading {{ display:flex; justify-content:space-between; gap:10px; align-items:center; }} .action-card h3 {{ margin-top:10px; }} .action-meta {{ display:flex; flex-wrap:wrap; gap:8px 16px; color:var(--muted); font-size:12px; margin-bottom:10px; }} .action-meta strong {{ color:var(--ink); margin-right:4px; }} .action-acceptance {{ margin-top:10px; }} .missing {{ margin:10px 0 0; padding:8px 10px; border-radius:8px; color:var(--warn); background:#fff7ed; font-size:12px; }} .table-wrap {{ overflow:auto; }} table {{ width:100%; border-collapse:collapse; min-width:760px; }} th,td {{ padding:10px 9px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }} th {{ color:var(--muted); font-weight:600; font-size:12px; }}
.badge,.state {{ display:inline-block; border-radius:999px; padding:2px 8px; font-size:12px; border:1px solid var(--line); background:#f8fafc; white-space:nowrap; }} .state.ready,.state.ready-for-build,.state.ready-for-acceptance,.confirmed {{ color:var(--good); background:#ecfdf3; border-color:#bbf7d0; }} .state.confirmed-for-extension,.confirmed_for_extension {{ color:#1d4ed8; background:#eff6ff; border-color:#bfdbfe; }} .state.attention,.pending-confirmation,.not-ready,.ready-for-design {{ color:var(--warn); background:#fff7ed; border-color:#fed7aa; }}
.feature-list {{ display:grid; gap:14px; margin-top:14px; }} .feature-card {{ padding:18px; }} .feature-heading {{ display:flex; justify-content:space-between; gap:12px; align-items:center; }} .meta {{ display:flex; flex-wrap:wrap; gap:8px 16px; color:var(--muted); font-size:12px; margin-bottom:10px; }} .two-col {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; }} .clamp-2 {{ display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; overflow:hidden; }}
details {{ margin-top:14px; border-top:1px solid var(--line); padding-top:12px; }} summary {{ cursor:pointer; font-weight:650; }} .feature-definition {{ margin-top:8px; }} .surface {{ margin:10px 0; padding:12px; border:1px solid var(--line); border-radius:10px; background:#fbfcff; }} ul {{ margin:6px 0 0 20px; padding:0; }} li {{ margin:3px 0; }} .toolbar {{ display:flex; gap:10px; margin:12px 0; }} input {{ flex:1; padding:10px 12px; border:1px solid var(--line); border-radius:9px; font:inherit; }} .daily-board {{ border-left:4px solid var(--accent); }} .daily-row {{ margin:8px 0; padding:8px 0; border-bottom:1px solid var(--line); }} .daily-row summary {{ font-weight:600; }} .daily-detail {{ max-height:420px; overflow:auto; padding:8px 4px 2px 16px; }} .daily-synthesis {{ margin:0 0 12px; padding:12px 14px; background:#e8f1ff; border:1px solid #b8d2ff; border-radius:8px; line-height:1.65; }} .daily-summary {{ margin:0 0 12px; padding:10px 12px; background:#f5f8fc; border-radius:8px; color:#405064; }} .daily-product-changes {{ max-height:300px; overflow:auto; padding:8px 10px; background:#fff; border:1px solid var(--line); border-radius:8px; }} .daily-changes {{ max-height:300px; overflow:auto; padding:8px 10px; background:#fbfcff; border:1px solid var(--line); border-radius:8px; }} .daily-group {{ margin:0 0 12px; padding:8px 10px; border-left:3px solid var(--accent); background:#fff; }} .daily-group h4 {{ margin-bottom:4px; }}
footer {{ margin-top:28px; color:var(--muted); font-size:12px; }} @media(max-width:900px) {{ .hero,.two-col,.scope {{ grid-template-columns:1fr; }} .metrics {{ grid-template-columns:repeat(2,1fr); }} header {{ display:block; }} .stamp {{ text-align:left; margin-top:8px; }} }} @media print {{ body {{ background:#fff; }} .panel,.feature-card {{ box-shadow:none; }} .toolbar {{ display:none; }} }}
</style></head><body><main class="wrap">
<header><div><h1>{html_text(project.get("name"))}</h1><p class="muted">项目进度概览 · 静态页面 · 由 project-manager 自动生成</p></div><div class="stamp">revision {html_text(state.get('revision'))}<br>生成于 {html_text(generated)}<br>来源：state.json</div></header>
<section class="hero"><div class="panel"><h2>产品概述</h2><p class="overview">{html_text(project.get("goal"))}</p><div class="scope"><div><h4>预期结果</h4>{html_bullets(project.get("success_outcomes"))}</div><div><h4>当前范围</h4>{html_bullets(project.get("in_scope"))}</div></div></div><div class="panel attention"><h2>当前需要关注</h2>{undecided_html}<p class="muted">此列表优先提示需求未确认、仍有未决问题或尚未完成完整设计契约的功能。</p></div></section>
<section class="metrics"><div class="metric"><strong>{len(requirements)}</strong><span>功能需求</span></div><div class="metric"><strong>{confirmed}</strong><span>需求已确认（含 AI）</span></div><div class="metric"><strong>{ai_unverified}</strong><span>AI确认待人工核验</span></div><div class="metric"><strong>{build_ready}</strong><span>已达到构建门禁</span></div><div class="metric"><strong>{extension_ready}</strong><span>确认待拓展</span></div><div class="metric"><strong>{len(undecided)}</strong><span>仍需敲定</span></div></section>
<section class="panel action-board"><h2>下一步行动 · PM Action Board</h2><p class="muted">每项行动都必须明确负责人、截止时间和可验收结果；显示“待指定 / 待设置 / 待补充”的项目需要先补齐治理信息。</p><div class="action-grid">{action_cards}</div></section>
<section class="panel daily-board"><h2>每日进度汇总</h2><p class="muted">按项目时区从当前目录的结构化状态变更和已导入公开 session 归档聚合；点击日期展开当天摘要、具体产品改动和进度提升。当天详情按需求/功能、开发工作包、产品决策、证据来源、里程碑、风险评审和 Session 分类，不按时间流水展示。摘要同时给出当前累计进度，区分“今天做了什么”和“项目现在走到哪里”。</p>{daily_rows}</section>
<section class="panel"><h2>本轮 / Session 工作汇报</h2><div class="two-col"><div><h3>已完成</h3>{completed_rows}<h3>进行中与下一步</h3>{in_progress_rows}</div><div><h3>待敲定或阻塞</h3>{pending_rows}<h3>最近状态变化</h3><ul>{change_rows}</ul></div></div><details><summary>Session 归档摘要</summary><ul>{archive_rows}</ul></details><p class="muted">摘要来源：当前 revision 的状态变更、工作包、需求门禁、风险和已导入的公开 session 归档。它不把隐藏推理或工具输出写入项目状态。</p></section>
<section class="panel"><h2>功能列表与敲定状态</h2><div class="toolbar"><input id="featureFilter" placeholder="搜索功能、状态、行为或需求 ID" aria-label="搜索功能"></div><div class="table-wrap"><table><thead><tr><th>ID</th><th>功能</th><th>需求状态</th><th>就绪度</th><th>当前判断</th><th>优先级</th></tr></thead><tbody id="featureRows">{"".join(feature_rows) or '<tr><td colspan="6" class="muted">尚无正式需求</td></tr>'}</tbody></table></div></section>
<section class="feature-list">{"".join(feature_cards) or '<div class="panel muted">尚无功能详情</div>'}</section>
<section class="panel"><h2>开发工作与项目运行情况</h2><div class="table-wrap"><table><thead><tr><th>ID</th><th>工作包</th><th>状态</th><th>关联功能</th><th>日期</th></tr></thead><tbody>{work_rows}</tbody></table></div><div class="two-col"><div><h3>里程碑</h3><ul>{milestone_rows}</ul></div><div><h3>风险与评审</h3><ul>{risk_rows}{review_rows}</ul></div></div></section>
{lifecycle_html}
<footer>本页不是实时服务；它是由 project-manager 根据当前项目状态生成的静态概览。运行 `pm.py apply`、`init` 或 `index` 后会自动刷新。</footer>
</main><script>const input=document.getElementById('featureFilter'); input?.addEventListener('input',()=>{{const q=input.value.trim().toLowerCase(); document.querySelectorAll('#featureRows tr[data-search],.feature-card[data-search]').forEach(el=>{{el.hidden=q && !el.dataset.search.toLowerCase().includes(q);}});}});</script></body></html>'''
    atomic_text(docs / PROJECT_OVERVIEW_NAME, page)


def render_views(root: Path, state: dict[str, Any]) -> None:
    docs = root / DOCS_REL
    p = state["project"]
    deadlines = deadline_rows(state)
    index = [
        "# 项目经理索引", "", "> 由 `pm.py` 从 `state.json` 生成，请勿手工编辑。", "",
        "## 当前状态", "",
        f"- 项目：{md(p.get('name'))}（`{md(p.get('id'))}`）",
        f"- 阶段：`{md(p.get('stage'))}`",
        f"- 当前交互模式：`{md(p.get('active_mode'))}`",
        f"- 目标：{md(p.get('goal'))}",
        f"- 状态 revision：`{state['revision']}`", "",
        "## 文档", "",
        "- [需求基线](REQUIREMENTS.md)",
        "- [开发衔接与缺口处理](DEVELOPMENT-GUIDE.md)",
        "- [阶段验收、逐项覆盖与交接回执](DELIVERY-WORKFLOW.md)",
        "- [待拓展模块索引](EXTENSION-MODULES.md)",
        "- [模块交互手册](MODULE-INTERACTIONS.md)",
        f"- [项目进度概览]({PROJECT_OVERVIEW_NAME})",
        "- [交付与日期](DELIVERY.md)",
        "- [决定与产品评审](DECISIONS.md)",
        "- [结构化状态](state.json)", "",
        "## 近期日期", "",
        "| ID | 事项 | 日期 | 提示 |", "|---|---|---|---|",
    ]
    index.extend(f"| {r['id']} | {md(r['title'])} | {md(r['deadline'])} | {r['alert']} |" for r in deadlines)
    if not deadlines:
        index.append("| — | 尚未商定 | — | 立项时主动协商 |")

    req_lines = [
        "# 需求基线", "", "> 由 `pm.py` 生成。正式修改通过结构化变更完成。", "",
        "## 原始诉求覆盖", "", "| ID | 类型 | 原子诉求 | 归类 | 去向 | 来源 |", "|---|---|---|---|---|---|",
    ]
    for item in state["intake_items"]:
        req_lines.append(
            f"| {item['id']} | {md(item.get('kind'))} | {md(item.get('title'))} | {md(item.get('disposition'))} | "
            f"{md(item.get('target_ids'))} | {md(item.get('source_refs'))} |"
        )
    if not state["intake_items"]:
        req_lines.append("| — | — | 尚未提取原子诉求 | — | — | — |")
    req_lines.extend([
        "", "## 正式需求", "",
        "| ID | 版本 | 需求 | 状态 | 就绪度 | 优先级 | 场景 | 来源 |", "|---|---:|---|---|---|---|---|---|",
    ])
    for req in state["requirements"]:
        req_lines.append(
            f"| {req['id']} | {req.get('version', 1)} | {md(req.get('title'))} | {md(req.get('status'))} | "
            f"{md(req.get('readiness'))} | {md(req.get('priority'))} | {md(req.get('scenario_id'))} | {md(req.get('source_refs'))} |"
        )
    if not state["requirements"]:
        req_lines.append("| — | — | 尚无需求 | — | — | — | — | — |")
    req_lines.extend(["", "## 详细契约", ""])
    for req in state["requirements"]:
        req_lines.extend([
            f"### {req['id']} {req.get('title', '')}", "",
            f"- 用户价值：{md(req.get('user_value'))}",
            f"- 前提与触发：{md(req.get('trigger'))}",
            f"- 可观察行为：{md(req.get('behavior'))}",
            f"- 结果：{md(req.get('outcome'))}",
            f"- 规则与边界：{md(req.get('boundaries'))}",
            f"- 验收条件：{md(req.get('acceptance_criteria'))}", "",
        ])

    delivery = [
        "# 交付与日期", "", "> 由 `pm.py` 生成。", "",
        "## 工作包", "", "| ID | 工作包 | 状态 | 负责人 | 关联需求 | 日期 |", "|---|---|---|---|---|---|",
    ]
    for wp in state["work_packages"]:
        delivery.append(
            f"| {wp['id']} | {md(wp.get('title'))} | {md(wp.get('status'))} | {md(wp.get('owner'))} | "
            f"{md(wp.get('requirement_ids'))} | {md(wp.get('due_at') or wp.get('target_date'))} |"
        )
    if not state["work_packages"]:
        delivery.append("| — | 尚无工作包 | — | — | — | — |")
    delivery.extend(["", "## 里程碑", "", "| ID | 里程碑 | 类型 | 状态 | 日期 |", "|---|---|---|---|---|"])
    for item in state["milestones"]:
        delivery.append(f"| {item['id']} | {md(item.get('title'))} | {md(item.get('deadline_type'))} | {md(item.get('status'))} | {md(item.get('due_at') or item.get('target_date'))} |")
    if not state["milestones"]:
        delivery.append("| — | 尚未商定 | — | — | — |")
    delivery.extend(["", "## 风险", "", "| ID | 风险 | 状态 | 影响 | 应对 |", "|---|---|---|---|---|"])
    for risk in state["risks"]:
        delivery.append(f"| {risk['id']} | {md(risk.get('title'))} | {md(risk.get('status'))} | {md(risk.get('impact'))} | {md(risk.get('response'))} |")
    if not state["risks"]:
        delivery.append("| — | 无已记录风险 | — | — | — |")

    decisions = [
        "# 决定与产品评审", "", "> 由 `pm.py` 生成。", "",
        "## 决定", "", "| ID | 议题 | 结论 | 理由 | 来源 |", "|---|---|---|---|---|",
    ]
    for item in state["decisions"]:
        decisions.append(f"| {item['id']} | {md(item.get('title'))} | {md(item.get('decision'))} | {md(item.get('reason'))} | {md(item.get('source_refs'))} |")
    if not state["decisions"]:
        decisions.append("| — | 尚无决定 | — | — | — |")
    decisions.extend(["", "## 产品评审", "", "| ID | 类型 | 问题 | 影响 | 建议 | 状态 |", "|---|---|---|---|---|---|"])
    for item in state["reviews"]:
        decisions.append(f"| {item['id']} | {md(item.get('kind'))} | {md(item.get('title'))} | {md(item.get('impact'))} | {md(item.get('recommendation'))} | {md(item.get('status'))} |")
    if not state["reviews"]:
        decisions.append("| — | — | 尚无评审项 | — | — | — |")

    extension_lines = [
        "# 待拓展模块索引", "",
        "> 由 `pm.py` 根据需求的 `confirmed_for_extension` 状态和拓展模块字段自动生成；确认入口仍在当前对话。", "",
        "| 模块 | 需求 | 状态 | 当前保留的接口/设计 | 工作包 |",
        "|---|---|---|---|---|",
    ]
    extension_requirements = [
        req for req in state["requirements"]
        if req.get("readiness") == "confirmed_for_extension"
        or req.get("extension_module_id")
        or req.get("related_extension_module_ids")
    ]
    for req in extension_requirements:
        module_ids = req.get("extension_module_id") or req.get("related_extension_module_ids") or "未分配"
        module_title = req.get("extension_module_title") or req.get("related_extension_module_title") or "关联拓展模块"
        contract = req.get("extension_contract") or req.get("extension_contract_summary") or req.get("extension_integration_note") or "待补充"
        contract_text = "；".join(f"{key}: {value}" for key, value in contract.items()) if isinstance(contract, dict) else compact_text(contract, 360)
        work_package_ids = req.get("work_package_ids") or req.get("work_package_id") or "待建立"
        status = req.get("readiness") if req.get("extension_module_id") or req.get("readiness") == "confirmed_for_extension" else f"关联接入点（{req.get('readiness')}）"
        extension_lines.append(
            f"| {md(module_ids)} · {md(module_title)} | {md(req.get('id'))} {md(req.get('title'))} | "
            f"{md(status)} | {md(compact_text(contract_text, 360))} | {md(work_package_ids)} |"
        )
    if not extension_requirements:
        extension_lines.append("| — | 尚无确认待拓展需求 | — | — | — |")

    interaction_lines = [
        "# 模块交互手册", "",
        "> 由 `pm.py` 根据需求的模块归属、关联需求和交互契约自动生成；耦合模块必须先补齐接口规范。", "",
        "| 源模块 | 目标模块/需求 | 耦合级别 | 交互与数据边界 | 接口规范状态 |",
        "|---|---|---|---|---|",
    ]
    interaction_count = 0
    requirements_by_id = {req.get("id"): req for req in state["requirements"]}
    for req in state["requirements"]:
        source_module = req.get("module_id") or req.get("module_title")
        related_ids = req.get("related_requirement_ids") or []
        contracts = req.get("interaction_contracts") or []
        if isinstance(contracts, dict):
            contracts = [contracts]
        if not source_module and not related_ids and not contracts:
            continue
        source_label = f"{source_module} · {req.get('module_title') or req.get('title')}" if source_module else req.get("id")
        targets = related_ids or ["待补充目标"]
        for target_id in targets:
            target = requirements_by_id.get(target_id)
            target_label = f"{target_id} · {target.get('title')}" if target else str(target_id)
            contract_text = "；".join(
                f"{item.get('name') or item.get('interface') or '契约'}: {item.get('behavior') or item.get('contract') or item}"
                if isinstance(item, dict) else str(item) for item in contracts
            ) or req.get("extension_integration_note") or req.get("interaction_note") or "待补充"
            interface_spec = req.get("interface_spec") or {}
            if isinstance(interface_spec, dict):
                interface_status = interface_spec.get("status") or ("已定义" if interface_spec else "待补充")
            else:
                interface_status = str(interface_spec)
            interaction_lines.append(
                f"| {md(source_label)} | {md(target_label)} | {md(req.get('coupling_level') or '待判断')} | "
                f"{md(compact_text(contract_text, 360))} | {md(interface_status)} |"
            )
            interaction_count += 1
    if not interaction_count:
        interaction_lines.append("| — | 尚无显式模块交互记录 | — | 对共享数据、状态、服务或跨需求行为进行耦合审查后再补充 | — |")

    atomic_text(docs / "INDEX.md", "\n".join(index) + "\n")
    atomic_text(docs / "EXTENSION-MODULES.md", "\n".join(extension_lines) + "\n")
    atomic_text(docs / "MODULE-INTERACTIONS.md", "\n".join(interaction_lines) + "\n")
    atomic_text(docs / "REQUIREMENTS.md", "\n".join(req_lines) + "\n")
    atomic_text(docs / "DELIVERY.md", "\n".join(delivery) + "\n")
    atomic_text(docs / "DECISIONS.md", "\n".join(decisions) + "\n")
    atomic_text(docs / "DEVELOPMENT-GUIDE.md", render_development_guide(delivery_diagnostics(root, state)))
    if workflow.config(state):
        atomic_text(docs / "DELIVERY-WORKFLOW.md", workflow.markdown(workflow.report(root, state)))
    render_project_overview(root, state)


def build_search_index(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for collection in COLLECTIONS:
        for record in state[collection]:
            entries.append({
                "id": record["id"],
                "kind": collection,
                "text": json.dumps(record, ensure_ascii=False, sort_keys=True),
            })
    data = {"schema_version": 1, "state_revision": state["revision"], "generated_at": now_iso(), "entries": entries}
    atomic_json(root / LOCAL_REL / "search-index.json", data)
    return data


def redact(text: str) -> tuple[str, bool]:
    patterns = [
        (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"), r"\1[REDACTED]"),
        (re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+"), r"\1[REDACTED]"),
        (re.compile(r"\b1[3-9]\d{9}\b"), "[REDACTED_PHONE]"),
        (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    ]
    changed = False
    for pattern, repl in patterns:
        updated = pattern.sub(repl, text)
        changed = changed or updated != text
        text = updated
    return text, changed


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, dict) and item.get("type") in {"input_text", "output_text", "text"}:
            value = item.get("text") or item.get("content")
            if isinstance(value, str):
                chunks.append(value)
    return "\n".join(chunks)


def extract_public_message(obj: dict[str, Any]) -> tuple[str, str, str | None] | None:
    candidate = obj
    if obj.get("type") == "response_item" and isinstance(obj.get("payload"), dict):
        candidate = obj["payload"]
    elif isinstance(obj.get("payload"), dict) and obj["payload"].get("type") == "message":
        candidate = obj["payload"]
    if candidate.get("type") not in {None, "message"}:
        return None
    role = candidate.get("role")
    if role not in {"user", "assistant"}:
        return None
    text = content_text(candidate.get("content"))
    if not text.strip():
        return None
    timestamp = obj.get("timestamp") or candidate.get("timestamp")
    return role, text, timestamp if isinstance(timestamp, str) else None


def cmd_inspect(args: argparse.Namespace) -> dict[str, Any]:
    root = args.project_root
    docs = root / DOCS_REL
    state_exists = (docs / STATE_NAME).exists()
    pta = (root / ".project-to-act" / "PROJECT_CONFIG.json").exists()
    result: dict[str, Any] = {
        "project_root": str(root),
        "project_to_act": "configured" if pta else "missing",
        "project_manager": "initialized" if state_exists else "uninitialized",
        "state_path": str(docs / STATE_NAME),
    }
    if state_exists:
        state = load_state(root)
        result.update({"revision": state["revision"], "project": state["project"]})
    return result


def cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    root = args.project_root
    ensure_pta(root)
    path = state_path(root)
    if path.exists():
        return {"status": "exists", "state_path": str(path)}
    planned = [str(path), str(root / DOCS_REL / "INDEX.md"), str(root / LOCAL_REL), str(root / ".gitignore")]
    if args.dry_run:
        return {"status": "dry_run", "planned": planned}
    state = default_state(args.project_id, args.name, args.timezone)
    atomic_json(path, state)
    render_views(root, state)
    build_search_index(root, state)
    changed_ignore = ensure_gitignore(root)
    return {"status": "initialized", "state_path": str(path), "gitignore_updated": changed_ignore}


def cmd_apply(args: argparse.Namespace) -> dict[str, Any]:
    with project_lock(args.project_root):
        state = load_state(args.project_root)
        changes = read_json(args.changes)
        expected = changes.get("expected_revision")
        if expected != state.get("revision"):
            raise PMError(f"stale state revision: expected {expected}, current {state.get('revision')}")
        operations = changes.get("operations")
        if not isinstance(operations, list) or not operations:
            raise PMError("changes requires non-empty operations list")
        updated = copy.deepcopy(state)
        stamp = now_iso()
        results = [apply_operation(updated, op, stamp, args.project_root) for op in operations]
        errors = validate_references(updated)
        if errors:
            raise PMError(f"reference validation failed: {json.dumps(errors, ensure_ascii=False)}")
        meaningful = [item for item in results if item["op"] != "noop"]
        if not meaningful:
            return {"status": "unchanged", "revision": state["revision"], "results": results}
        updated["revision"] = state["revision"] + 1
        updated["project"]["updated_at"] = stamp
        updated["change_events"].append({
            "revision": updated["revision"],
            "timestamp": stamp,
            "summary": changes.get("summary", "structured update"),
            "source_ref": changes.get("source_ref"),
            "operations": meaningful,
        })
        if args.dry_run:
            return {"status": "dry_run", "revision": updated["revision"], "results": results}
        atomic_json(state_path(args.project_root), updated)
        render_views(args.project_root, updated)
        build_search_index(args.project_root, updated)
        return {"status": "applied", "revision": updated["revision"], "results": results}


def cmd_index(args: argparse.Namespace) -> dict[str, Any]:
    with project_lock(args.project_root):
        state = load_state(args.project_root)
        render_views(args.project_root, state)
        index = build_search_index(args.project_root, state)
        return {"status": "rebuilt", "revision": state["revision"], "entries": len(index["entries"])}


def cmd_search(args: argparse.Namespace) -> dict[str, Any]:
    state = load_state(args.project_root)
    path = args.project_root / LOCAL_REL / "search-index.json"
    if not path.exists() or read_json(path).get("state_revision") != state["revision"]:
        index = build_search_index(args.project_root, state)
    else:
        index = read_json(path)
    query = args.query.casefold()
    results = [
        {"id": item["id"], "kind": item["kind"], "text": item["text"][: args.max_chars]}
        for item in index["entries"] if query in item["text"].casefold()
    ]
    if args.include_conversations:
        conv_dir = args.project_root / LOCAL_REL / "conversations"
        for path in sorted(conv_dir.glob("*.jsonl")) if conv_dir.exists() else []:
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if query in line.casefold():
                    results.append({"id": f"{path.stem}:{line_no}", "kind": "conversation", "text": line[: args.max_chars]})
    return {"query": args.query, "count": len(results), "results": results[: args.limit]}


def cmd_check(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    state = load_state(args.project_root)
    errors = validate_references(state)
    warnings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for collection in COLLECTIONS:
        for record in state[collection]:
            record_id = record.get("id")
            if record_id in seen:
                errors.append({"code": "DUPLICATE_GLOBAL_ID", "record": record_id})
            seen.add(record_id)
            try:
                validate_record(collection, record)
            except PMError as exc:
                errors.append({"code": "INVALID_RECORD", "record": record_id, "message": str(exc)})
    for row in deadline_rows(state):
        if row["alert"] == "invalid":
            errors.append({"code": "INVALID_DEADLINE", **row})
        elif row["alert"] in {"overdue", "due_soon"}:
            warnings.append({"code": row["alert"].upper(), **row})
    diagnostic = delivery_diagnostics(args.project_root, state, getattr(args, "requirement", None))
    stage = getattr(args, "stage", "all")
    lifecycle = workflow.report(args.project_root, state, getattr(args, "requirement", None))
    use_lifecycle = bool(workflow.config(state)) or stage in {"specification", "implementation", "verification", "acceptance"}
    delivery_warnings = [item for item in diagnostic["findings"] if stage in {"all", item["stage"]}
                         or (stage == "engineering" and item["stage"] == "product")]
    warnings.extend({key: item[key] for key in ("id", "code", "record", "stage", "reason", "action")}
                    for item in delivery_warnings)
    phase_findings = [f for f in lifecycle["findings"] if stage in {"all", f["stage"]}] if use_lifecycle else []
    warnings.extend({"code": "STAGE_NOT_PASSED", **f} for f in phase_findings)
    if use_lifecycle and not lifecycle["configured"]:
        warnings.append({"code": "DELIVERY_WORKFLOW_NOT_CONFIGURED", "action": "使用 configure_delivery 登记阶段及本期范围；保留旧证据，不自动判定通过。"})
    for item in state["intake_items"]:
        if item.get("disposition", "unclassified") == "unclassified":
            warnings.append({"code": "UNCLASSIFIED_INTAKE_ITEM", "record": item["id"]})
    result = {
        "valid": not errors,
        "strict_valid": not errors and not warnings,
        "revision": state["revision"],
        "errors": errors,
        "warnings": warnings,
        "stage": stage,
        "structure_valid": not errors,
        "stage_results": {
            "structure": "passed" if not errors else "failed",
            **{name: "gaps_found" if any(f["stage"] == name for f in diagnostic["findings"]) else "no_structural_gaps_detected"
               for name in ("product", "engineering", "planning")},
            "verification": "not_assessed", "acceptance": "not_assessed",
        },
        "delivery_diagnostics": diagnostic,
        "delivery_workflow": lifecycle,
    }
    if use_lifecycle:
        result["stage_results"].update(lifecycle["stage_results"])
    phase_ok = not (use_lifecycle and stage in workflow.STAGES and (
        not lifecycle["configured"] or lifecycle["stage_results"][stage] != "passed"))
    return result, 0 if (not errors and phase_ok and (not args.strict or not warnings)) else 1


def cmd_workflow(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    state = load_state(args.project_root)
    report = workflow.report(args.project_root, state, getattr(args, "requirement", None))
    if getattr(args, "write_report", False):
        with project_lock(args.project_root):
            state = load_state(args.project_root)
            full = workflow.report(args.project_root, state)
            path = args.project_root / DOCS_REL / "DELIVERY-WORKFLOW.md"
            atomic_text(path, workflow.markdown(full))
            report = workflow.report(args.project_root, state, getattr(args, "requirement", None))
            report["report_path"] = str(path)
    stages = workflow.STAGES if args.stage == "all" else (args.stage,)
    passed = report["configured"] and all(report["stage_results"][s] == "passed" for s in stages)
    return report, 1 if args.strict and not passed else 0


def cmd_resume(args: argparse.Namespace) -> dict[str, Any]:
    state = load_state(args.project_root)
    active_reqs = [r for r in state["requirements"] if r.get("status") not in {"rejected", "superseded"}]
    pending = [r for r in active_reqs if r.get("status") in {"clarification_needed", "pending_confirmation"}]
    decision_questions = []
    legacy_open_questions = []
    for requirement in active_reqs:
        for question in requirement.get("decision_questions") or []:
            if question.get("status") in {"open", "deferred"}:
                decision_questions.append({
                    "requirement_id": requirement["id"],
                    "requirement_version": requirement.get("version"),
                    **{key: question.get(key) for key in (
                        "id", "question", "recommendation", "alternatives", "status", "source_ref", "asked_at"
                    )},
                })
        if requirement.get("open_questions"):
            legacy_open_questions.append({
                "requirement_id": requirement["id"],
                "requirement_version": requirement.get("version"),
                "questions": requirement["open_questions"],
            })
    active_work = [w for w in state["work_packages"] if w.get("status") not in {"done", "cancelled"}]
    open_risks = [r for r in state["risks"] if r.get("status") not in {"closed"}]
    return {
        "project": state["project"],
        "revision": state["revision"],
        "requirements": {"active": len(active_reqs), "confirmed": sum(r.get("status") in {"confirmed", "ai_confirmed_unverified"} for r in active_reqs), "ai_confirmed_unverified": sum(r.get("status") == "ai_confirmed_unverified" for r in active_reqs)},
        "pending_confirmation": [{"id": r["id"], "title": r.get("title"), "status": r.get("status")} for r in pending[:10]],
        "decision_questions": decision_questions[:50],
        "legacy_open_questions": legacy_open_questions[:50],
        "active_work": [{"id": w["id"], "title": w.get("title"), "status": w.get("status")} for w in active_work[:10]],
        "open_risks": [{"id": r["id"], "title": r.get("title"), "status": r.get("status")} for r in open_risks[:10]],
        "deadline_alerts": [r for r in deadline_rows(state, args.hours) if r["alert"] != "scheduled"],
        "last_changes": state.get("change_events", [])[-5:],
        "progress_summary": build_progress_summary(args.project_root, state),
        "delivery_workflow": workflow.report(args.project_root, state),
    }


def cmd_import_session(args: argparse.Namespace) -> dict[str, Any]:
    with project_lock(args.project_root):
        return _cmd_import_session_locked(args)


def _cmd_import_session_locked(args: argparse.Namespace) -> dict[str, Any]:
    state = load_state(args.project_root)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", args.session_id):
        raise PMError("session id must be 1-128 safe filename characters and start with a letter or digit")
    if state["project"]["id"] != args.project_id:
        raise PMError(f"project id mismatch: state is {state['project']['id']}, argument is {args.project_id}")
    source = args.session_file.resolve()
    if not source.is_file():
        raise PMError(f"session file not found: {source}")
    output = args.project_root / LOCAL_REL / "conversations" / f"{args.session_id}.jsonl"
    existing: set[str] = set()
    existing_lines: list[str] = []
    if output.exists():
        existing_lines = output.read_text(encoding="utf-8").splitlines()
        for line in existing_lines:
            try:
                existing.add(json.loads(line)["fingerprint"])
            except (json.JSONDecodeError, KeyError, TypeError):
                raise PMError(f"invalid existing conversation archive: {output}")
    added: list[dict[str, Any]] = []
    redacted_count = 0
    with source.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            message = extract_public_message(obj)
            if message is None:
                continue
            role, raw_text, timestamp = message
            clean, was_redacted = redact(raw_text)
            fingerprint = hashlib.sha256(
                f"{args.session_id}\0{line_no}\0{role}\0{clean}".encode("utf-8")
            ).hexdigest()
            if fingerprint in existing:
                continue
            entry = {
                "fingerprint": fingerprint,
                "project_id": args.project_id,
                "session_id": args.session_id,
                "source_line": line_no,
                "timestamp": timestamp,
                "role": role,
                "content": clean,
                "content_sha256": hashlib.sha256(clean.encode("utf-8")).hexdigest(),
                "redacted": was_redacted,
            }
            added.append(entry)
            existing.add(fingerprint)
            redacted_count += int(was_redacted)
    if args.dry_run:
        return {"status": "dry_run", "messages_found": len(added), "redacted": redacted_count, "output": str(output)}
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = existing_lines + [json.dumps(item, ensure_ascii=False) for item in added]
    atomic_text(output, "\n".join(lines) + ("\n" if lines else ""))
    source_id = "SRC-SESSION-" + hashlib.sha256(args.session_id.encode("utf-8")).hexdigest()[:12].upper()
    source_record = {
        "id": source_id,
        "kind": "codex_session",
        "title": f"Codex session {args.session_id}",
        "session_id": args.session_id,
        "archive_ref": str(output.relative_to(args.project_root)).replace("\\", "/"),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "message_count": len(lines),
        "redacted_messages": redacted_count,
        "completeness": "public_messages_extracted",
    }
    existing_source = collection_map(state, "sources").get(source_id)
    stamp = now_iso()
    if existing_source:
        existing_source.update({k: v for k, v in source_record.items() if k != "id"})
        existing_source["updated_at"] = stamp
    else:
        source_record.update({"version": 1, "created_at": stamp, "updated_at": stamp})
        state["sources"].append(source_record)
    if added or not existing_source:
        state["revision"] += 1
        state["project"]["updated_at"] = stamp
        state["change_events"].append({
            "revision": state["revision"], "timestamp": stamp, "summary": f"import session {args.session_id}",
            "source_ref": source_id, "operations": [{"op": "import_session", "messages_added": len(added)}],
        })
        atomic_json(state_path(args.project_root), state)
        render_views(args.project_root, state)
        build_search_index(args.project_root, state)
    return {"status": "imported", "messages_added": len(added), "redacted": redacted_count, "source_id": source_id, "output": str(output), "revision": state["revision"]}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Project Manager deterministic state helper")
    p.add_argument("--project-root", type=Path, required=True)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("inspect")
    init = sub.add_parser("init")
    init.add_argument("--project-id", required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--timezone", default="Asia/Shanghai")
    init.add_argument("--dry-run", action="store_true")
    apply_cmd = sub.add_parser("apply")
    apply_cmd.add_argument("--changes", type=Path, required=True)
    apply_cmd.add_argument("--dry-run", action="store_true")
    sub.add_parser("index")
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--include-conversations", action="store_true")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--max-chars", type=int, default=500)
    check = sub.add_parser("check")
    check.add_argument("--strict", action="store_true")
    check.add_argument("--stage", choices=("all", "structure", "product", "engineering", "planning", "specification", "implementation", "verification", "acceptance"), default="all")
    check.add_argument("--requirement", action="append", help="Scope delivery diagnostics to a REQ; repeatable. Structure checks remain project-wide.")
    doctor = sub.add_parser("doctor", help="Diagnose handoff gaps and produce executable recovery guidance; never changes REQ states.")
    doctor.add_argument("--requirement", action="append")
    doctor.add_argument("--write-report", action="store_true", help="Refresh the full generated DEVELOPMENT-GUIDE.md.")
    flow = sub.add_parser("workflow", help="Inspect stage receipts, exact AC coverage and remaining work; no automatic passing.")
    flow.add_argument("--requirement", action="append")
    flow.add_argument("--stage", choices=("all", *workflow.STAGES), default="all")
    flow.add_argument("--strict", action="store_true")
    flow.add_argument("--write-report", action="store_true")
    resume = sub.add_parser("resume")
    resume.add_argument("--hours", type=int, default=72)
    imp = sub.add_parser("import-session")
    imp.add_argument("--project-id", required=True)
    imp.add_argument("--session-id", required=True)
    imp.add_argument("--session-file", type=Path, required=True)
    imp.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.project_root = args.project_root.resolve()
    try:
        if args.command == "inspect":
            result, code = cmd_inspect(args), 0
        elif args.command == "init":
            result, code = cmd_init(args), 0
        elif args.command == "apply":
            result, code = cmd_apply(args), 0
        elif args.command == "index":
            result, code = cmd_index(args), 0
        elif args.command == "search":
            result, code = cmd_search(args), 0
        elif args.command == "check":
            result, code = cmd_check(args)
        elif args.command == "doctor":
            result, code = cmd_doctor(args), 0
        elif args.command == "workflow":
            result, code = cmd_workflow(args)
        elif args.command == "resume":
            result, code = cmd_resume(args), 0
        elif args.command == "import-session":
            result, code = cmd_import_session(args), 0
        else:
            raise PMError(f"unknown command: {args.command}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except (PMError, workflow.WorkflowError) as exc:
        print(json.dumps({"error": str(exc), "command": args.command}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
