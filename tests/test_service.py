"""核心协商流程：授权、逐字确认、人员更换、联动校验、签署、履约、重新审议。"""

from __future__ import annotations

import threading
import unittest

from helpers import (
    ENTERPRISE_TOKEN,
    FACILITATOR,
    REVIEWER,
    UNION_TOKEN,
    VALID_ASSUMPTIONS,
    RoundFixture,
    proposal_payload,
)
from collective_bargaining_ledger import (
    AuthorizationError,
    ConflictError,
    IdempotencyConflict,
    NotFoundError,
    ValidationError,
)


class AuthorizationTest(RoundFixture):
    prefix = "auth"

    def test_only_authorized_side_can_submit_its_materials(self):
        with self.assertRaises(AuthorizationError):
            self.svc.submit_demand(ENTERPRISE_TOKEN, self.round_id, {"items": ["x"]}, "a1")
        with self.assertRaises(AuthorizationError):
            self.svc.submit_assumptions(UNION_TOKEN, self.round_id, dict(VALID_ASSUMPTIONS), "a2")

    def test_neutral_roles_cannot_negotiate(self):
        with self.assertRaises(AuthorizationError):
            self.svc.submit_demand(FACILITATOR, self.round_id, {"items": ["x"]}, "a3")
        with self.assertRaises(AuthorizationError):
            self.svc.cast_vote(REVIEWER, self.round_id, "approve", "a4")

    def test_revoked_token_is_rejected(self):
        self.svc.appoint_representative(
            FACILITATOR, self.round_id, "union", "u-new", "新任职工代表", "a5"
        )
        with self.assertRaises(Exception) as ctx:
            self.svc.submit_demand(UNION_TOKEN, self.round_id, {"items": ["x"]}, "a6")
        self.assertEqual(ctx.exception.code, "authentication_error")

    def test_missing_assumptions_block_linkage_evaluation(self):
        self.svc.submit_demand(UNION_TOKEN, self.round_id, {"items": ["涨薪"]}, "a7")
        result = self.svc.submit_proposal(
            UNION_TOKEN, self.round_id, proposal_payload(), "a8"
        )
        self.assertFalse(result["linkage"]["ok"])
        self.assertIn("经营假设", result["linkage"]["issues"][0])


class VerbatimConfirmationTest(RoundFixture):
    prefix = "conf"

    def test_both_sides_must_confirm_identical_version(self):
        h = self.submit_proposal("v1")
        first = self.svc.confirm_version(UNION_TOKEN, self.round_id, h, "v1:c1")
        self.assertIsNone(first["frozen"])
        frozen = self.svc.confirm_version(ENTERPRISE_TOKEN, self.round_id, h, "v1:c2")
        self.assertIsNotNone(frozen["frozen"])
        self.assertEqual(frozen["frozen"]["candidate_hash"], h)

    def test_different_texts_do_not_freeze(self):
        h1 = self.submit_proposal("v2")
        other = proposal_payload(wages={"base_monthly_wage": "5300"})
        h2 = self.svc.submit_proposal(ENTERPRISE_TOKEN, self.round_id, other, "v2:p2")["hash"]
        self.assertNotEqual(h1, h2)
        self.svc.confirm_version(UNION_TOKEN, self.round_id, h1, "v2:c1")
        # 企业方确认的是另一个版本，不能冻结。
        result = self.svc.confirm_version(ENTERPRISE_TOKEN, self.round_id, h2, "v2:c2")
        self.assertIsNone(result["frozen"])
        view = self.svc.round_view(UNION_TOKEN, self.round_id)
        self.assertTrue(view["open_disagreements"])

    def test_whitespace_difference_is_a_different_version(self):
        h1 = self.submit_proposal("v3")
        variant = proposal_payload()
        variant["remark"] = "  补充说明  "
        h2 = self.svc.submit_proposal(ENTERPRISE_TOKEN, self.round_id, variant, "v3:p2")["hash"]
        self.assertNotEqual(h1, h2, "措辞不同的反建议必须得到不同版本号")

    def test_confirmed_text_is_traceable_after_signing(self):
        h = self.submit_proposal("v4")
        self.freeze(h, "v4")
        self.ratify("v4")
        agreement_id = self.sign_round("v4")
        ledger = self.svc.agreement_view(UNION_TOKEN, agreement_id)["ledger"]
        self.assertEqual(ledger["version_hash"], h)
        self.assertIn("GZ-01", ledger["text"])
        self.assertIn("SHA-256", ledger["text"])


