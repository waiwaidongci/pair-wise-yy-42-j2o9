from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ensure_role, normalize_severity, require_dict,
                     require_number, require_positive_int, require_text,
                     optional_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_SUBMIT_ROLES, CREATE_ROLES, ENTITY,
                    OCCUPANCY_ROLES, RECORD_ROLES, RESOURCE_ROLES, STRUCTURE_ROLES,
                    VIEW_ROLES, completion_blockers, content_hash,
                    escalation_required, priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---------- 离线批次：按单号登记，回网合并 ----------
    def submit_batch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        require_dict(payload, "payload")
        order_no = require_text(payload.get("order_no"), "order_no", 100)
        item_id = payload.get("item_id")
        if item_id is not None:
            require_positive_int(item_id, "item_id")
            self.repository.get_item(item_id)
        content = content_hash(payload)
        existing = self.repository.get_batch_by_order_no(order_no)
        if existing is not None:
            if existing["content_hash"] == content:
                return {"batch": existing, "outcome": "idempotent"}
            # 内容不同：留下待核对，不覆盖正式记录
            import uuid
            review_order_no = f"{order_no}#conflict#{uuid.uuid4().hex[:12]}"
            review = self.repository.create_batch(review_order_no, item_id, content, payload, actor)
            review = self.repository.update_batch_status(
                review["id"], "conflict", original_batch_id=existing["id"])
            return {"batch": review, "outcome": "conflict"}
        batch = self.repository.create_batch(order_no, item_id, content, payload, actor)
        try:
            merged = self.repository.apply_batch_merge(batch, actor)
        except Exception:
            failed = self.repository.get_batch(batch["id"])
            return {"batch": failed, "outcome": "failed"}
        return {"batch": merged, "outcome": "merged"}

    def get_batch(self, order_no: str, role: str) -> Dict[str, Any]:
        self._view(role)
        batch = self.repository.get_batch_by_order_no(order_no)
        if batch is None:
            from .domain import NotFoundError
            raise NotFoundError("批次不存在")
        return batch

    def retry_batch(self, order_no: str, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self.get_batch(order_no, role)
        retried = self.repository.retry_batch(batch, actor)
        outcome = "merged" if retried["status"] == "merged" else "failed"
        return {"batch": retried, "outcome": outcome}

    def list_batches(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return self.repository.list_batches(status)

    # ---------- 资源与占用 ----------
    def create_resource(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RESOURCE_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        kind = require_text(payload.get("kind", ""), "kind", 100)
        total = require_number(payload.get("total_quantity", 0), "total_quantity")
        return self.repository.create_resource(name, kind, total)

    def list_resources(self, role: str) -> list:
        self._view(role)
        return self.repository.list_resources()

    def create_occupancy(self, resource_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, OCCUPANCY_ROLES)
        actor = require_text(actor, "actor", 100)
        require_positive_int(resource_id, "resource_id")
        quantity = require_number(payload.get("quantity", 0), "quantity")
        task_id = payload.get("task_id")
        task_area_id = payload.get("task_area_id")
        if task_id is not None:
            require_positive_int(task_id, "task_id")
        if task_area_id is not None:
            require_positive_int(task_area_id, "task_area_id")
        return self.repository.create_occupancy(
            resource_id, quantity, task_id, task_area_id, actor)

    def list_occupancies(self, resource_id: int, role: str) -> list:
        self._view(role)
        require_positive_int(resource_id, "resource_id")
        return self.repository.list_occupancies(resource_id)

    def release_occupancy(self, occupancy_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, OCCUPANCY_ROLES)
        actor = require_text(actor, "actor", 100)
        require_positive_int(occupancy_id, "occupancy_id")
        return self.repository.release_occupancy(occupancy_id)

    # ---------- 火线 / 风向 / 任务区：改动后关联任务结论失效重算 ----------
    def create_fire_line(self, item_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, STRUCTURE_ROLES)
        actor = require_text(actor, "actor", 100)
        require_positive_int(item_id, "item_id")
        self.repository.get_item(item_id)
        name = require_text(payload.get("name"), "name", 100)
        length = require_number(payload.get("length", 0), "length")
        return self.repository.create_fire_line(item_id, name, length, actor)

    def update_fire_line(self, fire_line_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, STRUCTURE_ROLES)
        actor = require_text(actor, "actor", 100)
        require_positive_int(fire_line_id, "fire_line_id")
        length = require_number(payload.get("length", 0), "length")
        fire_line, recomputed = self.repository.update_fire_line_length(
            fire_line_id, length, actor)
        return {"fire_line": fire_line, "recomputed_tasks": recomputed}

    def create_wind_record(self, item_id: int, payload: Dict[str, Any],
                           actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, STRUCTURE_ROLES)
        actor = require_text(actor, "actor", 100)
        require_positive_int(item_id, "item_id")
        self.repository.get_item(item_id)
        name = require_text(payload.get("name"), "name", 100)
        direction = require_text(payload.get("direction"), "direction", 50)
        speed = require_number(payload.get("speed", 0), "speed")
        return self.repository.create_wind_record(item_id, name, direction, speed, actor)

    def update_wind_record(self, wind_id: int, payload: Dict[str, Any],
                           actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, STRUCTURE_ROLES)
        actor = require_text(actor, "actor", 100)
        require_positive_int(wind_id, "wind_id")
        direction = require_text(payload.get("direction"), "direction", 50)
        speed = require_number(payload.get("speed", 0), "speed")
        wind, recomputed = self.repository.update_wind_record(wind_id, direction, speed, actor)
        return {"wind_record": wind, "recomputed_tasks": recomputed}

    def create_task_area(self, item_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, STRUCTURE_ROLES)
        actor = require_text(actor, "actor", 100)
        require_positive_int(item_id, "item_id")
        self.repository.get_item(item_id)
        name = require_text(payload.get("name"), "name", 100)
        description = optional_text(payload.get("description", ""), "description")
        factor = require_number(payload.get("factor", 1), "factor")
        return self.repository.create_task_area(item_id, name, description, factor, actor)

    def update_task_area(self, area_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, STRUCTURE_ROLES)
        actor = require_text(actor, "actor", 100)
        require_positive_int(area_id, "area_id")
        description = optional_text(payload.get("description", ""), "description")
        factor = require_number(payload.get("factor", 1), "factor")
        area, recomputed = self.repository.update_task_area(area_id, description, factor, actor)
        return {"task_area": area, "recomputed_tasks": recomputed}

    # ---------- 任务 ----------
    def create_task(self, item_id: int, payload: Dict[str, Any],
                    actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, STRUCTURE_ROLES)
        actor = require_text(actor, "actor", 100)
        require_positive_int(item_id, "item_id")
        self.repository.get_item(item_id)
        title = require_text(payload.get("title"), "title", 200)
        description = optional_text(payload.get("description", ""), "description")
        status = payload.get("status", "pending")
        if status not in ("pending", "active", "done"):
            raise ValueError("status必须是pending/active/done")
        task_area_id = payload.get("task_area_id")
        fire_line_id = payload.get("fire_line_id")
        wind_id = payload.get("wind_id")
        for field, value in (("task_area_id", task_area_id),
                             ("fire_line_id", fire_line_id), ("wind_id", wind_id)):
            if value is not None:
                require_positive_int(value, field)
        return self.repository.create_task(
            item_id, title, description, task_area_id, fire_line_id, wind_id, status, actor)

    def get_task(self, task_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        require_positive_int(task_id, "task_id")
        return self.repository.get_task(task_id)

    def list_tasks(self, item_id: int, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        require_positive_int(item_id, "item_id")
        return self.repository.list_tasks(item_id, status)

    def list_fire_lines(self, item_id: int, role: str) -> list:
        self._view(role)
        require_positive_int(item_id, "item_id")
        return self.repository.list_fire_lines(item_id)

    def list_wind_records(self, item_id: int, role: str) -> list:
        self._view(role)
        require_positive_int(item_id, "item_id")
        return self.repository.list_wind_records(item_id)

    def list_task_areas(self, item_id: int, role: str) -> list:
        self._view(role)
        require_positive_int(item_id, "item_id")
        return self.repository.list_task_areas(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
