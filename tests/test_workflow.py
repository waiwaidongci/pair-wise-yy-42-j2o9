import tempfile
import unittest
from pathlib import Path

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


class WorkflowTest(unittest.TestCase):
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

    def test_offline_batch_applied_and_audit_chain(self):
        batch = self.service.submit_batch(
            {"entries": [entry("WF-1", occupy_resources=["E1"])]},
            "disp1", "field_commander")
        self.assertEqual(batch["status"], "merged")
        self.assertEqual(batch["outcome_counts"], {"applied": 1})
        ticket = self.service.get_ticket_view("WF-1", "viewer")
        self.assertEqual(ticket["status"], "active")
        self.assertEqual(ticket["risk"]["spread_direction"], "S")
        self.assertEqual({o["resource_code"] for o in ticket["occupations"]}, {"E1"})
        self.assertTrue(self.repo.verify_audit_chain())

    def test_duplicate_reuses_first_result(self):
        payload = entry("WF-1", occupy_resources=["E1"])
        first = self.service.submit_batch({"entries": [payload]}, "disp1",
                                          "field_commander")
        second = self.service.submit_batch({"entries": [payload]}, "disp2",
                                           "field_commander")
        self.assertEqual(first["entries"][0]["outcome"], "applied")
        self.assertEqual(second["entries"][0]["outcome"], "reused")
        # 正式记录不被覆盖：创建人沿用首次
        ticket = self.service.get_ticket_view("WF-1", "viewer")
        self.assertEqual(ticket["created_by"], "disp1")
        # 同一单号重复占用同一资源不算冲突
        self.assertEqual(len(self.repo.list_occupations(status="active")), 1)

    def test_divergent_content_goes_to_review_and_never_overwrites(self):
        self.service.submit_batch({"entries": [entry("WF-1")]}, "disp1",
                                  "field_commander")
        divergent = entry("WF-1", wind_direction="S", wind_speed_kmh=30)
        batch = self.service.submit_batch({"entries": [divergent]}, "disp2",
                                          "field_commander")
        self.assertEqual(batch["entries"][0]["outcome"], "needs_review")
        ticket = self.service.get_ticket_view("WF-1", "viewer")
        self.assertEqual(ticket["wind_direction"], "N")  # 正式记录保持首次内容
        reviews = self.service.list_reviews("incident_commander")
        self.assertEqual(len(reviews), 1)
        # 再次回传不同内容不重复挂起
        divergent2 = entry("WF-1", wind_direction="E")
        self.service.submit_batch({"entries": [divergent2]}, "disp2",
                                  "field_commander")
        self.assertEqual(len(self.service.list_reviews("incident_commander")), 1)

        # 核对通过后按新输入更新，关联任务重算
        result = self.service.resolve_review(
            reviews[0]["id"], {"decision": "apply"}, "ic", "incident_commander")
        self.assertEqual(result["changed_fields"], ["wind_direction", "wind_speed_kmh"])
        self.assertEqual(self.service.get_ticket_view("WF-1", "viewer")["wind_direction"],
                         "S")
        self.assertEqual(len(self.service.list_reviews("incident_commander")), 0)

    def test_input_change_recalculates_related_tasks_only(self):
        self.service.submit_batch(
            {"entries": [entry("WF-1", tasks=[
                {"name": "巡线", "status": "done"},
                {"name": "隔离带", "status": "pending"},
            ])]}, "disp1", "field_commander")
        self.service.submit_batch({"entries": [entry("WF-2")]}, "disp2",
                                  "field_commander")
        result = self.service.amend_ticket(
            "WF-1", {"fireline_length_km": 8, "wind_direction": "E",
                     "wind_speed_kmh": 30},
            "disp1", "field_commander")
        recalc = {r["name"]: (r["from_status"], r["to_status"])
                  for r in result["recalculated"]}
        self.assertEqual(recalc["巡线"], ("done", "active"))  # 结论失效，退回重做
        self.assertEqual(recalc["隔离带"], ("pending", "pending"))
        wf1 = self.service.get_ticket_view("WF-1", "viewer")
        self.assertEqual(wf1["stale_task_ids"], [])
        self.assertGreaterEqual(wf1["risk"]["risk_score"], 7.0)
        self.assertEqual(wf1["risk"]["risk_level"], "high")
        # 其他单号照常，不受影响
        wf2 = self.service.get_ticket_view("WF-2", "viewer")
        self.assertEqual(wf2["wind_direction"], "N")
        self.assertEqual(wf2["stale_task_ids"], [])

    def test_close_requires_tasks_done_and_occupations_released(self):
        self.service.submit_batch(
            {"entries": [entry("WF-1", occupy_resources=["E1"],
                               tasks=[{"name": "巡线", "status": "active"}])]},
            "disp1", "field_commander")
        from src.domain import ConflictError
        with self.assertRaises(ConflictError):
            self.service.close_ticket("WF-1", "ic", "incident_commander")
        ticket = self.service.get_ticket_view("WF-1", "viewer")
        self.service.transition_task(ticket["tasks"][0]["id"], "done",
                                     "disp1", "field_commander")
        with self.assertRaises(ConflictError):
            self.service.close_ticket("WF-1", "ic", "incident_commander")
        occ = ticket["occupations"][0]
        self.service.release_occupation(occ["id"], "disp1", "field_commander")
        closed = self.service.close_ticket("WF-1", "ic", "incident_commander")
        self.assertEqual(closed["status"], "closed")


if __name__ == "__main__":
    unittest.main()
