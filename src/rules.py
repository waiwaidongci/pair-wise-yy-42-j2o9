from __future__ import annotations
import hashlib
import json
from .domain import ConflictError, ValidationError
TITLE='山火事件指挥与离线人员调度'; ENTITY='山火事件'; ID_PREFIX='WF'
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; TRANSITIONS={'reported': ['active'], 'active': ['contained'], 'contained': ['controlled'], 'controlled': ['closed'], 'closed': []}; TRANSITION_ROLES={'active': ['incident_commander'], 'contained': ['incident_commander'], 'controlled': ['incident_commander'], 'closed': ['incident_commander']}
CREATE_ROLES=set(['field_commander']); RECORD_ROLES=set(['field_commander', 'logistics']); AUDIT_ROLES=set(['incident_commander', 'viewer']); VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
BATCH_SUBMIT_ROLES=set(['field_commander','incident_commander']); RESOURCE_ROLES=set(['logistics','field_commander','incident_commander']); OCCUPANCY_ROLES=set(['logistics','field_commander','incident_commander']); STRUCTURE_ROLES=set(['field_commander','incident_commander'])
SEVERITY_WEIGHT={'low': 1.0, 'moderate': 3.0, 'high': 6.0, 'extreme': 9.0}; DEADLINE_HOURS={'low': 72, 'moderate': 24, 'high': 8, 'extreme': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))

# --- 离线批次与幂等 ---
def content_hash(payload):
    canonical=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

def same_content(a,b): return content_hash(a)==content_hash(b)

# --- 任务结论：由火线长度、风向、任务区共同决定，改动后失效重算 ---
CONCLUSION_RISKS=['low','moderate','high','extreme']
CONCLUSION_RECOMMENDATIONS={'low':'常规巡查','moderate':'加强监控','high':'前置力量','extreme':'立即处置'}
def compute_conclusion(fire_line_length,wind_speed,area_factor=1.0):
    fire_line_length=float(fire_line_length); wind_speed=float(wind_speed); area_factor=float(area_factor)
    if fire_line_length<0 or wind_speed<0 or area_factor<0: raise ValidationError("结论参数不能为负")
    score=wind_speed*4.0+(fire_line_length/50.0)+area_factor*10.0
    score=max(0.0,min(100.0,score))
    if score<25: risk='low'
    elif score<50: risk='moderate'
    elif score<75: risk='high'
    else: risk='extreme'
    return {'risk':risk,'score':round(score,2),'recommendation':CONCLUSION_RECOMMENDATIONS[risk],
            'inputs':{'fire_line_length':fire_line_length,'wind_speed':wind_speed,'area_factor':area_factor}}

# --- 资源占用：同一资源同时只能有一笔活动占用，落败方重算可用资源 ---
def available_resources(total,occupied):
    return max(0.0,float(total)-float(occupied))

def occupancy_fits(quantity,available):
    return float(quantity)>=0 and float(quantity)<=float(available)

# --- 任务区难度系数，供结论计算引用 ---
DEFAULT_AREA_FACTOR=1.0
