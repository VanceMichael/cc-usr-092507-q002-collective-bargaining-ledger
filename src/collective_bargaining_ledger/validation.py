"""工资、工时、福利的表决前整体联动校验，以及个人陈述的角色脱敏。

联动规则在同一个校验器内一次评估三类条款，任一项不通过则整个条款包
不能进入表决/签署——不允许工资单条先行通过。

数值字段约定见 ``model.py`` 顶部注释；规则所需的数值缺失时该条规则跳过，
纯文字条款不受影响，但口径冲突与结构错误始终拦截。
"""

from __future__ import annotations

import numbers
from typing import Any, Iterable

from .errors import LinkageError
from .model import (
    CLAUSE_BENEFITS,
    CLAUSE_HOURS,
    CLAUSE_KINDS,
    CLAUSE_WAGES,
    SENSITIVE_MASK,
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    return float(value)


def _violation(code: str, message: str, kind: str = "linkage") -> dict[str, str]:
    return {"code": code, "message": message, "kind": kind}


def validate_package(
    document: dict[str, Any],
    accepted_bases: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """返回所有联动违规；空列表表示三类条款整体可进入表决。

    ``accepted_bases`` 为双方在协商中已被接受的测算口径 {side: content}，
    用于拦截条款包口径与双方共同确认口径不一致的情形。
    """
    violations: list[dict[str, str]] = []
    clauses = document.get("clauses", {})
    assumptions = document.get("assumptions", {}) or {}
    bases = document.get("bases", {}) or {}

    for kind in CLAUSE_KINDS:
        if kind not in clauses or not isinstance(clauses[kind], dict):
            violations.append(_violation(
                "clause_missing", f"缺少必须整体校验的{_kind_label(kind)}条款", kind))

    if violations:
        return violations  # 类别不全，后续数值规则无意义

    wages, hours, benefits = (clauses[k] for k in (
        CLAUSE_WAGES, CLAUSE_HOURS, CLAUSE_BENEFITS))

    # 1. 工资不得低于法定最低工资标准
    monthly_min = _number(wages.get("monthly_min"))
    legal_min = _number(assumptions.get("legal_monthly_min"))
    if monthly_min is not None and monthly_min < 0:
        violations.append(_violation(
            "wage_negative", "月最低工资不能为负", CLAUSE_WAGES))
    if monthly_min is not None and legal_min is not None and monthly_min < legal_min:
        violations.append(_violation(
            "below_legal_min",
            f"月最低工资 {monthly_min:g} 元低于法定标准 {legal_min:g} 元",
            CLAUSE_WAGES))

    # 2. 工时与加班不得突破法定上限
    weekly_max = _number(hours.get("weekly_max"))
    legal_weekly = _number(assumptions.get("legal_weekly_max"))
    if weekly_max is not None and legal_weekly is not None and weekly_max > legal_weekly:
        violations.append(_violation(
            "over_legal_hours",
            f"周工时上限 {weekly_max:g} 小时超过法定 {legal_weekly:g} 小时",
            CLAUSE_HOURS))
    overtime_max = _number(hours.get("overtime_monthly_max"))
    legal_overtime = _number(assumptions.get("legal_overtime_monthly_max"))
    if (overtime_max is not None and legal_overtime is not None
            and overtime_max > legal_overtime):
        violations.append(_violation(
            "over_legal_overtime",
            f"月加班上限 {overtime_max:g} 小时超过法定 {legal_overtime:g} 小时",
            CLAUSE_HOURS))

    # 3. 工资涨幅 + 福利成本必须同时放进企业承受力测算，不得只算工资
    headcount = _number(assumptions.get("headcount"))
    capacity = _number(assumptions.get("monthly_capacity"))
    current_wage = _number(bases.get("current_monthly_wage"))
    raise_pct = _number(wages.get("monthly_raise_pct"))
    benefit_cost = _number(benefits.get("monthly_cost_person"))

    if headcount is not None and headcount <= 0:
        violations.append(_violation(
            "headcount_invalid", "经营假设中在册人数必须为正数"))
    for name, val in (("现行月平均工资", current_wage),
                      ("工资涨幅", raise_pct),
                      ("福利人均月成本", benefit_cost),
                      ("承受力上限", capacity)):
        if val is not None and val < 0:
            violations.append(_violation(
                "negative_number", f"{name}不能为负"))

    if None not in (headcount, capacity, current_wage, raise_pct, benefit_cost):
        incremental = headcount * (current_wage * raise_pct / 100.0 + benefit_cost)
        if incremental > capacity:
            violations.append(_violation(
                "capacity_exceeded",
                f"工资涨幅与福利合计新增月人工成本 {incremental:g} 元，"
                f"超过经营假设可承受上限 {capacity:g} 元（须工资、工时、福利整体平衡）"))

    # 4. 测算口径必须与双方已接受的口径逐字一致（数值层面比对）
    if accepted_bases:
        for side, accepted in accepted_bases.items():
            for key, value in bases.items():
                if key in accepted:
                    av = _number(accepted[key])
                    nv = _number(value)
                    if av is not None and nv is not None:
                        if av != nv:
                            violations.append(_violation(
                                "bases_diverge",
                                f"测算口径 {key}={nv:g} 与"
                                f"{_side_label(side)}已接受口径 {av:g} 不一致"))
                    elif accepted[key] != value:
                        violations.append(_violation(
                            "bases_diverge",
                            f"测算口径 {key} 与{_side_label(side)}已接受口径不一致"))

    return violations


def require_valid_package(
        document: dict[str, Any],
        accepted_bases: dict[str, dict[str, Any]] | None = None) -> None:
    violations = validate_package(document, accepted_bases)
    if violations:
        raise LinkageError(
            "工资、工时、福利整体联动校验未通过，条款包不能进入表决",
            violations)


def _kind_label(kind: str) -> str:
    return {CLAUSE_WAGES: "工资", CLAUSE_HOURS: "工时",
            CLAUSE_BENEFITS: "福利"}[kind]


def _side_label(side: str) -> str:
    return "职工方" if side == "worker" else "企业方"


# ---- 个人陈述脱敏 --------------------------------------------------------

SENSITIVE_KEYS = {"sensitive", "personal", "private", "id_number", "phone"}


def redact_content(content: Any, viewer_side: str, owner_side: str) -> Any:
    """按查看者角色对个人陈述内容脱敏：异侧只见公开部分。"""
    if viewer_side == owner_side:
        return content
    if isinstance(content, dict):
        redacted: dict[str, Any] = {}
        for key, value in content.items():
            if key in SENSITIVE_KEYS:
                redacted[key] = SENSITIVE_MASK
            else:
                redacted[key] = redact_content(value, viewer_side, owner_side)
        return redacted
    if isinstance(content, list):
        return [redact_content(item, viewer_side, owner_side) for item in content]
    return content


def redact_statement(statement: dict[str, Any], viewer_side: str) -> dict[str, Any]:
    """对单条陈述做角色脱敏；非个人陈述原样返回。"""
    if statement.get("kind") != "personal_statement":
        return statement
    result = dict(statement)
    result["content"] = redact_content(
        statement.get("content"), viewer_side, statement.get("side", viewer_side))
    if viewer_side != statement.get("side"):
        # 异侧只看到陈述人角色，不暴露个人身份
        result["reporter"] = _side_label(statement["side"]) + "个人陈述"
        result.pop("mandate_id", None)
    return result


def redact_all(statements: Iterable[dict[str, Any]], viewer_side: str) -> list[dict[str, Any]]:
    return [redact_statement(s, viewer_side) for s in statements]