class ReplacementTest(RoundFixture):
    prefix = "rep"

    def test_predecessor_opinions_are_not_inherited(self):
        h = self.submit_proposal("r1")
        self.svc.confirm_version(UNION_TOKEN, self.round_id, h, "r1:c1")
        self.svc.confirm_version(ENTERPRISE_TOKEN, self.round_id, h, "r1:c2")
        # 冻结后更换职工代表：候选被撤销，回到协商状态。
        self.svc.appoint_representative(
            FACILITATOR, self.round_id, "union", "u-new", "新任职工代表", "r1:swap"
        )
        view = self.svc.round_view("u-new", self.round_id)
        self.assertEqual(view["status"], "in_negotiation")
        proposal_doc = [d for d in view["documents"] if d["type"] == "proposal"][0]
        self.assertFalse(proposal_doc["from_current_representative"])
        self.assertNotIn("union", proposal_doc["confirmed_by_current"])
        # 前任的确认留在历史上，但不计入现任集合。
        self.assertIn("union", proposal_doc["confirmed_by"])
        # 新任必须重新确认，之后才能再次冻结。
        self.svc.confirm_version("u-new", self.round_id, h, "r1:c3")
        refrozen = self.svc.round_view("u-new", self.round_id)
        self.assertEqual(refrozen["status"], "awaiting_ratification")

    def test_predecessor_vote_does_not_carry_to_successor(self):
        h = self.submit_proposal("r2")
        self.freeze(h, "r2")
        self.svc.cast_vote(UNION_TOKEN, self.round_id, "approve", "r2:v1")
        self.svc.cast_vote(ENTERPRISE_TOKEN, self.round_id, "approve", "r2:v2")
        # 双方批准后更换企业代表：签署时必须发现现任未表决。
        self.svc.appoint_representative(
            FACILITATOR, self.round_id, "enterprise", "e-new", "新任企业代表", "r2:swap"
        )
        with self.assertRaises(ConflictError):
            self.svc.sign("e-new", self.round_id, "r2:s1")
        # 前任的批准不自动继承，新代表需要重新走确认与表决。
        view = self.svc.round_view("e-new", self.round_id)
        self.assertEqual(view["status"], "in_negotiation")

    def test_replacement_keeps_history_for_audit(self):
        self.svc.submit_demand(UNION_TOKEN, self.round_id, {"items": ["旧诉求"]}, "r3:d")
        self.svc.appoint_representative(
            FACILITATOR, self.round_id, "union", "u-new", "新任职工代表", "r3:swap"
        )
        view = self.svc.round_view("u-new", self.round_id)
        demand = [d for d in view["documents"] if d["type"] == "demand"][0]
        self.assertEqual(demand["author_generation"], 1)
        self.assertFalse(demand["from_current_representative"])
        self.assertTrue(any("提交诉求" in t for t in view["todos"]))


