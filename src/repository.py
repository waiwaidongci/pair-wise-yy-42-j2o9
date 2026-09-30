from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, OccupancyConflictError, ValidationError
from .rules import (ID_PREFIX, STATES, available_resources, compute_conclusion,
                    occupancy_fits)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS offline_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','merging','merged','conflict','failed')),
                    payload TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    original_batch_id INTEGER REFERENCES offline_batches(id) ON DELETE SET NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fire_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    length REAL NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, name)
                );
                CREATE TABLE IF NOT EXISTS wind_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    speed REAL NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, name)
                );
                CREATE TABLE IF NOT EXISTS task_areas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    factor REAL NOT NULL DEFAULT 1.0,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, name)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    task_area_id INTEGER REFERENCES task_areas(id) ON DELETE SET NULL,
                    fire_line_id INTEGER REFERENCES fire_lines(id) ON DELETE SET NULL,
                    wind_id INTEGER REFERENCES wind_records(id) ON DELETE SET NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','active','done')),
                    conclusion TEXT,
                    conclusion_stale INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL DEFAULT '',
                    total_quantity REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resource_occupancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
                    quantity REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','released')),
                    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                    task_area_id INTEGER REFERENCES task_areas(id) ON DELETE SET NULL,
                    batch_id INTEGER REFERENCES offline_batches(id) ON DELETE SET NULL,
                    occupied_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    released_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_occupancies_resource_active
                    ON resource_occupancies(resource_id) WHERE status='active';
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ---------- 离线批次 ----------
    def create_batch(self, order_no: str, item_id: Optional[int], content_hash: str,
                     payload: dict, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO offline_batches(order_no, item_id, content_hash, status,
                   payload, created_by, created_at, updated_at)
                   VALUES(?,?,?, 'pending', ?, ?, ?, ?)""",
                (order_no, item_id, content_hash,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), actor, now, now),
            )
            batch_id = int(cur.lastrowid)
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM offline_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return self._batch(row)

    def get_batch_by_order_no(self, order_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM offline_batches WHERE order_no=?", (order_no,)).fetchone()
        return self._batch(row) if row else None

    def list_batches(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM offline_batches"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._batch(r) for r in rows]

    def update_batch_status(self, batch_id: int, status: str,
                            result: Optional[dict] = None, error: Optional[str] = None,
                            original_batch_id: Optional[int] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE offline_batches SET status=?, result=?, error=?,
                   original_batch_id=?, updated_at=? WHERE id=?""",
                (status,
                 json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None,
                 error, original_batch_id, now, batch_id),
            )
        return self.get_batch(batch_id)

    @staticmethod
    def _batch(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        if d.get("result"):
            d["result"] = json.loads(d["result"])
        return d

    def apply_batch_merge(self, batch: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """一个事务内合并批次：火线、风向、任务区、任务、资源占用。
        资源占用冲突时落败方拿到占用对象并重算可用资源；
        其他失败回滚占用与任务，保留批次待下次重试。"""
        payload = batch["payload"]
        item_id = batch["item_id"]
        result: Dict[str, Any] = {
            "fire_lines": [], "wind_records": [], "task_areas": [],
            "tasks": [], "resource_occupancies": [], "resource_conflicts": [],
        }
        try:
            with self._lock, self.conn:
                now = utc_now()
                self.conn.execute(
                    "UPDATE offline_batches SET status='merging', updated_at=? WHERE id=?",
                    (now, batch["id"]),
                )
                # 火线
                fl_map: Dict[str, Dict[str, Any]] = {}
                for fl in payload.get("fire_lines", []):
                    cur = self.conn.execute(
                        """INSERT INTO fire_lines(item_id, name, length, created_by,
                           created_at, updated_at) VALUES(?,?,?,?,?,?)""",
                        (item_id, fl["name"], float(fl["length"]), actor, now, now),
                    )
                    fl_map[fl["name"]] = {"id": int(cur.lastrowid), "length": float(fl["length"])}
                    result["fire_lines"].append(
                        {"id": int(cur.lastrowid), "name": fl["name"], "length": float(fl["length"])})
                # 风向
                wind_map: Dict[str, Dict[str, Any]] = {}
                for w in payload.get("wind_records", []):
                    cur = self.conn.execute(
                        """INSERT INTO wind_records(item_id, name, direction, speed,
                           created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?)""",
                        (item_id, w["name"], w["direction"], float(w["speed"]), actor, now, now),
                    )
                    wind_map[w["name"]] = {"id": int(cur.lastrowid),
                                           "direction": w["direction"], "speed": float(w["speed"])}
                    result["wind_records"].append(
                        {"id": int(cur.lastrowid), "name": w["name"]})
                # 任务区
                area_map: Dict[str, Dict[str, Any]] = {}
                for ta in payload.get("task_areas", []):
                    cur = self.conn.execute(
                        """INSERT INTO task_areas(item_id, name, description, factor,
                           created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?)""",
                        (item_id, ta["name"], ta.get("description", ""),
                         float(ta.get("factor", 1.0)), actor, now, now),
                    )
                    area_map[ta["name"]] = {"id": int(cur.lastrowid),
                                            "factor": float(ta.get("factor", 1.0))}
                    result["task_areas"].append(
                        {"id": int(cur.lastrowid), "name": ta["name"]})
                # 任务（结论由火线长度、风向、任务区系数计算）
                for t in payload.get("tasks", []):
                    area = area_map.get(t.get("task_area_name"))
                    fl = fl_map.get(t.get("fire_line_name"))
                    w = wind_map.get(t.get("wind_name"))
                    task_area_id = area["id"] if area else None
                    fire_line_id = fl["id"] if fl else None
                    wind_id = w["id"] if w else None
                    conclusion = compute_conclusion(
                        fl["length"] if fl else 0.0,
                        w["speed"] if w else 0.0,
                        area["factor"] if area else 1.0,
                    )
                    cur = self.conn.execute(
                        """INSERT INTO tasks(item_id, task_area_id, fire_line_id, wind_id,
                           title, description, status, conclusion, conclusion_stale, version,
                           created_by, created_at, updated_at)
                           VALUES(?,?,?,?,?,?,?, ?, 0, 1, ?, ?, ?)""",
                        (item_id, task_area_id, fire_line_id, wind_id, t["title"],
                         t.get("description", ""), t.get("status", "pending"),
                         json.dumps(conclusion, ensure_ascii=False, sort_keys=True),
                         actor, now, now),
                    )
                    result["tasks"].append(
                        {"id": int(cur.lastrowid), "title": t["title"], "conclusion": conclusion})
                # 资源占用：冲突时落败方拿到占用对象并重算可用资源
                for occ in payload.get("resource_occupancies", []):
                    res = self.conn.execute(
                        "SELECT * FROM resources WHERE name=?", (occ["resource_name"],)
                    ).fetchone()
                    if res is None:
                        raise ValidationError(f"资源不存在: {occ['resource_name']}")
                    quantity = float(occ["quantity"])
                    area = area_map.get(occ.get("task_area_name"))
                    task_area_id = area["id"] if area else None
                    try:
                        cur = self.conn.execute(
                            """INSERT INTO resource_occupancies(resource_id, quantity, status,
                               task_area_id, batch_id, occupied_by, created_at)
                               VALUES(?,?, 'active', ?, ?, ?, ?)""",
                            (res["id"], quantity, task_area_id, batch["id"], actor, now),
                        )
                        result["resource_occupancies"].append(
                            {"id": int(cur.lastrowid), "resource": res["name"], "quantity": quantity})
                    except sqlite3.IntegrityError:
                        winner = self.conn.execute(
                            """SELECT * FROM resource_occupancies
                               WHERE resource_id=? AND status='active'""",
                            (res["id"],),
                        ).fetchone()
                        occupied = winner["quantity"] if winner else 0.0
                        available = available_resources(res["total_quantity"], occupied)
                        result["resource_conflicts"].append({
                            "resource": res["name"],
                            "winner_occupancy": dict(winner) if winner else None,
                            "available_after": available,
                            "requested_quantity": quantity,
                        })
                self.conn.execute(
                    "UPDATE offline_batches SET status='merged', result=?, updated_at=? WHERE id=?",
                    (json.dumps(result, ensure_ascii=False, sort_keys=True), now, batch["id"]),
                )
        except Exception as exc:
            self.update_batch_status(batch["id"], "failed", error=str(exc))
            raise
        return self.get_batch(batch["id"])

    def retry_batch(self, batch: Dict[str, Any], actor: str) -> Dict[str, Any]:
        if batch["status"] not in ("pending", "failed"):
            raise ConflictError(f"批次状态{batch['status']}不可重试")
        return self.apply_batch_merge(batch, actor)

    # ---------- 火线 / 风向 / 任务区 ----------
    def create_fire_line(self, item_id: int, name: str, length: float,
                         actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO fire_lines(item_id, name, length, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (item_id, name, length, actor, now, now),
            )
            fire_line_id = int(cur.lastrowid)
        return self.get_fire_line(fire_line_id)

    def get_fire_line(self, fire_line_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM fire_lines WHERE id=?", (fire_line_id,)).fetchone()
        if row is None:
            raise NotFoundError("火线不存在")
        return dict(row)

    def list_fire_lines(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM fire_lines WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_fire_line_length(self, fire_line_id: int, length: float,
                                 actor: str) -> tuple:
        """改动火线长度，关联任务结论失效重算，其他任务照常。"""
        now = utc_now()
        with self._lock, self.conn:
            fl = self.conn.execute(
                "SELECT * FROM fire_lines WHERE id=?", (fire_line_id,)
            ).fetchone()
            if fl is None:
                raise NotFoundError("火线不存在")
            self.conn.execute(
                "UPDATE fire_lines SET length=?, version=version+1, updated_at=? WHERE id=?",
                (length, now, fire_line_id),
            )
            recomputed = self._recompute_tasks(
                self.conn.execute(
                    "SELECT * FROM tasks WHERE fire_line_id=?", (fire_line_id,)
                ).fetchall(),
                override_length=length, now=now)
        return self.get_fire_line(fire_line_id), recomputed

    def create_wind_record(self, item_id: int, name: str, direction: str,
                           speed: float, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO wind_records(item_id, name, direction, speed,
                   created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?)""",
                (item_id, name, direction, speed, actor, now, now),
            )
            wind_id = int(cur.lastrowid)
        return self.get_wind_record(wind_id)

    def get_wind_record(self, wind_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM wind_records WHERE id=?", (wind_id,)).fetchone()
        if row is None:
            raise NotFoundError("风向记录不存在")
        return dict(row)

    def list_wind_records(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM wind_records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_wind_record(self, wind_id: int, direction: str, speed: float,
                           actor: str) -> tuple:
        """改动风向，关联任务结论失效重算，其他任务照常。"""
        now = utc_now()
        with self._lock, self.conn:
            w = self.conn.execute(
                "SELECT * FROM wind_records WHERE id=?", (wind_id,)
            ).fetchone()
            if w is None:
                raise NotFoundError("风向记录不存在")
            self.conn.execute(
                """UPDATE wind_records SET direction=?, speed=?, version=version+1,
                   updated_at=? WHERE id=?""",
                (direction, speed, now, wind_id),
            )
            recomputed = self._recompute_tasks(
                self.conn.execute(
                    "SELECT * FROM tasks WHERE wind_id=?", (wind_id,)
                ).fetchall(),
                override_speed=speed, now=now)
        return self.get_wind_record(wind_id), recomputed

    def create_task_area(self, item_id: int, name: str, description: str,
                         factor: float, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO task_areas(item_id, name, description, factor,
                   created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?)""",
                (item_id, name, description, factor, actor, now, now),
            )
            area_id = int(cur.lastrowid)
        return self.get_task_area(area_id)

    def get_task_area(self, area_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM task_areas WHERE id=?", (area_id,)).fetchone()
        if row is None:
            raise NotFoundError("任务区不存在")
        return dict(row)

    def list_task_areas(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM task_areas WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_task_area(self, area_id: int, description: str, factor: float,
                         actor: str) -> tuple:
        """改动任务区，关联任务结论失效重算，其他任务照常。"""
        now = utc_now()
        with self._lock, self.conn:
            area = self.conn.execute(
                "SELECT * FROM task_areas WHERE id=?", (area_id,)
            ).fetchone()
            if area is None:
                raise NotFoundError("任务区不存在")
            self.conn.execute(
                """UPDATE task_areas SET description=?, factor=?, version=version+1,
                   updated_at=? WHERE id=?""",
                (description, factor, now, area_id),
            )
            recomputed = self._recompute_tasks(
                self.conn.execute(
                    "SELECT * FROM tasks WHERE task_area_id=?", (area_id,)
                ).fetchall(),
                override_factor=factor, now=now)
        return self.get_task_area(area_id), recomputed

    def _recompute_tasks(self, task_rows, override_length=None, override_speed=None,
                         override_factor=None, now=None) -> List[Dict[str, Any]]:
        """在当前事务内重算给定任务的结论并清除失效标记。"""
        if now is None:
            now = utc_now()
        recomputed: List[Dict[str, Any]] = []
        for t in task_rows:
            fl = self.conn.execute(
                "SELECT * FROM fire_lines WHERE id=?", (t["fire_line_id"],)
            ).fetchone() if t["fire_line_id"] else None
            w = self.conn.execute(
                "SELECT * FROM wind_records WHERE id=?", (t["wind_id"],)
            ).fetchone() if t["wind_id"] else None
            area = self.conn.execute(
                "SELECT * FROM task_areas WHERE id=?", (t["task_area_id"],)
            ).fetchone() if t["task_area_id"] else None
            length = override_length if override_length is not None else (fl["length"] if fl else 0.0)
            speed = override_speed if override_speed is not None else (w["speed"] if w else 0.0)
            factor = override_factor if override_factor is not None else (area["factor"] if area else 1.0)
            conclusion = compute_conclusion(length, speed, factor)
            self.conn.execute(
                """UPDATE tasks SET conclusion=?, conclusion_stale=0, version=version+1,
                   updated_at=? WHERE id=?""",
                (json.dumps(conclusion, ensure_ascii=False, sort_keys=True), now, t["id"]),
            )
            recomputed.append({"id": t["id"], "conclusion": conclusion})
        return recomputed

    # ---------- 资源与占用 ----------
    def create_resource(self, name: str, kind: str, total_quantity: float) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            try:
                cur = self.conn.execute(
                    """INSERT INTO resources(name, kind, total_quantity, created_at)
                       VALUES(?,?,?,?)""",
                    (name, kind, total_quantity, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("资源名称已存在") from exc
            resource_id = int(cur.lastrowid)
        return self.get_resource(resource_id)

    def get_resource(self, resource_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
        if row is None:
            raise NotFoundError("资源不存在")
        return self._resource(row)

    def get_resource_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM resources WHERE name=?", (name,)).fetchone()
        return self._resource(row) if row else None

    def list_resources(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM resources ORDER BY id").fetchall()
        return [self._resource(r) for r in rows]

    def _resource(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["available_quantity"] = available_resources(
            d["total_quantity"], self.active_occupancy_quantity(d["id"]))
        return d

    def active_occupancy_quantity(self, resource_id: int) -> float:
        with self._lock:
            row = self.conn.execute(
                """SELECT COALESCE(SUM(quantity),0) AS n FROM resource_occupancies
                   WHERE resource_id=? AND status='active'""",
                (resource_id,),
            ).fetchone()
        return float(row["n"])

    def create_occupancy(self, resource_id: int, quantity: float,
                         task_id: Optional[int], task_area_id: Optional[int],
                         occupied_by: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            try:
                cur = self.conn.execute(
                    """INSERT INTO resource_occupancies(resource_id, quantity, status,
                       task_id, task_area_id, occupied_by, created_at)
                       VALUES(?,?, 'active', ?, ?, ?, ?)""",
                    (resource_id, quantity, task_id, task_area_id, occupied_by, now),
                )
                occupancy_id = int(cur.lastrowid)
            except sqlite3.IntegrityError:
                winner = self.conn.execute(
                    """SELECT * FROM resource_occupancies
                       WHERE resource_id=? AND status='active'""",
                    (resource_id,),
                ).fetchone()
                res = self.conn.execute(
                    "SELECT * FROM resources WHERE id=?", (resource_id,)
                ).fetchone()
                occupied = winner["quantity"] if winner else 0.0
                available = available_resources(res["total_quantity"], occupied)
                raise OccupancyConflictError({
                    "winner_occupancy": dict(winner) if winner else None,
                    "available_after": available,
                    "requested_quantity": quantity,
                })
        return self.get_occupancy(occupancy_id)

    def get_occupancy(self, occupancy_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM resource_occupancies WHERE id=?", (occupancy_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("占用记录不存在")
        return dict(row)

    def list_occupancies(self, resource_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM resource_occupancies WHERE resource_id=?
                   ORDER BY id""",
                (resource_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def release_occupancy(self, occupancy_id: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE resource_occupancies SET status='released', released_at=?
                   WHERE id=? AND status='active'""",
                (now, occupancy_id),
            )
            if cur.rowcount == 0:
                raise ConflictError("占用已释放或不存在")
        return self.get_occupancy(occupancy_id)

    # ---------- 任务 ----------
    def create_task(self, item_id: int, title: str, description: str,
                    task_area_id: Optional[int], fire_line_id: Optional[int],
                    wind_id: Optional[int], status: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            conclusion = compute_conclusion(
                self._field_length(fire_line_id),
                self._field_speed(wind_id),
                self._field_factor(task_area_id),
            )
            cur = self.conn.execute(
                """INSERT INTO tasks(item_id, task_area_id, fire_line_id, wind_id,
                   title, description, status, conclusion, conclusion_stale, version,
                   created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?, ?, 0, 1, ?, ?, ?)""",
                (item_id, task_area_id, fire_line_id, wind_id, title, description,
                 status, json.dumps(conclusion, ensure_ascii=False, sort_keys=True),
                 actor, now, now),
            )
            task_id = int(cur.lastrowid)
        return self.get_task(task_id)

    def _field_length(self, fire_line_id):
        if not fire_line_id:
            return 0.0
        row = self.conn.execute(
            "SELECT length FROM fire_lines WHERE id=?", (fire_line_id,)
        ).fetchone()
        return row["length"] if row else 0.0

    def _field_speed(self, wind_id):
        if not wind_id:
            return 0.0
        row = self.conn.execute(
            "SELECT speed FROM wind_records WHERE id=?", (wind_id,)
        ).fetchone()
        return row["speed"] if row else 0.0

    def _field_factor(self, task_area_id):
        if not task_area_id:
            return 1.0
        row = self.conn.execute(
            "SELECT factor FROM task_areas WHERE id=?", (task_area_id,)
        ).fetchone()
        return row["factor"] if row else 1.0

    def get_task(self, task_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("任务不存在")
        return self._task(row)

    def list_tasks(self, item_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM tasks WHERE item_id=?"
        params: list = [item_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._task(r) for r in rows]

    @staticmethod
    def _task(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        if d.get("conclusion"):
            d["conclusion"] = json.loads(d["conclusion"])
        return d

    def close(self) -> None:
        with self._lock:
            self.conn.close()
