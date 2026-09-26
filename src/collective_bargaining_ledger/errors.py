"""集体协商与履约服务的错误类型。"""

from __future__ import annotations


class BargainingError(Exception):
    """所有领域错误的基类。"""

    code = "bargaining_error"
    http_status = 400


class AuthorizationError(BargainingError):
    """调用方没有有效授权，或试图行使其他角色的权力。"""

    code = "authorization_error"
    http_status = 403


class AuthenticationError(BargainingError):
    """令牌缺失、无法识别或已随卸任失效。"""

    code = "authentication_error"
    http_status = 401


class NotFoundError(BargainingError):
    """协商回合或相关记录不存在。"""

    code = "not_found"
    http_status = 404


class ValidationError(BargainingError):
    """诉求、方案或履约事实未通过结构与联动校验。"""

    code = "validation_error"
    http_status = 422

    def __init__(self, message: str, issues: list[str] | None = None):
        super().__init__(message)
        self.issues = issues or [message]


class ConflictError(BargainingError):
    """当前协商状态不允许该操作。"""

    code = "conflict"
    http_status = 409


class IdempotencyConflict(BargainingError):
    """相同业务号携带了与首次请求不同的内容。"""

    code = "idempotency_conflict"
    http_status = 409

    def __init__(self, request_id: str, original_operation: str, original_hash: str):
        super().__init__(
            f"业务号 {request_id} 已用于操作 {original_operation}，"
            "本次请求正文与原请求不一致，冲突已暴露，不会覆盖原结果"
        )
        self.request_id = request_id
        self.original_operation = original_operation
        self.original_hash = original_hash