class LinkageTest(RoundFixture):
    prefix = "link"

    def test_partial_package_is_rejected(self):
        bad = {"package": {"wages": {"base_monthly_wage": "5200"}}, "caliber": {}}
        with self.assertRaises(ValidationError) as ctx:
            self.svc.submit_proposal(UNION_TOKEN, self.round_id, bad, "l1")
        self.assertTrue(any("hours" in i for i in ctx.exception.issues))

    def test_hourly_wage_must_not_drop_when_hours_rise(self):
        # 月薪涨 6% 但工时涨 10%：时薪反而下降，联动不通过。
        h = self.submit_proposal("l2", hours={"standard_hours_month": "191"})
        self.svc.confirm_version(UNION_TOKEN, self.round_id, h, "l2:c1")
        result = self.svc.confirm_version(ENTERPRISE_TOKEN, self.round_id, h, "l2:c2")
        self.assertIsNone(result["frozen"])
        self.assertFalse(result["blocked_by_linkage"]["ok"])
        report = self.svc.evaluate(UNION_TOKEN, self.round_id, h)
        self.assertFalse(report["ok"])
        self.assertTrue(any("时薪" in i for i in report["issues"]))

    def test_total_cost_must_fit_affordability(self):
        # 工资 +300/人、福利 +50/人、100 人 = 35000，上限 30000 时不通过。
        assumptions = dict(VALID_ASSUMPTIONS, monthly_affordability_budget="30000")
        self.svc.submit_demand(UNION_TOKEN, self.round_id, {"items": ["涨薪"]}, "l3:d")
        self.svc.submit_assumptions(ENTERPRISE_TOKEN, self.round_id, assumptions, "l3:a")
        h = self.svc.submit_proposal(UNION_TOKEN, self.round_id, proposal_payload(), "l3:p")["hash"]
        self.svc.confirm_version(UNION_TOKEN, self.round_id, h, "l3:c1")
        result = self.svc.confirm_version(ENTERPRISE_TOKEN, self.round_id, h, "l3:c2")
        self.assertIsNone(result["frozen"])
        report = result["blocked_by_linkage"]
        self.assertTrue(any("超过可承担上限" in i for i in report["issues"]))

    def test_below_minimum_wage_rejected(self):
        h = self.submit_proposal("l4", wages={"base_monthly_wage": "2000"})
        report = self.svc.evaluate(UNION_TOKEN, self.round_id, h)
        self.assertFalse(report["ok"])
        self.assertTrue(any("最低工资" in i for i in report["issues"]))

    def test_overtime_cap_respected(self):
        h = self.submit_proposal("l5", hours={"max_overtime_hours_month": "60"})
        report = self.svc.evaluate(UNION_TOKEN, self.round_id, h)
        self.assertFalse(report["ok"])
        self.assertTrue(any("法定上限" in i for i in report["issues"]))

    def test_vote_requires_frozen_candidate(self):
        self.submit_proposal("l6")
        with self.assertRaises(ConflictError):
            self.svc.cast_vote(UNION_TOKEN, self.round_id, "approve", "l6:v1")

    def test_valid_package_passes_and_freezes(self):
        h = self.submit_proposal("l7")
        self.svc.confirm_version(UNION_TOKEN, self.round_id, h, "l7:c1")
        result = self.svc.confirm_version(ENTERPRISE_TOKEN, self.round_id, h, "l7:c2")
        self.assertIsNotNone(result["frozen"])
        metrics = result["frozen"]["linkage"]["metrics"]
        self.assertEqual(metrics["monthly_new_cost_total"], "35000.00")
        self.assertEqual(metrics["monthly_headroom"], "5000.00")

    def test_rejected_version_must_be_revised_not_refrozen(self):
        h = self.submit_proposal("l8")
        self.freeze(h, "l8")
        self.svc.cast_vote(UNION_TOKEN, self.round_id, "approve", "l8:v1")
        rejected = self.svc.cast_vote(ENTERPRISE_TOKEN, self.round_id, "reject", "l8:v2")
        self.assertEqual(rejected["outcome"], "rejected_back_to_negotiation")
        # 重新确认同一个旧版本：被现任否决过，不得自动回到表决台。
        again = self.svc.confirm_version(ENTERPRISE_TOKEN, self.round_id, h, "l8:c3")
        self.assertIsNone(again["frozen"])
        with self.assertRaises(ConflictError):
            self.svc.cast_vote(UNION_TOKEN, self.round_id, "approve", "l8:v3")
        # 企业方提出反建议（新版本），双方重新逐字确认后才能再表决。
        revised = proposal_payload(wages={"base_monthly_wage": "5150", "allowance_monthly": "0"})
        h2 = self.svc.submit_proposal(ENTERPRISE_TOKEN, self.round_id, revised, "l8:p2")["hash"]
        self.assertNotEqual(h, h2)
        self.svc.confirm_version(UNION_TOKEN, self.round_id, h2, "l8:c4")
        refrozen = self.svc.confirm_version(ENTERPRISE_TOKEN, self.round_id, h2, "l8:c5")
        self.assertIsNotNone(refrozen["frozen"])


