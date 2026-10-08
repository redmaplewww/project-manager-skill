#!/usr/bin/env python3
"""Deterministic state, archive, and index helper for the project-manager skill."""

from __future__ import annotations

import argparse
import copy
import hashlib
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


SCHEMA_VERSION = 1
DOCS_REL = Path(".project-to-act/docs/project-manager")
LOCAL_REL = Path(".project-to-act/local/project-manager")
STATE_NAME = "state.json"
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
    "candidate", "clarification_needed", "pending_confirmation", "confirmed", "rejected", "superseded"
}
READINESS = {"not_ready", "ready_for_design", "ready_for_build", "ready_for_acceptance"}
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
        "pending_confirmation": {"clarification_needed", "confirmed", "rejected"},
        "confirmed": {"superseded", "rejected"},
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
        if record.get("status") == "confirmed" and not record.get("acceptance_criteria"):
            raise PMError(f"confirmed requirement {record_id} requires acceptance_criteria")
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


def apply_operation(state: dict[str, Any], op: dict[str, Any], stamp: str) -> dict[str, Any]:
    kind = op.get("op")
    if kind == "set_project":
        patch = op.get("patch")
        if not isinstance(patch, dict):
            raise PMError("set_project requires patch object")
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
        record["version"] = record.get("version", 1) + 1
        record["updated_at"] = stamp
        validate_record(collection, record)
        if collection == "requirements" and op.get("change_class") != "wording_only":
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
        old = record.get("status")
        new = op.get("to")
        if new == old:
            return {"op": "noop", "collection": collection, "id": record_id, "reason": "same_status"}
        allowed = ALLOWED_TRANSITIONS.get(collection, {}).get(old, set())
        if new not in allowed:
            raise PMError(f"invalid transition for {record_id}: {old} -> {new}")
        if collection == "requirements" and new in {"confirmed", "rejected", "superseded"}:
            if not op.get("source_ref") or not op.get("reason"):
                raise PMError(f"transition to {new} requires source_ref and reason")
            if new == "confirmed" and not record.get("acceptance_criteria"):
                raise PMError(f"confirmed requirement {record_id} requires acceptance_criteria")
            get_record(state, "sources", op["source_ref"])
        record["status"] = new
        if collection == "requirements" and new in {"confirmed", "rejected", "superseded"}:
            record.setdefault("status_history", []).append({
                "from": old, "to": new, "reason": op["reason"], "source_ref": op["source_ref"], "timestamp": stamp
            })
        record["updated_at"] = stamp
        return {"op": kind, "collection": collection, "id": record_id, "from": old, "to": new}

    if kind == "add_source_ref":
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

    atomic_text(docs / "INDEX.md", "\n".join(index) + "\n")
    atomic_text(docs / "REQUIREMENTS.md", "\n".join(req_lines) + "\n")
    atomic_text(docs / "DELIVERY.md", "\n".join(delivery) + "\n")
    atomic_text(docs / "DECISIONS.md", "\n".join(decisions) + "\n")


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
        results = [apply_operation(updated, op, stamp) for op in operations]
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
    confirmed = [r for r in state["requirements"] if r.get("status") == "confirmed"]
    linked = {rid for wp in state["work_packages"] for rid in wp.get("requirement_ids", [])}
    for req in confirmed:
        if req["id"] not in linked:
            warnings.append({"code": "CONFIRMED_WITHOUT_WORK_PACKAGE", "record": req["id"]})
    for item in state["intake_items"]:
        if item.get("disposition", "unclassified") == "unclassified":
            warnings.append({"code": "UNCLASSIFIED_INTAKE_ITEM", "record": item["id"]})
    result = {
        "valid": not errors,
        "strict_valid": not errors and not warnings,
        "revision": state["revision"],
        "errors": errors,
        "warnings": warnings,
    }
    return result, 0 if (not errors and (not args.strict or not warnings)) else 1


def cmd_resume(args: argparse.Namespace) -> dict[str, Any]:
    state = load_state(args.project_root)
    active_reqs = [r for r in state["requirements"] if r.get("status") not in {"rejected", "superseded"}]
    pending = [r for r in active_reqs if r.get("status") in {"clarification_needed", "pending_confirmation"}]
    active_work = [w for w in state["work_packages"] if w.get("status") not in {"done", "cancelled"}]
    open_risks = [r for r in state["risks"] if r.get("status") not in {"closed"}]
    return {
        "project": state["project"],
        "revision": state["revision"],
        "requirements": {"active": len(active_reqs), "confirmed": sum(r.get("status") == "confirmed" for r in active_reqs)},
        "pending_confirmation": [{"id": r["id"], "title": r.get("title"), "status": r.get("status")} for r in pending[:10]],
        "active_work": [{"id": w["id"], "title": w.get("title"), "status": w.get("status")} for w in active_work[:10]],
        "open_risks": [{"id": r["id"], "title": r.get("title"), "status": r.get("status")} for r in open_risks[:10]],
        "deadline_alerts": [r for r in deadline_rows(state, args.hours) if r["alert"] != "scheduled"],
        "last_changes": state.get("change_events", [])[-5:],
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
        elif args.command == "resume":
            result, code = cmd_resume(args), 0
        elif args.command == "import-session":
            result, code = cmd_import_session(args), 0
        else:
            raise PMError(f"unknown command: {args.command}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except PMError as exc:
        print(json.dumps({"error": str(exc), "command": args.command}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
