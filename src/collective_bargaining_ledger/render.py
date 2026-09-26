"""把已确认的方案版本与协议渲染为确定的、双方逐字一致的文本。

渲染结果只用于展示与归档；一致性判定始终以规范化 JSON 及其 SHA-256 哈希为准，
因此文本末尾固定附上“逐字基准”段，保证渲染器措辞调整不会改变基准内容，
也不会让同一份 payload 出现两种解释。
"""

from __future__ import annotations

from typing import Any

from .canonical import canonical_json, content_hash


def render_demand(payload: dict[str, Any]) -> str:
    lines = ["【职工方诉求】"]
    for item in payload.get("items", []):
        lines.append(f"诉求项：{item}")
    if payload.get("note"):
        lines.append(f"说明：{payload['note']}")
    return "\n".join(lines)


def render_business_assumptions(payload: dict[str, Any]) -> str:
    lines = ["【企业方经营假设】"]
    order = (
        ("headcount", "覆盖职工人数"),
        ("current_monthly_wage_per_head", "现行月工资（元/人）"),
        ("current_standard_hours_month", "现行月标准工时（小时）"),
        ("local_minimum_wage_monthly", "当地月最低工资（元）"),
        ("monthly_affordability_budget", "月度可承担新增成本上限（元）"),
        ("one_time_affordability_budget", "一次性支出可承担上限（元）"),
        ("legal_max_hours_month", "法定月工时上限（小时）"),
    )
    for key, label in order:
        if key in payload:
            lines.append(f"{label}：{payload[key]}")
    for key, value in payload.items():
        if key in {k for k, _ in order} or key == "kind":
            continue
        lines.append(f"补充假设 {key}：{canonical_json(value)}")
    return "\n".join(lines)


def render_proposal(payload: dict[str, Any]) -> str:
    package = payload["package"]
    caliber = payload["caliber"]
    lines = ["【协商方案（整包：工资/工时/福利）】", "一、工资"]
    wages = package["wages"]
    lines.append(f"月工资基数（元）：{wages['base_monthly_wage']}")
    if wages.get("allowance_monthly") is not None:
        lines.append(f"月度津贴（元）：{wages['allowance_monthly']}")
    lines.append("二、工时")
    hours = package["hours"]
    lines.append(f"月标准工时（小时）：{hours['standard_hours_month']}")
    if hours.get("max_overtime_hours_month") is not None:
        lines.append(f"月加班上限（小时）：{hours['max_overtime_hours_month']}")
    lines.append("三、福利")
    for item in package["benefits"]["items"]:
        lines.append(
            f"福利项：{item['name']}（雇主月成本 {item['employer_cost_monthly']} 元）"
        )
    if package["benefits"].get("one_time_cost") is not None:
        lines.append(f"福利一次性支出（元/人）：{package['benefits']['one_time_cost']}")
    lines.append("四、测算口径")
    for key, value in caliber.items():
        lines.append(f"{key}：{canonical_json(value) if not isinstance(value, str) else value}")
    return "\n".join(lines)


_RENDERERS = {
    "demand": render_demand,
    "assumptions": render_business_assumptions,
    "proposal": render_proposal,
}


def render_version(kind: str, payload: dict[str, Any]) -> str:
    """渲染版本正文，并附加规范化逐字基准。"""

    body = _RENDERERS[kind](payload)
    return (
        f"{body}\n\n"
        f"【逐字基准｜SHA-256:{content_hash(payload)}】\n"
        f"{canonical_json(payload)}"
    )


def derive_clauses(version_payload: dict[str, Any]) -> list[dict[str, str]]:
    """从方案版本派生稳定编号的条款；重新审议时按编号比对，禁止暗改。"""

    package = version_payload["package"]
    clauses: list[dict[str, str]] = [
        {
            "code": "GZ-01",
            "topic": "wages",
            "text": f"月工资基数为 {package['wages']['base_monthly_wage']} 元。",
        }
    ]
    if package["wages"].get("allowance_monthly") is not None:
        clauses.append(
            {
                "code": "GZ-02",
                "topic": "wages",
                "text": f"月度津贴为 {package['wages']['allowance_monthly']} 元。",
            }
        )
    clauses.append(
        {
            "code": "GS-01",
            "topic": "hours",
            "text": f"月标准工时为 {package['hours']['standard_hours_month']} 小时。",
        }
    )
    if package["hours"].get("max_overtime_hours_month") is not None:
        clauses.append(
            {
                "code": "GS-02",
                "topic": "hours",
                "text": f"月加班时间不超过 {package['hours']['max_overtime_hours_month']} 小时。",
            }
        )
    for index, item in enumerate(package["benefits"]["items"], start=1):
        clauses.append(
            {
                "code": f"FL-{index:02d}",
                "topic": "benefits",
                "text": f"福利“{item['name']}”由企业提供，雇主月成本 {item['employer_cost_monthly']} 元。",
            }
        )
    if package["benefits"].get("one_time_cost") is not None:
        clauses.append(
            {
                "code": "FL-99",
                "topic": "benefits",
                "text": f"福利一次性支出为 {package['benefits']['one_time_cost']} 元/人。",
            }
        )
    return clauses


def render_agreement(
    round_id: str,
    version_hash: str,
    version_payload: dict[str, Any],
    clauses: list[dict[str, str]],
    effective_from: str,
    generations: dict[str, int],
) -> str:
    """渲染双方共同签署的协议全文（含逐字基准）。"""

    lines = [
        f"集体协商协议（回合 {round_id}）",
        f"生效起始周期：{effective_from}",
        f"职工方签署任次：第{generations['union']}任；企业方签署任次：第{generations['enterprise']}任",
        f"依据方案版本：{version_hash}",
        "",
        render_proposal(version_payload),
        "",
        "【协议条款】",
    ]
    for clause in clauses:
        lines.append(f"{clause['code']} {clause['text']}")
    lines.extend(
        [
            "",
            f"【逐字基准｜SHA-256:{version_hash}】",
            canonical_json(version_payload),
        ]
    )
    return "\n".join(lines)


def render_supplement(agreement_id: str, sequence: int, text: str) -> str:
    return (
        f"补充约定（协议 {agreement_id} 第 {sequence} 号）\n"
        f"{text}\n"
        f"【逐字基准｜SHA-256:{content_hash(text)}】"
    )
