"""领域模型：角色授权、协商陈述、条款文本与协议结构。

设计要点：

- 每一次对外操作都携带 ``mandate_id``，服务端校验该授权仍然有效；
  代表更换后旧授权立即失效，其任内尚未被对方接受的意见不随新人继承。
- 所有可签署内容以规范化 JSON 逐字序列化后计算 SHA-256，
  “逐字一致”比较的是字节串而不是对象语义。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

WORKER = "worker"   # 职工方（工会）
COMPANY = "company"  # 企业方
SIDES = (WORKER, COMPANY)
OPPOSITE = {WORKER: COMPANY, COMPANY: WORKER}


class Side(str, Enum):
    WORKER = WORKER
    COMPANY = COMPANY

    @property
    def opposite(self) -> "Side":
        return Side(OPPOSITE[self.value])


# 协商过程中允许提交的陈述类型
STATEMENT_DEMANDS = "demands"            # 诉求
STATEMENT_ASSUMPTIONS = "assumptions"    # 经营假设
STATEMENT_BASES = "calculation_bases"    # 测算口径
STATEMENT_COUNTER = "counter_proposal"   # 反建议
STATEMENT_PERSONAL = "personal_statement"  # 个人陈述（按角色脱敏）
NEGOTIATION_STATEMENTS = (
    STATEMENT_DEMANDS,
    STATEMENT_ASSUMPTIONS,
    STATEMENT_BASES,
    STATEMENT_COUNTER,
)
ALL_STATEMENTS = NEGOTIATION_STATEMENTS + (STATEMENT_PERSONAL,)

# 条款大类：工资 / 工时 / 福利，三者表决前必须整体联动校验
CLAUSE_WAGES = "wages"
CLAUSE_HOURS = "hours"
CLAUSE_BENEFITS = "benefits"
CLAUSE_KINDS = (CLAUSE_WAGES, CLAUSE_HOURS, CLAUSE_BENEFITS)

# 履约结果
PERFORM_FULL = "full"
PERFORM_PARTIAL = "partial"
PERFORM_DISPUTE = "dispute"
PERFORM_KINDS = (PERFORM_FULL, PERFORM_PARTIAL, PERFORM_DISPUTE)

# 个人陈述脱敏后的可见性
SENSITIVE_MASK = "［依角色脱敏］"


def canonical_dumps(payload: Any) -> str:
    """领域内统一的逐字序列化：键排序、无空白、保证中文不转义。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def text_hash(payload: Any) -> str:
    """对任意可 JSON 化内容计算逐字版本哈希。"""
    return hashlib.sha256(canonical_dumps(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Representative:
    """一方代表及其授权凭据。授权更换后凭据立即失效。"""

    representative_id: str
    name: str
    side: str
    mandate_id: str
    valid_from: str
    valid_to: str | None = None  # None 表示仍在任

    @property
    def active(self) -> bool:
        return self.valid_to is None


@dataclass
class Package:
    """进入表决与签署的完整条款包：工资、工时、福利逐字绑定。"""

    clauses: dict[str, dict[str, Any]]  # kind -> 条款内容（可含数值与文字）
    assumptions: dict[str, Any] = field(default_factory=dict)
    bases: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def __post_init__(self) -> None:
        missing = [kind for kind in CLAUSE_KINDS if kind not in self.clauses]
        if missing:
            raise ValueError(f"条款包缺少必须整体校验的类别：{','.join(missing)}")
        extra = [kind for kind in self.clauses if kind not in CLAUSE_KINDS]
        if extra:
            raise ValueError(f"条款包包含未知类别：{','.join(extra)}")

    def to_document(self) -> dict[str, Any]:
        return {
            "clauses": {kind: self.clauses[kind] for kind in CLAUSE_KINDS},
            "assumptions": self.assumptions,
            "bases": self.bases,
            "note": self.note,
        }


@dataclass(frozen=True)
class PackageVersion:
    """一个逐字一致的条款版本及其双方确认状态。"""

    version_hash: str
    document: dict[str, Any]
    proposed_by: str
    created_at: str
    confirmed_by: frozenset[str] = frozenset()

    def confirmed_by_both(self) -> bool:
        return set(SIDES).issubset(self.confirmed_by)

    def with_confirmation(self, side: str, created_at: str) -> "PackageVersion":
        return PackageVersion(
            version_hash=self.version_hash,
            document=self.document,
            proposed_by=self.proposed_by,
            created_at=self.created_at,
            confirmed_by=self.confirmed_by | {side},
        )


# ---- 联动测算用的取值约定 -------------------------------------------------
#
# 条款内容是结构化 JSON，联动校验按以下可选键读取数值（缺省键不参与该条规则）：
#   wages.monthly_min        月最低工资（元）
#   wages.monthly_raise_pct  较现工资的平均涨幅百分比
#   hours.weekly_max         周工时上限（小时）
#   hours.overtime_monthly_max 月加班上限（小时）
#   benefits.monthly_cost_person 福利人均月成本（元）
# 经营假设 assumptions 支持：
#   headcount                在册人数
#   monthly_capacity         企业可承受的月度新增人工成本上限（元）
#   legal_monthly_min        当地月最低工资法定标准（元）
#   legal_weekly_max         法定周工时上限（小时）
#   legal_overtime_monthly_max 法定月加班上限（小时）
# 测算口径 bases 支持：
#   current_monthly_wage     现行月平均工资（元）
# 数值规则缺失的一侧不做硬校验，保证纯文字条款也能流转。