class IdempotencyTest(RoundFixture):
    prefix = "idem"

    def test_same_request_id_reuses_original_result(self):
        first = self.svc.submit_demand(UNION_TOKEN, self.round_id, {"items": ["涨薪"]}, "i1")
        again = self.svc.submit_demand(UNION_TOKEN, self.round_id, {"items": ["涨薪"]}, "i1")
        self.assertEqual(first, again)
        docs = self.store.query_all(
            "SELECT * FROM documents WHERE round_id=? AND doc_type='demand'", (self.round_id,)
        )
        self.assertEqual(len(docs), 1, "重试不得产生第二条记录")

    def test_same_request_id_with_different_body_exposes_conflict(self):
        self.svc.submit_demand(UNION_TOKEN, self.round_id, {"items": ["涨薪"]}, "i2")
        with self.assertRaises(IdempotencyConflict) as ctx:
            self.svc.submit_demand(UNION_TOKEN, self.round_id, {"items": ["不同诉求"]}, "i2")
        self.assertEqual(ctx.exception.original_operation, "submit_demand")
        # 原结果不被覆盖。
        view = self.svc.round_view(UNION_TOKEN, self.round_id)
        demand = [d for d in view["documents"] if d["type"] == "demand"][0]
        self.assertIn("涨薪", demand["rendered_text"])

    def test_request_id_cannot_be_reused_for_other_operation(self):
        self.svc.submit_demand(UNION_TOKEN, self.round_id, {"items": ["涨薪"]}, "i3")
        with self.assertRaises(IdempotencyConflict):
            self.svc.submit_assumptions(
                ENTERPRISE_TOKEN, self.round_id, dict(VALID_ASSUMPTIONS), "i3"
            )

    def test_idempotency_survives_restart(self):
        import tempfile
        from collective_bargaining_ledger import BargainingService, Store

        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/ledger.db"
            svc1 = BargainingService(Store(path))
            svc1.appoint_officer("facilitator", FACILITATOR, "主持人")
            rid = svc1.open_round(FACILITATOR, "持久化回合", "p1")["round_id"]
            svc1.store.close()
            # 进程重启：同一业务号沿用原结果，而不是开启第二个回合。
            svc2 = BargainingService(Store(path))
            again = svc2.open_round(FACILITATOR, "持久化回合", "p1")
            self.assertEqual(again["round_id"], rid)
            count = svc2.store.query_one("SELECT COUNT(*) AS c FROM rounds")["c"]
            self.assertEqual(count, 1)
            svc2.store.close()


