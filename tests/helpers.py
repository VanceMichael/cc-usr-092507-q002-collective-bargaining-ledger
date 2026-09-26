"""测试共享的构造辅助：标准回合、合法方案、常用载荷。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collective_bargaining_ledger import BargainingService, Store  # noqa: E402

FACILITATOR = "fac-token"
REVIEWER = "rev-token"
UNION_TOKEN = "u-token"
ENTERPRISE_TOKEN = "e-token"

VALID_ASSUMPTIONS = {
    "headcount": 100,
    "current_monthly_wage_per_head": "5000",
    "current_standard_hours_month": "174",
    "local_minimum_wage_monthly": "2690",
    "monthly_affordability_budget": "40000",
    "one_time_affordability_budget": "50000",
    "legal_max_hours_month": "212",
}

VALID_PACKAGE = {
    "wages": {"base_monthly_wage": "5200", "allowance_monthly": "100"},
    "hours": {"standard_hours_month": "174", "max_overtime_hours_month": "30"},
    "benefits": {
        "items": [{"name": "高温补贴", "employer_cost_monthly": "50"}],
        "one_time_cost": "0",
    },
}

VALID_CALIBER = {
    "wage_base": "应发月工资",
    "overtime_pay_base": "月工资基数",
    "benefits_valuation": "雇主实际承担成本",
    "rounding": "四舍五入到分",
}


def proposal_payload(**package_overrides) -> dict:
    package = {
        "wages": dict(VALID_PACKAGE["wages"]),
        "hours": dict(VALID_PACKAGE["hours"]),
        "benefits": {
            "items": [dict(i) for i in VALID_PACKAGE["benefits"]["items"]],
            "one_time_cost": VALID_PACKAGE["benefits"]["one_time_cost"],
        },
    }
    for section, values in package_overrides.items():
        if section == "benefits_items":
            package["benefits"]["items"] = values
        else:
            package[section].update(values)
    return {"package": package, "caliber": dict(VALID_CALIBER)}


class RoundFixture(unittest.TestCase):
    """每个用例一个全新内存库与标准回合。"""

    prefix = "t"

    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.svc = BargainingService(self.store)
        self.svc.appoint_officer("facilitator", FACILITATOR, "园区工会主持人")
        self.svc.appoint_officer("reviewer", REVIEWER, "协议复核人员")
        self.round_id = self.svc.open_round(FACILITATOR, "测试回合", f"{self.prefix}:open")[
            "round_id"
        ]
        self.svc.appoint_representative(
            FACILITATOR, self.round_id, "union", UNION_TOKEN, "职工代表", f"{self.prefix}:u"
        )
        self.svc.appoint_representative(
            FACILITATOR, self.round_id, "enterprise", ENTERPRISE_TOKEN, "企业代表",
            f"{self.prefix}:e",
        )

    def tearDown(self) -> None:
        self.store.close()

    def submit_basics(self, prefix: str = "t") -> None:
        self.svc.submit_demand(
            UNION_TOKEN, self.round_id, {"items": ["月薪上调至5200元"]}, f"{prefix}:d"
        )
        self.svc.submit_assumptions(
            ENTERPRISE_TOKEN, self.round_id, dict(VALID_ASSUMPTIONS), f"{prefix}:a"
        )

    def submit_proposal(self, prefix: str = "t", **overrides) -> str:
        self.submit_basics(prefix)
        return self.svc.submit_proposal(
            UNION_TOKEN, self.round_id, proposal_payload(**overrides), f"{prefix}:p"
        )["hash"]

    def freeze(self, version_hash: str, prefix: str = "t") -> None:
        self.svc.confirm_version(UNION_TOKEN, self.round_id, version_hash, f"{prefix}:c1")
        self.svc.confirm_version(ENTERPRISE_TOKEN, self.round_id, version_hash, f"{prefix}:c2")

    def ratify(self, prefix: str = "t") -> None:
        self.svc.cast_vote(UNION_TOKEN, self.round_id, "approve", f"{prefix}:v1")
        self.svc.cast_vote(ENTERPRISE_TOKEN, self.round_id, "approve", f"{prefix}:v2")

    def sign_round(self, prefix: str = "t") -> str:
        self.svc.sign(UNION_TOKEN, self.round_id, f"{prefix}:s1")
        result = self.svc.sign(ENTERPRISE_TOKEN, self.round_id, f"{prefix}:s2")
        return result["agreement_id"]

    def reach_signed(self, prefix: str = "t", **overrides) -> str:
        version_hash = self.submit_proposal(prefix, **overrides)
        self.freeze(version_hash, prefix)
        self.ratify(prefix)
        return self.sign_round(prefix)
