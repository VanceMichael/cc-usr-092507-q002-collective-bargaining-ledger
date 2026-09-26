"""服务对外抛出的领域错误。"""

from __future__ import annotations


class LedgerError(Exception):
    """所有业务错误的基类，消息面向双方代表，可直接展示。"""


class AuthorizationError(LedgerError):
    """代表没有有效授权，或授权已被更换。"""


class MandateEndedError(AuthorizationError):
    """操作所依据的授权已终止（代表更换），命令不能继续。"""


class NotFoundError(LedgerError):
    """对象不存在。"""


class StateError(LedgerError):
    """对象当前状态不允许该操作。"""


class ValidationError(LedgerError):
    """提交内容未通过校验（口径、联动规则等）。"""


class LinkageError(ValidationError):
    """工资、工时、福利未通过表决前整体联动校验。"""

    def __init__(self, message: str, violations: list[dict] | None = None):
        super().__init__(message)
        self.violations = violations or []


class TextConflictError(LedgerError):
    """同一业务号携带了与首次不同的请求体（异文冲突）。"""

    def __init__(self, message: str, original_result: dict | None = None):
        super().__init__(message)
        self.original_result = original_result


class ImmutableClauseError(LedgerError):
    """试图修改已经签署生效的条款；新证据只能发起重新审议。"""


class ConflictError(LedgerError):
    """并发冲突，例如已有生效协议或账本记录重复。"""
