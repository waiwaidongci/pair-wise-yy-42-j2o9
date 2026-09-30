from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ensure_role, optional_text, require_choice,
                     require_number, require_text)
from .repository import Repository
from .rules import (AMEND_ROLES, AUDIT_ROLES, CREATE_ROLES, OCCUPY_ROLES,
                    REGISTER_RESOURCE_ROLES, RESOLVE_REVIEW_ROLES, TASK_ROLES,
                    VIEW_ROLES, WIND_DIRECTIONS, ZONE_KINDS, assess_risk,
                    available_resources, entry_content_hash,
                    ticket_can_close, ticket_content_hash,
                    task_basis_hash, validate_task_transition)
from .domain import RESOURCE_KINDS, TASK_STATUSES


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    # ---------- 入参规范化：离线条目与在线登记共用同一套校验 ----------
    @staticmethod
    def _normalize_ticket_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
        ticket_no = require_text(payload.get("ticket_no"), "ticket_no", 40)
        fireline = require_number(payload.get("fireline_length_km", 0),
                                  "fireline_length_km")
        wind = require_choice(payload.get("wind_direction"), "wind_direction",
                              WIND_DIRECTIONS)
        wind_speed = require_number(payload.get("wind_speed_kmh", 0),
                                    "wind_speed_kmh")
        zone_kind = require_choice(payload.get("zone_kind"), "zone_kind", ZONE_KINDS)
        zone_name = require_text(payload.get("zone_name"), "zone_name", 120)
        note = optional_text(payload.get("note"), "note", 2000)
        risk = assess_risk(fireline, wind, wind_speed, zone_kind)
        content_hash = ticket_content_hash(
            ticket_no, fireline, wind, wind_speed, zone_kind, zone_name, note)
        return {
            "ticket_no": ticket_no, "fireline_length_km": fireline,
            "wind_direction": wind, "wind_speed_kmh": wind_speed,
            "zone_kind": zone_kind, "zone_name": zone_name, "note": note,
            "risk": risk, "content_hash": content_hash,
        }

    def _prepare_tasks(self, fields: Dict[str, Any],
                       raw_tasks: Any) -> List[Dict[str, Any]]:
        names: set = set()
        if raw_tasks is None:
            raw_tasks = [{"name": f"{fields['zone_name']}-主任务", "status": "pending"}]
        if not isinstance(raw_tasks, list) or not raw_tasks:
            from .domain import ValidationError
            raise ValidationError("tasks必须是非空数组")
        tasks = []
        for raw in raw_tasks:
            if not isinstance(raw, dict):
                from .domain import ValidationError
                raise ValidationError("tasks的每一项必须是对象")
            name = require_text(raw.get("name"), "task.name", 120)
            if name in names:
                from .domain import ValidationError
                raise ValidationError(f"任务名重复: {name}")
            names.add(name)
            status = require_choice(raw.get("status", "pending"), "task.status",
                                    TASK_STATUSES)
            tasks.append({
                "name": name, "status": status,
                "conclusion": self._conclusion(fields, name),
                "basis_hash": task_basis_hash(
                    fields["fireline_length_km"], fields["wind_direction"],
                    fields["wind_speed_kmh"], fields["zone_kind"],
                    fields["zone_name"], name),
            })
        return tasks

    @staticmethod
    def _conclusion(fields: Dict[str, Any], task_name: str) -> Dict[str, Any]:
        risk = fields["risk"]
        return {
            "task": task_name,
            "risk_level": risk["risk_level"],
            "risk_score": risk["risk_score"],
            "spread_direction": risk["spread_direction"],
            "tactic": risk["tactic"],
        }

    def _prepare_occupies(self, raw: Any) -> List[str]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            from .domain import ValidationError
            raise ValidationError("occupy_resources必须是资源编号数组")
        known = {r["code"] for r in self.repository.list_resources()}
        codes: List[str] = []
        for code in raw:
            code = require_text(code, "occupy_resources[]", 40)
            if code not in known:
                from .domain import ValidationError
                raise ValidationError(f"资源不存在: {code}")
            if code not in codes:
                codes.append(code)
        return codes

    def _prepare_entry(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        fields = self._normalize_ticket_fields(payload)
        tasks = self._prepare_tasks(fields, payload.get("tasks"))
        occupies = self._prepare_occupies(payload.get("occupy_resources"))
        # 离线条目指纹含任务，不含资源（资源随当下可用性争夺）
        fields["content_hash"] = entry_content_hash(
            fields["fireline_length_km"], fields["wind_direction"],
            fields["wind_speed_kmh"], fields["zone_kind"], fields["zone_name"],
            fields["note"], payload.get("tasks") or [])
        proposed = {
            "fireline_length_km": fields["fireline_length_km"],
            "wind_direction": fields["wind_direction"],
            "wind_speed_kmh": fields["wind_speed_kmh"],
            "zone_kind": fields["zone_kind"],
            "zone_name": fields["zone_name"],
            "note": fields["note"],
            "tasks": [{"name": t["name"], "status": t["status"]} for t in tasks],
            "occupy_resources": occupies,
        }
        return {**fields, "tasks": tasks, "occupies": occupies,
                "proposed": proposed}

    # ---------- 资源 ----------
    def register_resource(self, payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, REGISTER_RESOURCE_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("code"), "code", 40)
        name = require_text(payload.get("name"), "name", 120)
        kind = require_choice(payload.get("kind"), "kind", RESOURCE_KINDS)
        capacity = require_number(payload.get("capacity", 1), "capacity", 0.000001)
        return self.repository.create_resource(code, name, kind, capacity, actor)

    def list_resources(self, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        active = self.repository.active_resource_codes()
        resources = self.repository.list_resources()
        return {"resources": resources,
                "available": available_resources(resources, active),
                "active_occupations": self.repository.list_occupations(status="active")}

    def occupy(self, ticket_no: str, payload: Dict[str, Any], actor: str,
               role: str) -> Dict[str, Any]:
        ensure_role(role, OCCUPY_ROLES)
        actor = require_text(actor, "actor", 100)
        ticket = self.repository.get_ticket(ticket_no)
        codes = self._prepare_occupies(payload.get("occupy_resources"))
        if not codes:
            from .domain import ValidationError
            raise ValidationError("occupy_resources不能为空")
        occupied, conflicts = [], []
        for code in codes:
            try:
                self.repository.occupy(ticket["id"], code, actor)
                occupied.append(code)
            except ConflictError as exc:
                conflicts.append({"resource_code": code, "holder": exc.payload["holder"]})
        # 落败方拿到占用对象（holder）与重新计算后的可用资源
        if conflicts:
            resources = self.repository.list_resources()
            active = self.repository.active_resource_codes()
            raise ConflictError(
                "资源占用冲突：只允许一笔成立",
                {"occupied": occupied, "blocked": conflicts,
                 "available_resources": available_resources(resources, active)})
        return {"ticket_no": ticket_no, "occupied": occupied}

    def release_occupation(self, occupation_id: int, actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, OCCUPY_ROLES)
        actor = require_text(actor, "actor", 100)
        return self.repository.release(occupation_id, actor)

    # ---------- 在线登记 / 单号查询 ----------
    def register_ticket(self, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        prepared = self._prepare_entry(payload)
        try:
            self.repository.register_ticket_direct(prepared, actor)
        except ConflictError as exc:
            resources = self.repository.list_resources()
            active = self.repository.active_resource_codes()
            exc.payload["available_resources"] = available_resources(resources, active)
            raise
        return self.get_ticket_view(prepared["ticket_no"], "viewer")

    def list_tickets(self, role: str) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        result = []
        for ticket in self.repository.list_tickets():
            result.append(self._enrich_ticket(ticket))
        return result

    def get_ticket_view(self, ticket_no: str, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        return self._enrich_ticket(self.repository.get_ticket(ticket_no))

    def _enrich_ticket(self, ticket: Dict[str, Any]) -> Dict[str, Any]:
        tasks = self.repository.list_tasks(ticket["id"])
        fields = ticket
        stale_tasks = []
        for task in tasks:
            if task["status"] == "cancelled":
                task["stale"] = False
                continue
            expected_basis = task_basis_hash(
                fields["fireline_length_km"], fields["wind_direction"],
                fields["wind_speed_kmh"], fields["zone_kind"], fields["zone_name"],
                task["name"])
            task["stale"] = task["basis_hash"] != expected_basis
            if task["stale"]:
                stale_tasks.append(task["id"])
        occupations = self.repository.list_occupations(ticket["ticket_no"])
        ticket = dict(ticket)
        ticket["tasks"] = tasks
        ticket["occupations"] = occupations
        ticket["stale_task_ids"] = stale_tasks
        return ticket

    # ---------- 火线/风向/任务区改动：关联任务结论失效重算 ----------
    def amend_ticket(self, ticket_no: str, payload: Dict[str, Any], actor: str,
                     role: str, source: str = "amend") -> Dict[str, Any]:
        ensure_role(role, AMEND_ROLES)
        actor = require_text(actor, "actor", 100)
        ticket = self.repository.get_ticket(ticket_no)
        merged = {
            "ticket_no": ticket_no,
            "fireline_length_km": payload.get("fireline_length_km", ticket["fireline_length_km"]),
            "wind_direction": payload.get("wind_direction", ticket["wind_direction"]),
            "wind_speed_kmh": payload.get("wind_speed_kmh", ticket["wind_speed_kmh"]),
            "zone_kind": payload.get("zone_kind", ticket["zone_kind"]),
            "zone_name": payload.get("zone_name", ticket["zone_name"]),
            "note": payload.get("note", ticket["note"]),
        }
        fields = self._normalize_ticket_fields(merged)
        changed = [name for name in
                   ("fireline_length_km", "wind_direction", "wind_speed_kmh",
                    "zone_kind", "zone_name", "note")
                   if ticket[name] != fields[name]]
        existing = self.repository.list_tasks(ticket["id"])
        # 未显式给出任务清单时，对全部未取消任务按新输入重算；其他任务照常
        raw_tasks = payload.get("tasks")
        if raw_tasks is None:
            raw_tasks = [{"name": t["name"],
                          "status": "active" if t["status"] == "done" else t["status"]}
                         for t in existing if t["status"] != "cancelled"]
        tasks = self._prepare_tasks(fields, raw_tasks)
        occupies = self._prepare_occupies(payload.get("occupy_resources"))
        outcome = self.repository.apply_ticket_update(
            ticket["id"], {**fields, "changed_fields": changed}, tasks, occupies,
            actor, source)
        return {"ticket_no": ticket_no, "changed_fields": changed, **outcome,
                "ticket": self._enrich_ticket(self.repository.get_ticket(ticket_no))}

    # ---------- 任务状态 ----------
    def transition_task(self, task_id: int, target: str, actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_ROLES)
        actor = require_text(actor, "actor", 100)
        task = self.repository.get_task(task_id)
        validate_task_transition(task["status"], target)
        return self.repository.transition_task(task_id, target, actor)

    # ---------- 离线批次 ----------
    def submit_batch(self, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        entries_raw = payload.get("entries")
        if not isinstance(entries_raw, list) or not entries_raw:
            from .domain import ValidationError
            raise ValidationError("entries必须是非空数组")
        if len(entries_raw) > 500:
            from .domain import ValidationError
            raise ValidationError("单批次条目不能超过500")
        notes = payload.get("notes")
        if notes is not None and not isinstance(notes, str):
            from .domain import ValidationError
            raise ValidationError("notes必须是字符串")
        # 先在服务层规范化；规范化失败不落库（现场单号内容本身不合法）
        prepared = [self._prepare_entry(entry) for entry in entries_raw]
        # 批次连同原始内容先入库并保留，保证下次可以原样重试
        batch = self.repository.create_batch(
            actor, {"entries": entries_raw, "notes": notes},
            notes=[notes] if isinstance(notes, str) else [])
        for item in prepared:
            item["batch_id"] = batch["id"]
            item["batch_no"] = batch["batch_no"]
        outcome = self.repository.merge_entries(batch["id"], prepared, actor)
        result = self.repository.get_batch(batch["id"])
        if outcome["status"] == "pending_retry":
            result["retryable"] = True
            result["error"] = outcome["error"]
        return self._enrich_batch_results(result)

    def retry_batch(self, batch_no: str, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self.repository.get_batch_by_no(batch_no)
        if batch["status"] != "pending_retry":
            raise ConflictError(f"批次当前状态为{batch['status']}，无需重试")
        prepared = [self._prepare_entry(entry)
                    for entry in batch["payload"]["entries"]]
        for item in prepared:
            item["batch_id"] = batch["id"]
            item["batch_no"] = batch["batch_no"]
        self.repository.merge_entries(batch["id"], prepared, actor)
        return self._enrich_batch_results(self.repository.get_batch(batch["id"]))

    def _enrich_batch_results(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        # 给落败条目附上重新计算后的可用资源清单
        resources = self.repository.list_resources()
        active = self.repository.active_resource_codes()
        avail = available_resources(resources, active)
        for entry in batch["entries"]:
            if entry["blocked"]:
                entry["available_resources"] = avail
        counts: Dict[str, int] = {}
        for entry in batch["entries"]:
            counts[entry["outcome"]] = counts.get(entry["outcome"], 0) + 1
        batch["outcome_counts"] = counts
        batch["retryable"] = batch["status"] == "pending_retry"
        return batch

    def list_batches(self, role: str) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        return [self._enrich_batch_results(b)
                for b in self.repository.list_batches()]

    def get_batch_view(self, batch_no: str, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        return self._enrich_batch_results(self.repository.get_batch_by_no(batch_no))

    # ---------- 待核对 ----------
    def list_reviews(self, role: str) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        return self.repository.list_reviews("pending")

    def resolve_review(self, review_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, RESOLVE_REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        review = self.repository.get_review(review_id)
        decision = require_choice(payload.get("decision"), "decision",
                                  ("apply", "reject"))
        note = optional_text(payload.get("note"), "note", 500)
        if decision == "apply":
            proposed = review["proposed"]
            proposed["ticket_no"] = review["ticket_no"]
            # 核对通过：按新输入更新正式记录，关联任务结论同步重算
            amended = self.amend_ticket(review["ticket_no"], proposed, actor, role,
                                        source="review_apply")
        self.repository.resolve_review(review_id,
                                       "applied" if decision == "apply" else "rejected",
                                       actor, note)
        if decision == "reject":
            return {"review_id": review_id, "decision": "reject",
                    "ticket_no": review["ticket_no"]}
        return {"review_id": review_id, "decision": "apply",
                "ticket_no": review["ticket_no"], **amended}

    # ---------- 关单 ----------
    def close_ticket(self, ticket_no: str, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, AMEND_ROLES)
        actor = require_text(actor, "actor", 100)
        ticket = self.repository.get_ticket(ticket_no)
        open_tasks = sum(1 for t in self.repository.list_tasks(ticket["id"])
                         if t["status"] not in ("done", "cancelled"))
        active_occ = len(self.repository.list_occupations(ticket_no, status="active"))
        blockers = ticket_can_close(open_tasks, active_occ)
        if blockers:
            raise ConflictError("；".join(blockers))
        return self.repository.close_ticket(ticket["id"], actor)

    # ---------- 审计 ----------
    def audit(self, role: str, entity_id: Optional[str] = None) -> List[Dict[str, Any]]:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(entity_id)

    def verify_chain(self, role: str) -> bool:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.verify_audit_chain()
