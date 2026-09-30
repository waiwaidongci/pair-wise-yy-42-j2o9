import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from src.domain import OccupancyConflictError
from src.http_api import make_handler
from src.repository import Repository
from src.service import Service


class OfflineBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "batch item", "description": "offline merge",
             "severity": "high", "quantity": 5, "threshold": 10,
             "external_ref": "BATCH-ITEM"},
            "creator", "field_commander")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _payload(self, order_no, resource_name=None):
        occ = []
        if resource_name:
            occ = [{"resource_name": resource_name, "quantity": 2,
                    "task_area_name": "A区"}]
        return {
            "order_no": order_no,
            "item_id": self.item["id"],
            "fire_lines": [{"name": "东线", "length": 100}],
            "wind_records": [{"name": "主风", "direction": "东南", "speed": 5}],
            "task_areas": [{"name": "A区", "description": "山脚", "factor": 1.0}],
            "tasks": [{"title": "开辟隔离带", "task_area_name": "A区",
                       "fire_line_name": "东线", "wind_name": "主风",
                       "status": "pending"}],
            "resource_occupancies": occ,
        }

    def test_batch_merge_idempotent(self):
        self.service.create_resource({"name": "消防车", "kind": "vehicle", "total_quantity": 10}, "logistics", "logistics")
        payload = self._payload("B-001", "消防车")
        first = self.service.submit_batch(payload, "dispatcher", "field_commander")
        self.assertEqual(first["outcome"], "merged")
        # 同一单号重复回传：沿用首次结果，不重复建
        second = self.service.submit_batch(payload, "dispatcher", "field_commander")
        self.assertEqual(second["outcome"], "idempotent")
        self.assertEqual(second["batch"]["id"], first["batch"]["id"])
        # 正式记录只建一份
        self.assertEqual(len(self.service.list_fire_lines(self.item["id"], "viewer")), 1)
        self.assertEqual(len(self.service.list_tasks(self.item["id"], "viewer")), 1)

    def test_batch_content_conflict_pending_review(self):
        self.service.create_resource({"name": "消防车", "kind": "vehicle", "total_quantity": 10}, "logistics", "logistics")
        payload = self._payload("B-002", "消防车")
        first = self.service.submit_batch(payload, "dispatcher", "field_commander")
        self.assertEqual(first["outcome"], "merged")
        # 同一单号内容不同：留下待核对，不覆盖正式记录
        changed = self._payload("B-002", "消防车")
        changed["fire_lines"] = [{"name": "东线", "length": 999}]
        conflict = self.service.submit_batch(changed, "dispatcher", "field_commander")
        self.assertEqual(conflict["outcome"], "conflict")
        self.assertEqual(conflict["batch"]["status"], "conflict")
        self.assertIsNotNone(conflict["batch"]["original_batch_id"])
        # 正式记录未被覆盖
        fl = self.service.list_fire_lines(self.item["id"], "viewer")
        self.assertEqual(fl[0]["length"], 100)
        # 待核对批次可查
        reviews = self.service.list_batches("viewer", status="conflict")
        self.assertTrue(any(r["id"] == conflict["batch"]["id"] for r in reviews))

    def test_batch_occupancy_conflict_loser_gets_winner(self):
        self.service.create_resource({"name": "消防车", "kind": "vehicle", "total_quantity": 10}, "logistics", "logistics")
        # 同一批次内两笔相同资源占用：一笔成立，落败方拿到占用对象并重算可用资源
        payload = self._payload("B-003", "消防车")
        payload["resource_occupancies"] = [
            {"resource_name": "消防车", "quantity": 2, "task_area_name": "A区"},
            {"resource_name": "消防车", "quantity": 3, "task_area_name": "A区"},
        ]
        result = self.service.submit_batch(payload, "dispatcher", "field_commander")
        self.assertEqual(result["outcome"], "merged")
        conflicts = result["batch"]["result"]["resource_conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertIsNotNone(conflicts[0]["winner_occupancy"])
        self.assertEqual(conflicts[0]["available_after"], 10 - 2)
        self.assertEqual(conflicts[0]["requested_quantity"], 3)
        # 资源可用量按赢家占用重算
        resources = self.service.list_resources("viewer")
        res = next(r for r in resources if r["name"] == "消防车")
        self.assertEqual(res["available_quantity"], 8)

    def test_write_failure_rollback_and_retry(self):
        # 资源不存在：合并失败，占用与任务回滚，批次保留待重试
        payload = self._payload("B-004", "不存在的资源")
        result = self.service.submit_batch(payload, "dispatcher", "field_commander")
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["batch"]["status"], "failed")
        # 回滚：火线、任务均未入库
        self.assertEqual(len(self.service.list_fire_lines(self.item["id"], "viewer")), 0)
        self.assertEqual(len(self.service.list_tasks(self.item["id"], "viewer")), 0)
        # 补上资源后重试成功
        self.service.create_resource({"name": "不存在的资源", "kind": "vehicle", "total_quantity": 10}, "logistics", "logistics")
        retried = self.service.retry_batch("B-004", "dispatcher", "field_commander")
        self.assertEqual(retried["outcome"], "merged")
        self.assertEqual(retried["batch"]["status"], "merged")
        self.assertEqual(len(self.service.list_fire_lines(self.item["id"], "viewer")), 1)

    def test_concurrent_occupancy_one_winner(self):
        self.service.create_resource({"name": "消防车", "kind": "vehicle", "total_quantity": 10}, "logistics", "logistics")
        barrier = threading.Barrier(2)
        outcomes = []

        def occupy():
            barrier.wait()
            try:
                self.service.create_occupancy(
                    1, {"quantity": 2}, "dispatcher", "field_commander")
                outcomes.append("ok")
            except OccupancyConflictError as exc:
                outcomes.append(("conflict", exc.details))

        threads = [threading.Thread(target=occupy) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("ok"), 1)
        conflict = next(o for o in outcomes if o != "ok")
        self.assertIsNotNone(conflict[1]["winner_occupancy"])
        self.assertEqual(conflict[1]["available_after"], 8)


class TaskConclusionInvalidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "inv item", "description": "invalidation",
             "severity": "high", "quantity": 5, "threshold": 10,
             "external_ref": "INV-ITEM"},
            "creator", "field_commander")
        self.fl = self.service.create_fire_line(
            self.item["id"], {"name": "东线", "length": 100}, "dispatcher", "field_commander")
        self.wind = self.service.create_wind_record(
            self.item["id"], {"name": "主风", "direction": "东南", "speed": 5},
            "dispatcher", "field_commander")
        self.area = self.service.create_task_area(
            self.item["id"], {"name": "A区", "factor": 1.0}, "dispatcher", "field_commander")
        self.task = self.service.create_task(
            self.item["id"], {"title": "任务一", "task_area_id": self.area["id"],
                              "fire_line_id": self.fl["id"], "wind_id": self.wind["id"]},
            "dispatcher", "field_commander")
        self.other = self.service.create_task(
            self.item["id"], {"title": "任务二（不关联）"}, "dispatcher", "field_commander")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_fire_line_change_recomputes_linked_only(self):
        before = self.service.get_task(self.task["id"], "viewer")["conclusion"]
        other_before = self.service.get_task(self.other["id"], "viewer")["conclusion"]
        self.assertEqual(before["risk"], "moderate")
        result = self.service.update_fire_line(
            self.fl["id"], {"length": 300}, "dispatcher", "field_commander")
        self.assertEqual(len(result["recomputed_tasks"]), 1)
        after = self.service.get_task(self.task["id"], "viewer")["conclusion"]
        self.assertNotEqual(after["score"], before["score"])
        # 其他任务照常：结论不重算
        other_after = self.service.get_task(self.other["id"], "viewer")["conclusion"]
        self.assertEqual(other_after, other_before)
        self.assertEqual(self.service.get_task(self.other["id"], "viewer")["conclusion_stale"], 0)

    def test_wind_change_recomputes_linked(self):
        before = self.service.get_task(self.task["id"], "viewer")["conclusion"]
        result = self.service.update_wind_record(
            self.wind["id"], {"direction": "西北", "speed": 20}, "dispatcher", "field_commander")
        self.assertEqual(len(result["recomputed_tasks"]), 1)
        after = self.service.get_task(self.task["id"], "viewer")["conclusion"]
        self.assertNotEqual(after["score"], before["score"])
        self.assertEqual(after["risk"], "extreme")

    def test_task_area_change_recomputes_linked(self):
        before = self.service.get_task(self.task["id"], "viewer")["conclusion"]
        result = self.service.update_task_area(
            self.area["id"], {"factor": 3.0}, "dispatcher", "field_commander")
        self.assertEqual(len(result["recomputed_tasks"]), 1)
        after = self.service.get_task(self.task["id"], "viewer")["conclusion"]
        self.assertNotEqual(after["score"], before["score"])


class HttpOccupancyConflictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.service.create_resource({"name": "消防车", "kind": "vehicle", "total_quantity": 10}, "logistics", "logistics")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.service, str(Path(__file__).resolve().parent.parent / "static")))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.repo.close()
        self.tmp.cleanup()

    def _post(self, path, body, actor="dispatcher", role="field_commander"):
        req = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Actor": actor, "X-Role": role},
            method="POST")
        try:
            with urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_second_occupancy_returns_409_with_winner(self):
        status, _ = self._post("/api/resources/1/occupancies", {"quantity": 2})
        self.assertEqual(status, 201)
        status, body = self._post("/api/resources/1/occupancies", {"quantity": 3})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "OccupancyConflictError")
        self.assertIsNotNone(body["details"]["winner_occupancy"])
        self.assertEqual(body["details"]["available_after"], 8)


if __name__ == "__main__":
    unittest.main()