class SigningConcurrencyTest(RoundFixture):
    prefix = "sign"

    def test_concurrent_signing_yields_exactly_one_agreement(self):
        h = self.submit_proposal("s1")
        self.freeze(h, "s1")
        self.ratify("s1")
        results: list[dict] = []
        errors: list[Exception] = []

        def sign(token: str, request_id: str) -> None:
            try:
                results.append(self.svc.sign(token, self.round_id, request_id))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=sign, args=(UNION_TOKEN, "s1:sa")),
            threading.Thread(target=sign, args=(ENTERPRISE_TOKEN, "s1:sb")),
            threading.Thread(target=sign, args=(UNION_TOKEN, "s1:sc")),
            threading.Thread(target=sign, args=(ENTERPRISE_TOKEN, "s1:sd")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors, f"并发签署不应报错: {errors}")
        agreements = self.store.query_all(
            "SELECT * FROM agreements WHERE round_id=?", (self.round_id,)
        )
        self.assertEqual(len(agreements), 1, "并发签署不得产生两份有效协议")
        effective = [r for r in results if r["state"] == "effective"]
        self.assertEqual(len(effective), 1)
        signatures = self.store.query_all(
            "SELECT * FROM signatures WHERE round_id=?", (self.round_id,)
        )
        sides = {s["side"] for s in signatures}
        self.assertEqual(sides, {"union", "enterprise"})

    def test_double_sign_same_side_is_idempotent_noop(self):
        h = self.submit_proposal("s2")
        self.freeze(h, "s2")
        self.ratify("s2")
        first = self.svc.sign(UNION_TOKEN, self.round_id, "s2:sa")
        self.assertEqual(first["state"], "pending_counterparty")
        second = self.svc.sign(UNION_TOKEN, self.round_id, "s2:sb")
        self.assertEqual(second["state"], "pending_counterparty")
        agreements = self.store.query_all(
            "SELECT * FROM agreements WHERE round_id=?", (self.round_id,)
        )
        self.assertEqual(len(agreements), 0, "单方签署不得产生协议（半份事务）")

    def test_signing_requires_ratification(self):
        h = self.submit_proposal("s3")
        self.freeze(h, "s3")
        with self.assertRaises(ConflictError):
            self.svc.sign(UNION_TOKEN, self.round_id, "s3:sa")

    def test_signed_round_is_immutable(self):
        h = self.submit_proposal("s4")
        self.freeze(h, "s4")
        self.ratify("s4")
        self.sign_round("s4")
        with self.assertRaises(ConflictError):
            self.svc.submit_proposal(UNION_TOKEN, self.round_id, proposal_payload(), "s4:p2")
        with self.assertRaises(ConflictError):
            self.svc.confirm_version(UNION_TOKEN, self.round_id, h, "s4:c9")


