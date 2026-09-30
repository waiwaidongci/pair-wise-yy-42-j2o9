from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List

from .domain import (RESOURCE_KINDS, RISK_LEVELS, TASK_STATUSES, TICKET_STATUSES,
                     ValidationError, require_choice)

TITLE = '山火指挥现场离线调度'
ENTITY_TICKET = 'ticket'
ENTITY_RESOURCE = 'resource'
ENTITY_BATCH = 'offline_batch'
ENTITY_TASK = 'task'

# 角色矩阵：登记/离线回传由现场调度完成，资源由后勤保障，核对与裁决由总指挥完成
CREATE_ROLES = frozenset(('field_commander',))
REGISTER_RESOURCE_ROLES = frozenset(('logistics', 'incident_commander'))
OCCUPY_ROLES = frozenset(('field_commander', 'logistics'))
AMEND_ROLES = frozenset(('field_commander', 'incident_commander'))
TASK_ROLES = frozenset(('field_commander', 'logistics', 'incident_commander'))
RESOLVE_REVIEW_ROLES = frozenset(('incident_commander',))
AUDIT_ROLES = frozenset(('incident_commander', 'viewer'))
VIEW_ROLES = frozenset(('field_commander', 'incident_commander', 'logistics', 'viewer'))

# 八方位风向（以火势主要蔓延方向为下风向）
WIND_DIRECTIONS = ('N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW')
_OPPOSITE = {'N': 'S', 'NE': 'SW', 'E': 'W', 'SE': 'NW',
             'S': 'N', 'SW': 'NE', 'W': 'E', 'NW': 'SE'}

# 任务区类型权重：居民点与油库等敏感区高于一般林区
ZONE_KINDS = ('forest', 'residential', 'critical_infra')
ZONE_WEIGHT = {'forest': 1.0, 'residential': 2.0, 'critical_infra': 3.0}

# 风速（km/h）附加分；超过50按封顶处理
def wind_speed_bonus(speed_kmh: float) -> float:
    return min(4.0, round(float(speed_kmh) / 12.5, 6))


def downwind(wind_direction: str) -> str:
    if wind_direction not in _OPPOSITE:
        raise ValidationError("风向必须是八方位之一(N/NE/E/SE/S/SW/W/NW)")
    return _OPPOSITE[wind_direction]


def fireline_bonus(length_km: float) -> float:
    # 每2公里记1分，封顶6分
    return min(6.0, round(float(length_km) / 2.0, 6))


def assess_risk(fireline_length_km: float, wind_direction: str, wind_speed_kmh: float,
                zone_kind: str) -> Dict[str, Any]:
    """纯函数：依据火线长度、风向、风速和任务区计算风险结论。"""
    require_choice(wind_direction, 'wind_direction', WIND_DIRECTIONS)
    require_choice(zone_kind, 'zone_kind', ZONE_KINDS)
    if float(fireline_length_km) < 0 or float(wind_speed_kmh) < 0:
        raise ValidationError("火线长度与风速不能为负")
    score = round(fireline_bonus(fireline_length_km)
                  + ZONE_WEIGHT[zone_kind]
                  + wind_speed_bonus(wind_speed_kmh), 6)
    if score >= 10.0:
        level = RISK_LEVELS[3]
    elif score >= 7.0:
        level = RISK_LEVELS[2]
    elif score >= 4.0:
        level = RISK_LEVELS[1]
    else:
        level = RISK_LEVELS[0]
    spread = downwind(wind_direction)
    tactics = {
        'low': '常规巡护，保持观察',
        'moderate': '沿下风向预设隔离带',
        'high': f'优先保护{zone_kind}任务区，向{spread}方向预置增援',
        'extreme': f'立即疏散{spread}方向并请求跨区增援',
    }[level]
    return {'risk_score': score, 'risk_level': level,
            'spread_direction': spread, 'tactic': tactics}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def ticket_content_hash(ticket_no: str, fireline_length_km: float,
                        wind_direction: str, wind_speed_kmh: float,
                        zone_kind: str, zone_name: str, note: str) -> str:
    """单号登记内容的规范化指纹；重复回传是否一致靠它判定。"""
    return digest({
        'ticket_no': ticket_no,
        'fireline_length_km': round(float(fireline_length_km), 6),
        'wind_direction': wind_direction,
        'wind_speed_kmh': round(float(wind_speed_kmh), 6),
        'zone_kind': zone_kind,
        'zone_name': zone_name,
        'note': note,
    })


def entry_content_hash(fireline_length_km: float, wind_direction: str,
                       wind_speed_kmh: float, zone_kind: str, zone_name: str,
                       note: str, tasks: Iterable[Dict[str, Any]]) -> str:
    """离线条目内容指纹：包含任务及其状态，但不包含资源（资源占用随当下可用性变化）。"""
    tasks = [{'name': str(t.get('name', '')).strip(),
              'status': require_choice(t.get('status', 'pending'), 'task.status',
                                       TASK_STATUSES)}
             for t in tasks]
    return digest({
        'fireline_length_km': round(float(fireline_length_km), 6),
        'wind_direction': wind_direction,
        'wind_speed_kmh': round(float(wind_speed_kmh), 6),
        'zone_kind': zone_kind,
        'zone_name': zone_name,
        'note': note,
        'tasks': tasks,
    })


def task_basis_hash(fireline_length_km: float, wind_direction: str,
                    wind_speed_kmh: float, zone_kind: str, zone_name: str,
                    task_name: str) -> str:
    """任务结论所依赖输入的指纹；输入一改，basis对不上即结论失效。"""
    return digest({
        'fireline_length_km': round(float(fireline_length_km), 6),
        'wind_direction': wind_direction,
        'wind_speed_kmh': round(float(wind_speed_kmh), 6),
        'zone_kind': zone_kind,
        'zone_name': zone_name,
        'task_name': task_name,
    })


def available_resources(resources: List[Dict[str, Any]],
                        active_codes) -> List[Dict[str, Any]]:
    active_codes = set(active_codes)
    return [{'code': r['code'], 'name': r['name'], 'kind': r['kind'],
             'capacity': r['capacity']}
            for r in resources
            if r['status'] == 'available' and r['code'] not in active_codes]


# 任务状态推进规则；cancelled为终态，任何输入变化都不重算
TASK_TRANSITIONS = {
    'pending': ('active', 'cancelled'),
    'active': ('done', 'cancelled'),
    'done': (),
    'cancelled': (),
}


def validate_task_transition(current: str, target: str) -> None:
    require_choice(target, 'target', TASK_STATUSES)
    if target not in TASK_TRANSITIONS.get(current, ()):
        from .domain import ConflictError
        raise ConflictError(f"任务不能从{current}转换到{target}")


def ticket_can_close(open_tasks: int, active_occupations: int) -> List[str]:
    blockers: List[str] = []
    if open_tasks:
        blockers.append('仍有未完成任务')
    if active_occupations:
        blockers.append('仍有未释放的资源占用')
    return blockers
