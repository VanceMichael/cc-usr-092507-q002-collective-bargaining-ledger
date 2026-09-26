"""按角色脱敏。

履约阶段的“个人陈述”可能包含职工或经办人个人信息。账本只保存角色与任次，
不依赖自然人姓名；向无权查看的一方或中立角色提供视图时，个人陈述整体替换
为角色级代号，履行事实（时间、金额、状态）仍可核对。
"""

from __future__ import annotations

from .canonical import content_hash

_NEUTRAL_GROUPS = {"facilitator", "reviewer"}


def same_group(viewer_group: str, author_side: str) -> bool:
    """同一劳资阵营内部可查看原文；中立角色与对立方只看脱敏代号。"""

    return viewer_group == author_side


def role_label(side: str, generation: int) -> str:
    if side == "union":
        return f"职工方代表（第{generation}任）"
    if side == "enterprise":
        return f"企业方代表（第{generation}任）"
    return side


def mask_statement(statement: str, author_side: str, generation: int) -> str:
    """把个人陈述替换为不可逆的角色级代号（保留长度与摘要供核对）。"""

    digest = content_hash(statement)[:10]
    return f"〔{role_label(author_side, generation)}个人陈述｜{len(statement)}字｜摘要{digest}〕"


def view_statement(
    statement: str | None,
    author_side: str,
    generation: int,
    viewer_group: str,
) -> str | None:
    """依据查看者角色决定返回原文还是脱敏代号。"""

    if statement is None:
        return None
    if viewer_group in _NEUTRAL_GROUPS:
        return mask_statement(statement, author_side, generation)
    if same_group(viewer_group, author_side):
        return statement
    return mask_statement(statement, author_side, generation)