class PerformanceLedgerTest(RoundFixture):
    prefix = "perf"

    def setUp(self):
        super().setUp()
        self.agreement_id = self.reach_signed("pf")

    def test_append_only_ledger_by_period(self):
        self.svc.append_performance(
            UNION_TOKEN, self.agreement_id, "2026-10", "full",
            {"paid_wage": "5300", "hours_actual": "174"}, "pf:p1",
        )
        self.svc.append_performance(
            ENTERPRISE_TOKEN, self.agreement_id, "2026-11", "partial",
            {"paid_wage": "5100", "shortfall": "200"}, "pf:p2",
        )
        self.svc.raise_dispute(
            UNION_TOKEN, self.agreement_id, "2026-11", "11月少发200元津贴", "pf:d1"
        )
        ledger = self.svc.agreement_view(UNION_TOKEN, self.agreement_id)["ledger"]
        self.assertEqual([p["period"] for p in ledger["performance"]], ["2026-10", "2026-11"])
        self.assertEqual(ledger["performance"][1]["status"], "partial")
        self.assertEqual(ledger["disputes"][0]["status"], "open")
        self.svc.resolve_dispute(
            FACILITATOR, self.agreement_id, 1, "企业次月补发并书面致歉", "pf:rd1"
        )
        ledger = self.svc.agreement_view(UNION_TOKEN, self.agreement_id)["ledger"]
        self.assertEqual(ledger["disputes"][0]["status"], "resolved")

    def test_supplement_requires_counterparty_acceptance(self):
        proposed = self.svc.propose_supplement(
            UNION_TOKEN, self.agreement_id, "自2026-12起增设年度体检一次", "pf:sup1"
        )
        with self.assertRaises(ConflictError):
            self.svc.accept_supplement(
                UNION_TOKEN, self.agreement_id, proposed["seq"], "pf:sup2"
            )
        accepted = self.svc.accept_supplement(
            ENTERPRISE_TOKEN, self.agreement_id, proposed["seq"], "pf:sup3"
        )
        self.assertEqual(accepted["status"], "effective")
        ledger = self.svc.agreement_view(UNION_TOKEN, self.agreement_id)["ledger"]
        self.assertEqual(ledger["supplements"][0]["status"], "effective")
        # 原条款不因补充约定而改动。
        self.assertEqual(ledger["clauses"][0]["code"], "GZ-01")

    def test_personal_statement_masked_by_role(self):
        self.svc.append_performance(
            UNION_TOKEN, self.agreement_id, "2026-10", "full",
            {"paid_wage": "5300"}, "pf:m1",
            statement="我是张三，电话13800000000，家住幸福路1号",
        )
        union_view = self.svc.agreement_view(UNION_TOKEN, self.agreement_id)["ledger"]
        enterprise_view = self.svc.agreement_view(ENTERPRISE_TOKEN, self.agreement_id)["ledger"]
        reviewer_view = self.svc.agreement_view(REVIEWER, self.agreement_id)["ledger"]
        self.assertIn("张三", union_view["performance"][0]["personal_statement"])
        masked = enterprise_view["performance"][0]["personal_statement"]
        self.assertNotIn("张三", masked)
        self.assertNotIn("13800000000", masked)
        self.assertIn("职工方代表", masked)
        self.assertNotIn("张三", reviewer_view["performance"][0]["personal_statement"])

    def test_third_party_cannot_append(self):
        with self.assertRaises(AuthorizationError):
            self.svc.append_performance(
                FACILITATOR, self.agreement_id, "2026-10", "full", {"x": 1}, "pf:x1"
            )


class ReviewTest(RoundFixture):
    prefix = "rev"

    def setUp(self):
        super().setUp()
        self.agreement_id = self.reach_signed("rv")

    def test_new_evidence_opens_review_without_touching_clauses(self):
        before = self.svc.agreement_view(UNION_TOKEN, self.agreement_id)["ledger"]
        review = self.svc.request_review(
            UNION_TOKEN, self.agreement_id,
            "当地最低工资标准上调至2800元", "市政府2026年第12号通知", "rv:r1",
        )
        decision = self.svc.decide_review(REVIEWER, review["review_id"], "accepted", "受理", "rv:d1")
        after = self.svc.agreement_view(UNION_TOKEN, self.agreement_id)["ledger"]
        self.assertEqual(before["clauses"], after["clauses"], "原协议条款不得被改动")
        self.assertEqual(before["text"], after["text"])
        self.assertEqual(after["status"], "effective")
        self.assertIn("successor_round_id", decision)

    def test_review_round_requires_full_process_again(self):
        review = self.svc.request_review(
            ENTERPRISE_TOKEN, self.agreement_id,
            "订单量下降30%", "2026年三季度审计报告", "rv:r2",
        )
        decision = self.svc.decide_review(REVIEWER, review["review_id"], "accepted", "受理", "rv:d2")
        new_round = decision["successor_round_id"]
        # 新回合没有任何代表，必须重新任命。
        with self.assertRaises(AuthorizationError):
            self.svc.submit_demand(UNION_TOKEN, new_round, {"items": ["x"]}, "rv:x1")
        self.svc.appoint_representative(
            FACILITATOR, new_round, "union", "u3", "职工代表", "rv:u3"
        )
        self.svc.appoint_representative(
            FACILITATOR, new_round, "enterprise", "e3", "企业代表", "rv:e3"
        )
        self.svc.submit_demand("u3", new_round, {"items": ["按新最低工资调整"]}, "rv:d3")
        # 新工资基数 5300 + 津贴 100 + 福利 50，月增 450/人，100 人需 45000，
        # 企业据此给出更高的可承担上限假设，联动方可整体通过。
        new_assumptions = dict(VALID_ASSUMPTIONS, monthly_affordability_budget="50000")
        self.svc.submit_assumptions("e3", new_round, new_assumptions, "rv:a3")
        new_pkg = proposal_payload(wages={"base_monthly_wage": "5300"})
        h = self.svc.submit_proposal("u3", new_round, new_pkg, "rv:p3")["hash"]
        self.svc.confirm_version("u3", new_round, h, "rv:c3")
        self.svc.confirm_version("e3", new_round, h, "rv:c4")
        self.svc.cast_vote("u3", new_round, "approve", "rv:v3")
        self.svc.cast_vote("e3", new_round, "approve", "rv:v4")
        self.svc.sign("u3", new_round, "rv:s3")
        result = self.svc.sign("e3", new_round, "rv:s4")
        self.assertEqual(result["state"], "effective")
        # 原协议依旧原样可查。
        old = self.svc.agreement_view(UNION_TOKEN, self.agreement_id)["ledger"]
        self.assertEqual(old["clauses"][0]["text"], "月工资基数为 5200 元。")

    def test_rejected_review_keeps_everything(self):
        review = self.svc.request_review(
            UNION_TOKEN, self.agreement_id, "理由", "证据", "rv:r3"
        )
        self.svc.decide_review(REVIEWER, review["review_id"], "rejected", "证据不足", "rv:d3")
        ledger = self.svc.agreement_view(UNION_TOKEN, self.agreement_id)["ledger"]
        self.assertEqual(ledger["reviews"][0]["status"], "rejected")
        self.assertEqual(ledger["status"], "effective")

    def test_review_requires_evidence(self):
        with self.assertRaises(ValidationError):
            self.svc.request_review(UNION_TOKEN, self.agreement_id, "想改", "", "rv:r4")


