from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import (BATCH_STATUSES, ENTRY_OUTCOMES, OCCUPATION_STATUSES,
                     RESOURCE_KINDS, REVIEW_STATUSES, TASK_STATUSES,
                     TICKET_STATUSES, ConflictError, NotFoundError)
from .rules import (ENTITY_BATCH, ENTITY_RESOURCE, ENTITY_TASK, ENTITY_TICKET,
                    WIND_DIRECTIONS, ZONE_KINDS)


def _check(values) -> str:
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in values)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        if self.db_path not in (":memory:", ""):
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # check_same_thread=False + 进程内RLock：同一资源占用的检查与插入在同一临界区完成
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        # 测试用故障注入点，例如 fault_points={'merge_write'}
        self.fault_points: set = set()
        self._create_schema()

    def _create_schema(self) -> None:
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS resources (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ({_check(RESOURCE_KINDS)})),
                    capacity REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'available'
                        CHECK(status IN ('available','retired')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_no TEXT NOT NULL UNIQUE,
                    fireline_length_km REAL NOT NULL,
                    wind_direction TEXT NOT NULL CHECK(wind_direction IN ({_check(WIND_DIRECTIONS)})),
                    wind_speed_kmh REAL NOT NULL,
                    zone_kind TEXT NOT NULL CHECK(zone_kind IN ({_check(ZONE_KINDS)})),
                    zone_name TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ({_check(TICKET_STATUSES)})),
                    content_hash TEXT NOT NULL,
                    risk_json TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({_check(TASK_STATUSES)})),
                    conclusion_json TEXT,
                    basis_hash TEXT,
                    assigned_resource_code TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(ticket_id, name)
                );
                CREATE TABLE IF NOT EXISTS occupations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                    resource_code TEXT NOT NULL REFERENCES resources(code),
                    status TEXT NOT NULL CHECK(status IN ({_check(OCCUPATION_STATUSES)})),
                    occupied_by TEXT NOT NULL,
                    occupied_at TEXT NOT NULL,
                    released_at TEXT,
                    released_by TEXT
                );
                -- 同一资源同时只允许一笔有效占用，靠数据库约束兜底并发
                CREATE UNIQUE INDEX IF NOT EXISTS ux_active_occupation
                    ON occupations(resource_code) WHERE status='active';
                CREATE TABLE IF NOT EXISTS offline_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    notes_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL CHECK(status IN ({_check(BATCH_STATUSES)})),
                    last_error TEXT,
                    submitted_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    merged_at TEXT
                );
                CREATE TABLE IF NOT EXISTS batch_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES offline_batches(id) ON DELETE CASCADE,
                    ticket_no TEXT NOT NULL,
                    ticket_id INTEGER,
                    outcome TEXT NOT NULL CHECK(outcome IN ({_check(ENTRY_OUTCOMES)})),
                    occupied_json TEXT NOT NULL DEFAULT '[]',
                    blocked_json TEXT NOT NULL DEFAULT '[]',
                    detail_json TEXT NOT NULL DEFAULT '{{}}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                    proposed_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({_check(REVIEW_STATUSES)})),
                    submitted_by TEXT NOT NULL,
                    resolution_note TEXT NOT NULL DEFAULT '',
                    resolved_by TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_review_pending
                    ON reviews(ticket_id) WHERE status='pending';
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    # ---------- 基础映射 ----------
    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row is not None else None

    @staticmethod
    def _ticket(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["risk"] = json.loads(item.pop("risk_json"))
        return item

    @staticmethod
    def _task(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["conclusion"] = json.loads(item["conclusion_json"]) if item["conclusion_json"] else None
        item.pop("conclusion_json", None)
        return item

    @staticmethod
    def _batch(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["notes"] = json.loads(item.pop("notes_json"))
        return item

    @staticmethod
    def _entry(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["occupied"] = json.loads(item.pop("occupied_json"))
        item["blocked"] = json.loads(item.pop("blocked_json"))
        item["detail"] = json.loads(item.pop("detail_json"))
        return item

    @staticmethod
    def _review(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["proposed"] = json.loads(item.pop("proposed_json"))
        return item

    # ---------- 审计 ----------
    def _audit_inside(self, conn, action: str, entity_type: str, entity_id: Any,
                      actor: str, detail: dict, now: str) -> None:
        row = conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous = row["entry_hash"] if row else "GENESIS"
        event = make_entry(action, entity_type, str(entity_id), actor, detail, previous)
        event["created_at"] = now
        conn.execute(
            """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
               previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (event["action"], event["entity_type"], event["entity_id"],
             event["actor"],
             json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
             event["previous_hash"], event["entry_hash"], now),
        )

    def append_audit(self, action: str, entity_type: str, entity_id: Any,
                     actor: str, detail: dict) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self._audit_inside(self.conn, action, entity_type, entity_id,
                               actor, detail, now)
        return {"action": action, "entity_type": entity_type, "entity_id": entity_id}

    def list_audit(self, entity_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (str(entity_id),)
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

    # ---------- 资源 ----------
    def create_resource(self, code: str, name: str, kind: str, capacity: float,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                self.conn.execute(
                    """INSERT INTO resources(code, name, kind, capacity, status,
                       created_by, created_at) VALUES(?,?,?,?,'available',?,?)""",
                    (code, name, kind, capacity, actor, now),
                )
                self._audit_inside(self.conn, "register_resource", ENTITY_RESOURCE,
                                   code, actor, {"name": name, "kind": kind}, now)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("资源编号已存在") from exc
        return self.get_resource(code)

    def get_resource(self, code: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM resources WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("资源不存在")
        return dict(row)

    def list_resources(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM resources ORDER BY code").fetchall()
        return [dict(row) for row in rows]

    def active_resource_codes(self) -> set:
        with self._lock:
            rows = self.conn.execute(
                "SELECT resource_code FROM occupations WHERE status='active'"
            ).fetchall()
        return {row["resource_code"] for row in rows}

    # ---------- 直接占用/释放（单笔入口） ----------
    def occupy(self, ticket_id: int, resource_code: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            conn = self.conn
            holder = conn.execute(
                """SELECT o.*, t.ticket_no FROM occupations o
                   JOIN tickets t ON t.id=o.ticket_id
                   WHERE o.resource_code=? AND o.status='active'""",
                (resource_code,),
            ).fetchone()
            if holder is not None:
                if holder["ticket_id"] == ticket_id:
                    return dict(holder)
                raise ConflictError(
                    "资源已被其他现场单号占用",
                    {"resource_code": resource_code,
                     "holder": {"ticket_no": holder["ticket_no"],
                                "occupied_by": holder["occupied_by"]}},
                )
            cur = conn.execute(
                """INSERT INTO occupations(ticket_id, resource_code, status,
                   occupied_by, occupied_at) VALUES(?,?,'active',?,?)""",
                (ticket_id, resource_code, actor, now),
            )
            conn.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now, ticket_id))
            self._audit_inside(conn, "occupy", ENTITY_RESOURCE, resource_code, actor,
                               {"ticket_id": ticket_id}, now)
            occupation_id = int(cur.lastrowid)
            row = conn.execute("SELECT * FROM occupations WHERE id=?", (occupation_id,)).fetchone()
            return dict(row)

    def release(self, occupation_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            conn = self.conn
            row = conn.execute("SELECT * FROM occupations WHERE id=?", (occupation_id,)).fetchone()
            if row is None:
                raise NotFoundError("占用记录不存在")
            if row["status"] == "released":
                return dict(row)
            conn.execute(
                "UPDATE occupations SET status='released', released_at=?, released_by=? WHERE id=?",
                (now, actor, occupation_id),
            )
            conn.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now, row["ticket_id"]))
            self._audit_inside(conn, "release", ENTITY_RESOURCE, row["resource_code"],
                               actor, {"ticket_id": row["ticket_id"]}, now)
            fresh = conn.execute("SELECT * FROM occupations WHERE id=?", (occupation_id,)).fetchone()
            return dict(fresh)

    def list_occupations(self, ticket_no: Optional[str] = None,
                         status: str = "active") -> List[Dict[str, Any]]:
        sql = ("SELECT o.*, t.ticket_no FROM occupations o "
               "JOIN tickets t ON t.id=o.ticket_id")
        where, params = [], []
        if status:
            where.append("o.status=?")
            params.append(status)
        if ticket_no:
            where.append("t.ticket_no=?")
            params.append(ticket_no)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY o.id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ---------- 现场单号与任务 ----------
    def get_ticket(self, ticket_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM tickets WHERE ticket_no=?",
                                    (ticket_no,)).fetchone()
        if row is None:
            raise NotFoundError("现场单号不存在")
        return self._ticket(row)

    def list_tickets(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM tickets ORDER BY id").fetchall()
        return [self._ticket(row) for row in rows]

    def list_tasks(self, ticket_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM tasks WHERE ticket_id=? ORDER BY id", (ticket_id,)
            ).fetchall()
        return [self._task(row) for row in rows]

    def get_task(self, task_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("任务不存在")
        return self._task(row)

    def transition_task(self, task_id: int, status: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            conn = self.conn
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise NotFoundError("任务不存在")
            conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                         (status, now, task_id))
            self._audit_inside(conn, "task_transition", ENTITY_TASK, task_id, actor,
                               {"from": row["status"], "to": status}, now)
            fresh = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return self._task(fresh)

    def add_task(self, ticket_id: int, name: str, status: str,
                 conclusion: dict, basis_hash: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                conn = self.conn
                cur = conn.execute(
                    """INSERT INTO tasks(ticket_id, name, status, conclusion_json,
                       basis_hash, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (ticket_id, name, status, json.dumps(conclusion, ensure_ascii=False),
                     basis_hash, actor, now, now),
                )
                self._audit_inside(conn, "add_task", ENTITY_TASK, int(cur.lastrowid),
                                   actor, {"ticket_id": ticket_id, "name": name}, now)
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,)).fetchone()
                return self._task(row)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("任务名在该单号下已存在") from exc

    def close_ticket(self, ticket_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            conn = self.conn
            conn.execute(
                "UPDATE tickets SET status='closed', closed_at=?, updated_at=? WHERE id=?",
                (now, now, ticket_id),
            )
            self._audit_inside(conn, "close_ticket", ENTITY_TICKET, ticket_id, actor,
                               {}, now)
            row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            return self._ticket(row)

    # ---------- 离线批次 ----------
    def create_batch(self, submitted_by: str, payload: dict,
                     notes: Optional[list] = None) -> Dict[str, Any]:
        now = utc_now()
        batch_no = "BATCH-" + uuid.uuid4().hex[:12].upper()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO offline_batches(batch_no, payload_json, notes_json,
                   status, submitted_by, created_at)
                   VALUES(?,?,?, 'received', ?,?)""",
                (batch_no, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 json.dumps(notes or [], ensure_ascii=False), submitted_by, now),
            )
            batch_id = int(cur.lastrowid)
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM offline_batches WHERE id=?",
                                    (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("离线批次不存在")
        item = self._batch(row)
        item["entries"] = self.list_entries(batch_id)
        return item

    def get_batch_by_no(self, batch_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM offline_batches WHERE batch_no=?",
                                    (batch_no,)).fetchone()
        if row is None:
            raise NotFoundError("离线批次不存在")
        return self.get_batch(int(row["id"]))

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM offline_batches ORDER BY id").fetchall()
        return [self.get_batch(int(row["id"])) for row in rows]

    def list_entries(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM batch_entries WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        return [self._entry(row) for row in rows]

    def _try_occupy_inside(self, conn, ticket_id: int, resource_code: str,
                           actor: str, now: str) -> Dict[str, Any]:
        """在合并事务内部判定资源胜负：同一单号重复占用视为沿用，他单占用即落败。"""
        holder = conn.execute(
            """SELECT o.*, t.ticket_no FROM occupations o
               JOIN tickets t ON t.id=o.ticket_id
               WHERE o.resource_code=? AND o.status='active'""",
            (resource_code,),
        ).fetchone()
        if holder is not None:
            if holder["ticket_id"] == ticket_id:
                return {"won": True, "reused": True, "code": resource_code}
            return {"won": False, "code": resource_code,
                    "holder": {"ticket_no": holder["ticket_no"],
                               "occupied_by": holder["occupied_by"]}}
        conn.execute(
            """INSERT INTO occupations(ticket_id, resource_code, status,
               occupied_by, occupied_at) VALUES(?,?,'active',?,?)""",
            (ticket_id, resource_code, actor, now),
        )
        return {"won": True, "reused": False, "code": resource_code}

    def merge_entries(self, batch_id: int, prepared: List[Dict[str, Any]],
                      actor: str) -> Dict[str, Any]:
        """资源占用、任务状态与离线条目在同一事务内入库。

        任一写入失败则整笔回滚（占用一并回滚），批次保留为pending_retry，下次用保留的
        原始payload重试。返回 {'status': ..., 'results': [...]}。
        """
        now = utc_now()
        results: List[Dict[str, Any]] = []
        try:
            with self._lock, self.conn:
                conn = self.conn
                for entry in prepared:
                    result = self._merge_one(conn, entry, actor, now)
                    results.append(result)
                conn.execute(
                    "UPDATE offline_batches SET status='merged', merged_at=?, last_error=NULL WHERE id=?",
                    (now, batch_id),
                )
                if "merge_write" in self.fault_points:
                    # 模拟事务提交前的存储故障：with块退出时整体回滚
                    raise sqlite3.OperationalError("injected write failure")
        except sqlite3.Error as exc:
            with self._lock, self.conn:
                self.conn.execute(
                    "UPDATE offline_batches SET status='pending_retry', last_error=? WHERE id=?",
                    (str(exc), batch_id),
                )
            return {"status": "pending_retry", "results": [], "error": str(exc)}
        return {"status": "merged", "results": results}

    def _merge_one(self, conn, entry: Dict[str, Any], actor: str,
                   now: str) -> Dict[str, Any]:
        no = entry["ticket_no"]
        row = conn.execute("SELECT * FROM tickets WHERE ticket_no=?", (no,)).fetchone()
        result: Dict[str, Any] = {"ticket_no": no, "outcome": None,
                                  "ticket_id": None, "occupied": [],
                                  "blocked": [], "tasks": []}
        if row is None:
            cur = conn.execute(
                """INSERT INTO tickets(ticket_no, fireline_length_km, wind_direction,
                   wind_speed_kmh, zone_kind, zone_name, note, status, content_hash,
                   risk_json, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,'active',?,?,?,?,?)""",
                (no, entry["fireline_length_km"], entry["wind_direction"],
                 entry["wind_speed_kmh"], entry["zone_kind"], entry["zone_name"],
                 entry["note"], entry["content_hash"],
                 json.dumps(entry["risk"], ensure_ascii=False), actor, now, now),
            )
            ticket_id = int(cur.lastrowid)
            for task in entry["tasks"]:
                tcur = conn.execute(
                    """INSERT INTO tasks(ticket_id, name, status, conclusion_json,
                       basis_hash, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (ticket_id, task["name"], task["status"],
                     json.dumps(task["conclusion"], ensure_ascii=False),
                     task["basis_hash"], actor, now, now),
                )
                result["tasks"].append({"id": int(tcur.lastrowid), "name": task["name"],
                                        "status": task["status"]})
            result["outcome"] = "applied"
            self._audit_inside(conn, "apply_ticket", ENTITY_TICKET, ticket_id, actor,
                               {"ticket_no": no, "source_batch": entry.get("batch_no")}, now)
        elif row["content_hash"] == entry["content_hash"]:
            # 同一单号重复回传且内容一致：沿用首次结果，正式记录不动
            ticket_id = int(row["id"])
            result["outcome"] = "reused"
            self._audit_inside(conn, "reuse_ticket", ENTITY_TICKET, ticket_id, actor,
                               {"ticket_no": no}, now)
        else:
            # 内容不同：不覆盖正式记录，挂待核对（每单仅保留一条pending）
            ticket_id = int(row["id"])
            pending = conn.execute(
                "SELECT 1 FROM reviews WHERE ticket_id=? AND status='pending'",
                (ticket_id,),
            ).fetchone()
            if pending is None:
                conn.execute(
                    """INSERT INTO reviews(ticket_id, proposed_json, status,
                       submitted_by, created_at) VALUES(?,?,'pending',?,?)""",
                    (ticket_id, json.dumps(entry["proposed"], ensure_ascii=False),
                     actor, now),
                )
            result["outcome"] = "needs_review"
            self._audit_inside(conn, "review_ticket", ENTITY_TICKET, ticket_id, actor,
                               {"ticket_no": no}, now)
            conn.execute(
                """INSERT INTO batch_entries(batch_id, ticket_no, ticket_id, outcome,
                   occupied_json, blocked_json, detail_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (entry["batch_id"], no, ticket_id, result["outcome"],
                 json.dumps([]), json.dumps([]),
                 json.dumps({"reason": "content differs from formal record"},
                            ensure_ascii=False), now),
            )
            return result

        # applied/reused 才参与资源争夺（落败不影响单号其余内容的入库结果）
        for code in entry["occupies"]:
            verdict = self._try_occupy_inside(conn, ticket_id, code, actor, now)
            if verdict["won"]:
                result["occupied"].append(code)
            else:
                result["blocked"].append({"resource_code": code,
                                          "holder": verdict["holder"]})

        conn.execute(
            """INSERT INTO batch_entries(batch_id, ticket_no, ticket_id, outcome,
               occupied_json, blocked_json, detail_json, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (entry["batch_id"], no, ticket_id, result["outcome"],
             json.dumps(result["occupied"]),
             json.dumps(result["blocked"], ensure_ascii=False),
             json.dumps({"tasks": result["tasks"]}, ensure_ascii=False), now),
        )
        return result

    def apply_ticket_update(self, ticket_id: int, fields: Dict[str, Any],
                            tasks: List[Dict[str, Any]], occupies: List[str],
                            actor: str, source: str) -> Dict[str, Any]:
        """火线长度/风向/任务区改动后的统一落库：更新正式记录并重算关联任务结论。

        与资源占用同一事务；其他单号不受影响。
        """
        now = utc_now()
        with self._lock, self.conn:
            conn = self.conn
            conn.execute(
                """UPDATE tickets SET fireline_length_km=?, wind_direction=?,
                   wind_speed_kmh=?, zone_kind=?, zone_name=?, note=?, content_hash=?,
                   risk_json=?, updated_at=? WHERE id=?""",
                (fields["fireline_length_km"], fields["wind_direction"],
                 fields["wind_speed_kmh"], fields["zone_kind"], fields["zone_name"],
                 fields["note"], fields["content_hash"],
                 json.dumps(fields["risk"], ensure_ascii=False), now, ticket_id),
            )
            existing = conn.execute("SELECT * FROM tasks WHERE ticket_id=? ORDER BY id",
                                    (ticket_id,)).fetchall()
            by_name = {r["name"]: r for r in existing}
            recalculated, created = [], []
            for task in tasks:
                if task["name"] in by_name:
                    old = by_name[task["name"]]
                    new_status = "active" if old["status"] == "done" else old["status"]
                    conn.execute(
                        """UPDATE tasks SET status=?, conclusion_json=?, basis_hash=?,
                           updated_at=? WHERE id=?""",
                        (new_status, json.dumps(task["conclusion"], ensure_ascii=False),
                         task["basis_hash"], now, old["id"]),
                    )
                    recalculated.append({"id": old["id"], "name": task["name"],
                                         "from_status": old["status"],
                                         "to_status": new_status})
                else:
                    cur = conn.execute(
                        """INSERT INTO tasks(ticket_id, name, status, conclusion_json,
                           basis_hash, created_by, created_at, updated_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (ticket_id, task["name"], task["status"],
                         json.dumps(task["conclusion"], ensure_ascii=False),
                         task["basis_hash"], actor, now, now),
                    )
                    created.append(int(cur.lastrowid))
            blocked, occupied = [], []
            for code in occupies:
                verdict = self._try_occupy_inside(conn, ticket_id, code, actor, now)
                (occupied if verdict["won"] else blocked).append(
                    code if verdict["won"] else
                    {"resource_code": code, "holder": verdict["holder"]})
            self._audit_inside(conn, "amend_ticket", ENTITY_TICKET, ticket_id, actor,
                               {"source": source, "recalculated": recalculated,
                                "created_tasks": created,
                                "changed_fields": fields["changed_fields"]}, now)
            return {"recalculated": recalculated, "created_tasks": created,
                    "occupied": occupied, "blocked": blocked}

    # ---------- 待核对 ----------
    def list_reviews(self, status: str = "pending") -> List[Dict[str, Any]]:
        sql = ("SELECT r.*, t.ticket_no FROM reviews r "
               "JOIN tickets t ON t.id=r.ticket_id")
        params: tuple = ()
        if status:
            sql += " WHERE r.status=?"
            params = (status,)
        sql += " ORDER BY r.id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._review(row) for row in rows]

    def get_review(self, review_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT r.*, t.ticket_no FROM reviews r JOIN tickets t ON t.id=r.ticket_id WHERE r.id=?",
                (review_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("待核对记录不存在")
        return self._review(row)

    def resolve_review(self, review_id: int, status: str, actor: str,
                       note: str = "") -> None:
        now = utc_now()
        with self._lock, self.conn:
            conn = self.conn
            row = conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            if row is None:
                raise NotFoundError("待核对记录不存在")
            if row["status"] != "pending":
                raise ConflictError("该待核对记录已处理")
            conn.execute(
                """UPDATE reviews SET status=?, resolved_by=?, resolved_at=?,
                   resolution_note=? WHERE id=?""",
                (status, actor, now, note, review_id),
            )
            self._audit_inside(conn, "resolve_review", ENTITY_TICKET,
                               row["ticket_id"], actor, {"result": status, "note": note},
                               now)

    def register_ticket_direct(self, prepared: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """在线直接登记入口：原子建单/任务/占用；任一资源被占则整笔不成立。"""
        now = utc_now()
        try:
            with self._lock, self.conn:
                conn = self.conn
                blocked = []
                for code in prepared["occupies"]:
                    holder = conn.execute(
                        """SELECT o.ticket_id, t.ticket_no, o.occupied_by FROM occupations o
                           JOIN tickets t ON t.id=o.ticket_id
                           WHERE o.resource_code=? AND o.status='active'""",
                        (code,),
                    ).fetchone()
                    if holder is not None:
                        blocked.append({"resource_code": code,
                                        "holder": {"ticket_no": holder["ticket_no"],
                                                   "occupied_by": holder["occupied_by"]}})
                if blocked:
                    raise ConflictError("资源已被其他现场单号占用",
                                        {"blocked": blocked})
                cur = conn.execute(
                    """INSERT INTO tickets(ticket_no, fireline_length_km, wind_direction,
                       wind_speed_kmh, zone_kind, zone_name, note, status, content_hash,
                       risk_json, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,'active',?,?,?,?,?)""",
                    (prepared["ticket_no"], prepared["fireline_length_km"],
                     prepared["wind_direction"], prepared["wind_speed_kmh"],
                     prepared["zone_kind"], prepared["zone_name"], prepared["note"],
                     prepared["content_hash"],
                     json.dumps(prepared["risk"], ensure_ascii=False), actor, now, now),
                )
                ticket_id = int(cur.lastrowid)
                tasks = []
                for task in prepared["tasks"]:
                    tcur = conn.execute(
                        """INSERT INTO tasks(ticket_id, name, status, conclusion_json,
                           basis_hash, created_by, created_at, updated_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (ticket_id, task["name"], task["status"],
                         json.dumps(task["conclusion"], ensure_ascii=False),
                         task["basis_hash"], actor, now, now),
                    )
                    tasks.append({"id": int(tcur.lastrowid), "name": task["name"],
                                  "status": task["status"]})
                for code in prepared["occupies"]:
                    conn.execute(
                        """INSERT INTO occupations(ticket_id, resource_code, status,
                           occupied_by, occupied_at) VALUES(?,?,'active',?,?)""",
                        (ticket_id, code, actor, now),
                    )
                self._audit_inside(conn, "register_ticket", ENTITY_TICKET, ticket_id,
                                   actor, {"ticket_no": prepared["ticket_no"]}, now)
        except sqlite3.IntegrityError as exc:
            placeholders = ",".join("?" * len(prepared["occupies"]))
            holder_rows = self.conn.execute(
                f"""SELECT o.resource_code, t.ticket_no, o.occupied_by FROM occupations o
                    JOIN tickets t ON t.id=o.ticket_id
                    WHERE o.status='active' AND o.resource_code IN ({placeholders})""",
                prepared["occupies"],
            ).fetchall() if prepared["occupies"] else []
            if holder_rows:
                blocked = [{"resource_code": r["resource_code"],
                            "holder": {"ticket_no": r["ticket_no"],
                                       "occupied_by": r["occupied_by"]}}
                           for r in holder_rows]
                raise ConflictError("资源已被其他现场单号占用",
                                    {"blocked": blocked}) from exc
            raise ConflictError("现场单号已存在，不能覆盖正式记录") from exc
        return self.get_ticket(prepared["ticket_no"])

    def close(self) -> None:
        with self._lock:
            self.conn.close()
