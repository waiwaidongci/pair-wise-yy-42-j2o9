from __future__ import annotations

from typing import Any, Dict, Optional


class ErrorKind:
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"


class DomainError(Exception):
    kind = ErrorKind.VALIDATION

    def __init__(self, message: str, payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        # 结构化上下文，例如资源争用时返回占用对象与最新可用资源
        self.payload: Dict[str, Any] = dict(payload or {})


class ValidationError(DomainError):
    kind = ErrorKind.VALIDATION


class NotFoundError(DomainError):
    kind = ErrorKind.NOT_FOUND


class PermissionDenied(DomainError):
    kind = ErrorKind.FORBIDDEN


class ConflictError(DomainError):
    kind = ErrorKind.CONFLICT


# 允许的取值域集中在领域层，存储层的CHECK与规则层的判定都引用这里
ROLES = ('field_commander', 'incident_commander', 'logistics', 'viewer')
RESOURCE_KINDS = ('engine', 'crew', 'aircraft', 'equipment')
TICKET_STATUSES = ('active', 'closed')
TASK_STATUSES = ('pending', 'active', 'done', 'cancelled')
OCCUPATION_STATUSES = ('active', 'released')
BATCH_STATUSES = ('received', 'merged', 'pending_retry')
ENTRY_OUTCOMES = ('applied', 'reused', 'needs_review')
REVIEW_STATUSES = ('pending', 'applied', 'rejected')
RISK_LEVELS = ('low', 'moderate', 'high', 'extreme')


def require_text(value: Any, field: str, max_length: int = 100) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}不能为空")
    value = value.strip()
    if len(value) > max_length:
        raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value


def optional_text(value: Any, field: str, max_length: int = 2000, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        return default
    value = value.strip()
    if len(value) > max_length:
        raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value


def require_number(value: Any, field: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise ValidationError(f"{field}必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field}必须是数字")
    if number != number or number in (float("inf"), float("-inf")):
        raise ValidationError(f"{field}必须是有限数字")
    if number < minimum:
        raise ValidationError(f"{field}不能小于{minimum}")
    # 统一精度，避免1与1.0000001造成的哈希/重复回传误判
    return round(number, 6)


def require_choice(value: Any, field: str, choices) -> str:
    if value not in choices:
        raise ValidationError(f"{field}不在允许范围内: {', '.join(choices)}")
    return value


def ensure_role(role: Any, allowed) -> None:
    if role not in allowed:
        raise PermissionDenied("当前角色无权执行该操作")