class ViewTest(RoundFixture):
    prefix = "view"

    def test_each_side_sees_own_todos_and_shared_commitments(self):
        self.submit_basics("vw")
        h = self.svc.submit_proposal(
            UNION_TOKEN, self.round_id, proposal_payload(), "vw:p"
        )["hash"]
        union_view = self.svc.round_view(UNION_TOKEN, self.round_id)
        enterprise_view = self.svc.round_view(ENTERPRISE_TOKEN, self.round_id)
        self.assertTrue(any("确认" in t for t in union_view["todos"]))
        self.assertTrue(any("确认" in t for t in enterprise_view["todos"]))
        self.assertEqual(union_view["effective_commitments"], [])
        self.freeze(h, "vw")
        self.ratify("vw")
        agreement_id = self.sign_round("vw")
        union_view = self.svc.round_view(UNION_TOKEN, self.round_id)
        enterprise_view = self.svc.round_view(ENTERPRISE_TOKEN, self.round_id)
        self.assertEqual(
            union_view["effective_commitments"]["version_hash"],
            enterprise_view["effective_commitments"]["version_hash"],
        )
        self.assertEqual(
            union_view["effective_commitments"]["clauses"],
            enterprise_view["effective_commitments"]["clauses"],
        )
        dash_u = self.svc.dashboard(UNION_TOKEN)
        dash_e = self.svc.dashboard(ENTERPRISE_TOKEN)
        self.assertEqual(len(dash_u["effective_commitments"]), 1)
        self.assertEqual(len(dash_e["effective_commitments"]), 1)
        self.assertEqual(dash_u["identity"]["side"], "union")
        self.assertEqual(dash_e["identity"]["side"], "enterprise")

    def test_dashboard_shows_open_disagreements(self):
        self.submit_proposal("vw2")
        dash = self.svc.dashboard(ENTERPRISE_TOKEN)
        kinds = {d["kind"] for d in dash["open_disagreements"]}
        self.assertIn("version_not_confirmed", kinds)


if __name__ == "__main__":
    unittest.main()
