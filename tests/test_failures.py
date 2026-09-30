import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


def entry(ticket_no, **overrides):
    payload = {
        "ticket_no": ticket_no,
        "fireline_length_km": 3.0,
        "wind_direction": "N",
        "wind_speed_kmh": 10,
        "zone_kind": "forest",
        "zone_name": "东坡",
        "tasks": [{"name": "巡线", "status": "active"}],
        "occupy_resources": [],
    }
    payload.update(overrides)
    return payload


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.service.register_resource(
            {"code": "E1", "name": "一号车", "kind": "engine"}, "log", "logistics")
        self.service.register_resource(
            {"code": "E2", "name": "二号车", "kind": "engine"}, "log", "logistics")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_permission_matrix(self):
        with self.assertRaises(PermissionDenied):
            self.service.submit_batch({"entries": [entry("WF-1")]},
                                      "x", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.register_resource(
                {"code": "X", "name": "x", "kind": "engine"}, "x", "viewer")
        self.service.submit_batch({"entries": [entry("WF-1")]}, "disp",
                                  "field_commander")
        reviews = self.service.list_reviews("viewer")  # viewer可查看
        self.assertEqual(reviews, [])
        with self.assertRaises(PermissionDenied):
            self.service.resolve_review(1, {"decision": "reject"},
                                        "disp", "field_commander")

    def test_validation_errors(self):
        with self.assertRaises(ValidationError):
            self.service.submit_batch({"entries": []}, "disp", "field_commander")
        with self.assertRaises(ValidationError):
            self.service.submit_batch(
                {"entries": [entry("WF-1", wind_direction="UP")]},
                "disp", "field_commander")
        with self.assertRaises(ValidationError):
            self.service.submit_batch(
                {"entries": [entry("WF-1", occupy_resources=["NOPE"])]},
                "disp", "field_commander")

    def test_concurrent_occupation_single_winner(self):
        self.service.submit_batch({"entries": [entry("WF-1")]}, "disp1",
                                  "field_commander")
        self.service.submit_batch({"entries": [entry("WF-2")]}, "disp2",
                                  "field_commander")
        outcomes = []

        def grab(actor, ticket_no):
            try:
                outcomes.append(("won", self.service.occupy(
                    ticket_no, {"occupy_resources": ["E1"]}, actor,
                    "field_commander")))
            except ConflictError as exc:
                outcomes.append(("lost", exc.payload))

        t1 = threading.Thread(target=grab, args=("disp1", "WF-1"))
        t2 = threading.Thread(target=grab, args=("disp2", "WF-2"))
        t1.start(); t2.start(); t1.join(); t2.join()

        results = {outcome[0] for outcome in outcomes}
        self.assertEqual(results, {"won", "lost"})
        active = self.repo.list_occupations(status="active")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["resource_code"], "E1")
        lost = next(outcome[1] for outcome in outcomes if outcome[0] == "lost")
        # 落败方拿到占用对象
        self.assertEqual(lost["blocked"][0]["resource_code"], "E1")
        self.assertIn(lost["blocked"][0]["holder"]["ticket_no"], ("WF-1", "WF-2"))
        # 并拿到重新计算后的可用资源（E2仍可用，E1不在其中）
        codes = {r["code"] for r in lost["available_resources"]}
        self.assertIn("E2", codes)
        self.assertNotIn("E1", codes)

    def test_batch_merge_failure_rolls_back_and_retains_for_retry(self):
        self.repo.fault_points.add("merge_write")
        failed = self.service.submit_batch(
            {"entries": [entry("WF-9", occupy_resources=["E1"])]},
            "disp", "field_commander")
        self.assertEqual(failed["status"], "pending_retry")
        self.assertTrue(failed["retryable"])
        # 回滚：单号未建立、占用未成立、条目未落库
        from src.domain import NotFoundError
        with self.assertRaises(NotFoundError):
            self.repo.get_ticket("WF-9")
        self.assertEqual(self.repo.list_occupations(status="active"), [])
        self.assertEqual(failed["entries"], [])
        # 原始payload保留在批次中可供下次重试
        self.assertEqual(failed["payload"]["entries"][0]["ticket_no"], "WF-9")

        self.repo.fault_points.discard("merge_write")
        retried = self.service.retry_batch(failed["batch_no"], "disp",
                                           "field_commander")
        self.assertEqual(retried["status"], "merged")
        self.assertEqual(retried["outcome_counts"], {"applied": 1})
        ticket = self.service.get_ticket_view("WF-9", "viewer")
        self.assertEqual({o["resource_code"] for o in ticket["occupations"]}, {"E1"})
        self.assertTrue(self.repo.verify_audit_chain())

    def test_retry_only_for_pending_batch(self):
        batch = self.service.submit_batch({"entries": [entry("WF-1")]}, "disp",
                                          "field_commander")
        with self.assertRaises(ConflictError):
            self.service.retry_batch(batch["batch_no"], "disp", "field_commander")

    def test_direct_register_conflict_is_atomic(self):
        self.service.register_ticket(entry("WF-1", occupy_resources=["E1"]),
                                     "disp1", "field_commander")
        with self.assertRaises(ConflictError) as ctx:
            self.service.register_ticket(entry("WF-2", occupy_resources=["E1"]),
                                         "disp2", "field_commander")
        self.assertEqual(ctx.exception.payload["blocked"][0]["resource_code"], "E1")
        self.assertEqual(len(self.service.list_tickets("viewer")), 1)
        with self.assertRaises(ConflictError):
            self.service.register_ticket(entry("WF-1"), "disp3", "field_commander")

    def test_same_ticket_occupy_twice_is_idempotent(self):
        self.service.submit_batch({"entries": [entry("WF-1")]}, "disp",
                                  "field_commander")
        first = self.service.occupy("WF-1", {"occupy_resources": ["E1"]},
                                    "disp", "field_commander")
        second = self.service.occupy("WF-1", {"occupy_resources": ["E1"]},
                                     "disp", "field_commander")
        self.assertEqual(first["occupied"], second["occupied"])
        self.assertEqual(len(self.repo.list_occupations(status="active")), 1)

    def test_unknown_paths_and_entities(self):
        from src.domain import NotFoundError
        with self.assertRaises(NotFoundError):
            self.service.get_ticket_view("NO-SUCH", "viewer")
        with self.assertRaises(NotFoundError):
            self.service.occupy("NO-SUCH", {"occupy_resources": ["E1"]},
                                "disp", "field_commander")


if __name__ == "__main__":
    unittest.main()
