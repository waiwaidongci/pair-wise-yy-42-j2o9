import unittest

from src import rules
from src.domain import ConflictError, ValidationError


class RulesTest(unittest.TestCase):
    def test_risk_increases_with_fireline_wind_and_zone(self):
        calm = rules.assess_risk(1.0, 'N', 0, 'forest')
        storm = rules.assess_risk(12.0, 'NW', 55, 'critical_infra')
        self.assertGreater(storm['risk_score'], calm['risk_score'])
        self.assertEqual(calm['spread_direction'], 'S')
        self.assertEqual(storm['spread_direction'], 'SE')
        self.assertEqual(storm['risk_level'], 'extreme')
        self.assertEqual(calm['risk_level'], 'low')

    def test_downwind_rejects_unknown_direction(self):
        with self.assertRaises(ValidationError):
            rules.downwind('UP')

    def test_content_hash_stable_and_sensitive(self):
        args = dict(ticket_no='WF-1', fireline_length_km=3.0,
                    wind_direction='N', wind_speed_kmh=10,
                    zone_kind='forest', zone_name='东坡', note='')
        h1 = rules.ticket_content_hash(**args)
        h2 = rules.ticket_content_hash(**{**args, 'fireline_length_km': 3.0})
        h3 = rules.ticket_content_hash(**{**args, 'wind_direction': 'S'})
        self.assertEqual(h1, h2)  # 3.0与3数值等价，规范化后一致
        self.assertNotEqual(h1, h3)

    def test_entry_hash_covers_task_status_not_resources(self):
        e1 = rules.entry_content_hash(3, 'N', 10, 'forest', '东坡', '',
                                      [{'name': '巡线', 'status': 'active'}])
        e2 = rules.entry_content_hash(3, 'N', 10, 'forest', '东坡', '',
                                      [{'name': '巡线', 'status': 'done'}])
        e3 = rules.entry_content_hash(3, 'N', 10, 'forest', '东坡', '',
                                      [{'name': '巡线', 'status': 'active'}])
        self.assertNotEqual(e1, e2)
        self.assertEqual(e1, e3)

    def test_basis_hash_invalidates_on_input_change(self):
        base = rules.task_basis_hash(3, 'N', 10, 'forest', '东坡', '巡线')
        self.assertNotEqual(base, rules.task_basis_hash(4, 'N', 10, 'forest', '东坡', '巡线'))
        self.assertNotEqual(base, rules.task_basis_hash(3, 'E', 10, 'forest', '东坡', '巡线'))
        self.assertNotEqual(base, rules.task_basis_hash(3, 'N', 10, 'residential', '东坡', '巡线'))
        self.assertEqual(base, rules.task_basis_hash(3, 'N', 10, 'forest', '东坡', '巡线'))

    def test_available_resources_excludes_active_and_retired(self):
        resources = [
            {'code': 'E1', 'name': '一号车', 'kind': 'engine', 'capacity': 1,
             'status': 'available'},
            {'code': 'E2', 'name': '二号车', 'kind': 'engine', 'capacity': 1,
             'status': 'available'},
            {'code': 'A1', 'name': '直升机', 'kind': 'aircraft', 'capacity': 1,
             'status': 'retired'},
        ]
        avail = rules.available_resources(resources, {'E1'})
        self.assertEqual([r['code'] for r in avail], ['E2'])

    def test_task_transitions(self):
        rules.validate_task_transition('pending', 'active')
        rules.validate_task_transition('active', 'done')
        with self.assertRaises(ConflictError):
            rules.validate_task_transition('done', 'active')
        with self.assertRaises(ConflictError):
            rules.validate_task_transition('pending', 'done')

    def test_close_blockers(self):
        self.assertEqual(rules.ticket_can_close(0, 0), [])
        self.assertEqual(len(rules.ticket_can_close(1, 1)), 2)


if __name__ == "__main__":
    unittest.main()
