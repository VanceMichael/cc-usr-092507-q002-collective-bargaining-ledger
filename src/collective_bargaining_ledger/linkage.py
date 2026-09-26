"""工资、工时、福利的联动整体校验。

三项议题不允许分头表决：工时缩短可能变相降低时薪、福利追加会推高总人工成本，
任何一项单独看成立都不构成可签署方案。这里在表决前对整包方案做一次确定性测算，
输出全部指标和不通过的具体原因，供双方在同一份口径下核对。

金额与比例统一使用 Decimal，避免双方各自用浮点复算出不一致结果；
最终指标按口径约定的精度（默认分）四舍五入后再比较。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any

from .errors import ValidationError

TWO_PLACES = Decimal("0.01")
REQUIRED_CALIBER_KEYS = ("wage_base", "overtime_pay_base", "benefits_valuation", "rounding")
SIDES = ("wages", "hours", "benefits")


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError(f"字段 {field} 必须是数字")
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except Exception:
            pass
    raise ValidationError(f"字段 {field} 必须是数字")


def _money(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def validate_caliber(caliber: dict[str, Any]) -> list[str]:
    """测算口径必须声明工资基数、加班基数、福利计价和舍入规则。"""

    issues: list[str] = []
    if not isinstance(caliber, dict):
        return ["测算口径必须是结构化对象"]
    for key in REQUIRED_CALIBER_KEYS:
        if not caliber.get(key):
            issues.append(f"测算口径缺少 {key}")
    return issues


def evaluate_linkage(
    package: dict[str, Any],
    assumptions: dict[str, Any],
    caliber: dict[str, Any],
) -> dict[str, Any]:
    """对整包方案做联动测算，返回指标与问题清单；issues 为空才可提交表决。

    入参均为已通过 JSON 结构检查的普通字典。函数是纯函数，
    同样的方案、假设与口径必然得到逐字一致的报告。
    """

    issues: list[str] = []
    issues.extend(validate_caliber(caliber))
    for side in SIDES:
        if not isinstance(package.get(side), dict):
            issues.append(f"方案缺少 {side} 部分，工资工时福利必须整体提交")
    if not isinstance(assumptions, dict):
        issues.append("经营假设必须是结构化对象")
    if issues:
        return {"ok": False, "issues": issues, "metrics": None}

    wages = package["wages"]
    hours = package["hours"]
    benefits = package["benefits"]

    try:
        headcount = int(assumptions.get("headcount"))
        if headcount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        issues.append("经营假设 headcount 必须为正整数")
        return {"ok": False, "issues": issues, "metrics": None}

    # 显式逐项取数，缺失即记问题，避免隐式按零处理。
    def take(container: dict[str, Any], key: str, name: str, *, allow_missing: bool = False) -> Decimal | None:
        if key not in container or container[key] is None:
            if not allow_missing:
                issues.append(f"{name}缺失")
            return None
        try:
            return _decimal(container[key], name)
        except ValidationError:
            issues.append(f"{name}必须是数字")
            return None

    cur_wage = take(assumptions, "current_monthly_wage_per_head", "现行月工资")
    cur_hours = take(assumptions, "current_standard_hours_month", "现行月标准工时")
    min_wage = take(assumptions, "local_minimum_wage_monthly", "当地月最低工资")
    budget = take(assumptions, "monthly_affordability_budget", "月度可承担新增成本上限")
    legal_hours = take(assumptions, "legal_max_hours_month", "法定月工时上限", allow_missing=True)
    one_time_budget = take(
        assumptions, "one_time_affordability_budget", "一次性支出可承担上限", allow_missing=True
    )

    new_wage = take(wages, "base_monthly_wage", "方案月工资基数")
    allowance = take(wages, "allowance_monthly", "月度津贴", allow_missing=True)
    items = benefits.get("items")
    benefit_one_time = take(benefits, "one_time_cost", "福利一次性支出", allow_missing=True)

    std_hours = take(hours, "standard_hours_month", "方案月标准工时")
    max_ot = take(hours, "max_overtime_hours_month", "月加班上限", allow_missing=True)

    if issues:
        return {"ok": False, "issues": issues, "metrics": None}

    allowance = allowance or Decimal(0)
    max_ot = max_ot or Decimal(0)
    benefit_one_time = benefit_one_time or Decimal(0)
    legal_hours = legal_hours if legal_hours is not None else Decimal("212")

    if new_wage < 0 or allowance < 0:
        issues.append("工资与津贴不得为负")
    if std_hours <= 0:
        issues.append("月标准工时必须为正")
    if max_ot < 0:
        issues.append("月加班上限不得为负")

    benefit_items: list[dict[str, Any]] = []
    benefit_monthly_per_head = Decimal(0)
    if not isinstance(items, list) or not items:
        issues.append("福利清单至少包含一项，整体校验不允许空福利部分")
    else:
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not item.get("name"):
                issues.append(f"福利第{index + 1}项缺少名称")
                continue
            cost = take(item, "employer_cost_monthly", f"福利“{item['name']}”雇主月成本", allow_missing=True)
            cost = cost or Decimal(0)
            if cost < 0:
                issues.append(f"福利“{item['name']}”成本不得为负")
            benefit_monthly_per_head += cost
            benefit_items.append({"name": str(item["name"]), "employer_cost_monthly": str(_money(cost))})

    with localcontext() as ctx:
        ctx.prec = 28
        total_new_wage = _money(new_wage + allowance)
        wage_delta_per_head = _money(total_new_wage - cur_wage)

        cur_hourly = _money(cur_wage / cur_hours)
        new_hourly = _money(total_new_wage / std_hours)

        total_hours = std_hours + max_ot
        monthly_delta_total = _money(
            headcount * (wage_delta_per_head + _money(benefit_monthly_per_head))
        )
        one_time_total = _money(headcount * benefit_one_time)
        headroom = _money(budget - monthly_delta_total)

    # 规则一：不得低于最低工资。
    if total_new_wage < min_wage:
        issues.append(
            f"月应发工资 {total_new_wage} 元低于当地最低工资 {min_wage} 元"
        )

    # 规则二：联动核心——无论工时增减，方案时薪不得低于现行时薪，
    # 杜绝“月薪微涨、工时大增”或“缩工时掩蔽降薪”。
    if new_hourly < cur_hourly:
        issues.append(
            f"方案时薪 {new_hourly} 元低于现行时薪 {cur_hourly} 元，"
            "工资与工时联动不通过"
        )

    # 规则三：标准工时加加班上限不得突破法定上限。
    if total_hours > legal_hours:
        issues.append(
            f"月工时合计 {total_hours} 小时超过法定上限 {legal_hours} 小时"
        )

    # 规则四：整包月度新增人工成本不得超过企业按经营假设给出的承担上限。
    if monthly_delta_total > budget:
        issues.append(
            f"整包月度新增成本 {monthly_delta_total} 元超过可承担上限 "
            f"{budget} 元（超 {_money(monthly_delta_total - budget)} 元）"
        )

    # 规则五：一次性支出在企业给出专项上限时同样受控。
    if one_time_total > 0 and one_time_budget is not None and one_time_total > one_time_budget:
        issues.append(
            f"福利一次性支出 {one_time_total} 元超过专项上限 {one_time_budget} 元"
        )

    metrics = {
        "headcount": headcount,
        "current_hourly_wage": str(cur_hourly),
        "new_monthly_wage_per_head": str(total_new_wage),
        "new_hourly_wage": str(new_hourly),
        "wage_delta_per_head_month": str(wage_delta_per_head),
        "benefit_monthly_per_head": str(_money(benefit_monthly_per_head)),
        "benefit_items": benefit_items,
        "standard_hours_month": str(std_hours),
        "max_overtime_hours_month": str(max_ot),
        "total_hours_month": str(total_hours),
        "monthly_new_cost_total": str(monthly_delta_total),
        "monthly_affordability_budget": str(budget),
        "monthly_headroom": str(headroom),
        "one_time_cost_total": str(one_time_total),
        "caliber": {key: str(caliber[key]) for key in REQUIRED_CALIBER_KEYS},
    }
    return {"ok": not issues, "issues": issues, "metrics": metrics}
