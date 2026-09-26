import unittest

from tests.helpers import (
    ASSUMPTIONS,
    BASES,
    GOOD_CLAUSES,
    STAFF,
    make_service,
    register_both,
    seal_agreement,
)
from collective_bargaining_ledger import (
    AuthorizationError,
    ConflictError,
    LinkageError,
    MandateEndedError,
    NotFoundError,
    StateError,
    TextConflictError,
)

import tempfile
from pathlib import Path


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.svc = make_service(self.tmp)
        self.worker, self.company = register_both(self.svc)

    def tearDown(self):
        self._dir.cleanup()


class AuthorizationTest(ServiceCase):
    def test_commands_require_valid_mandate(self):
        with self.assertRaises(AuthorizationError):
            self.svc.open_negotiation("not-a-mandate", "协商")
        with self.assertRaises(AuthorizationError):
            self.svc.open_negotiation("", "协商")

    def test_staff_endpoints_require_staff_token(self):
        with self.assertRaises(AuthorizationError):
            self.svc.register_representative("wrong", "worker", "x", "x")

    def test_replacement_ends_old_mandate_immediately(self):
        neg = self.svc.open_negotiation(self.worker, "第一轮")["negotiation_id"]
        new_worker = self.svc.register_representative(
            STAFF, "worker", "w-002", "李代表")
        self.assertEqual(new_worker["replaced_mandates"], [self.worker])
        # 旧授权的一切写操作立即失效
        with self.assertRaises(MandateEndedError):
            self.svc.submit_statement(self.worker, neg, "demands", {"x": 1})

    def test_predecessor_unaccepted_opinion_does_not_carry_over(self):
        neg = self.svc.open_negotiation(self.worker, "第一轮")["negotiation_id"]
        stmt = self.svc.submit_statement(
            self.worker, neg, "demands", {"raise": "10%"})
        # 企业方尚未接受，职工方就换了代表
        self.svc.register_representative(STAFF, "worker", "w-002", "李代表")
        new_worker = self.svc.list_mandates(STAFF, "worker")[-1]["mandate_id"]
        statements = self.svc.list_statements(new_worker, neg)
        self.assertEqual(statements[0]["status"], "lapsed")
        with self.assertRaises(StateError):
            self.svc.accept_statement(self.company, stmt["statement_id"])

    def test_accepted_opinion_survives_replacement_as_shared_fact(self):
        neg = self.svc.open_negotiation(self.worker, "第一轮")["negotiation_id"]
        stmt = self.svc.submit_statement(
            self.worker, neg, "calculation_bases", {"current_monthly_wage": 6000})
        self.svc.accept_statement(self.company, stmt["statement_id"])
        self.svc.register_representative(STAFF, "worker", "w-002", "李代表")
        statements = self.svc.list_statements(
            self.svc.list_mandates(STAFF, "worker")[-1]["mandate_id"], neg)
        self.assertEqual(statements[0]["status"], "accepted")

    def test_new_representative_must_reconfirm_and_revote(self):
        neg = seal_agreement_setup(self.svc, self.worker, self.company)
        vh = neg["version_hash"]
        self.svc.confirm_version(self.worker, neg["id"], vh)
        self.svc.confirm_version(self.company, neg["id"], vh)
        # 职工方换人：其确认不再算作有效确认
        self.svc.register_representative(STAFF, "worker", "w-002", "李代表")
        new_worker = self.svc.list_mandates(STAFF, "worker")[-1]["mandate_id"]
        with self.assertRaises(StateError):
            self.svc.cast_vote(new_worker, neg["id"], vh, True)
        # 新任代表重新确认后才能表决
        self.svc.confirm_version(new_worker, neg["id"], vh)
        self.svc.cast_vote(new_worker, neg["id"], vh, True)


class NegotiationFlowTest(ServiceCase):
    def _prepare(self):
        return seal_agreement_setup(self.svc, self.worker, self.company)

    def test_only_identical_version_confirmed_by_both_can_be_voted(self):
        neg = self._prepare()
        vh = neg["version_hash"]
        # 双方确认前不能表决
        with self.assertRaises(StateError):
            self.svc.cast_vote(self.worker, neg["id"], vh, True)
        self.svc.confirm_version(self.worker, neg["id"], vh)
        with self.assertRaises(StateError):
            self.svc.cast_vote(self.company, neg["id"], vh, True)
        self.svc.confirm_version(self.company, neg["id"], vh)
        tally = self.svc.cast_vote(self.worker, neg["id"], vh, True)["tally"]
        self.assertEqual(tally, {"yes": 1, "no": 0})

    def test_divergent_texts_are_exposed_not_silently_matched(self):
        neg = self._prepare()
        vh_a = neg["version_hash"]
        # 企业方拿出不同版本（福利不同）
        other_clauses = {**GOOD_CLAUSES,
                         "benefits": {"monthly_cost_person": 0}}
        vh_b = self.svc.propose_package(
            self.company, neg["id"], other_clauses,
            assumptions=ASSUMPTIONS, bases=BASES)["version_hash"]
        self.assertNotEqual(vh_a, vh_b)
        self.svc.confirm_version(self.worker, neg["id"], vh_a)
        self.svc.confirm_version(self.company, neg["id"], vh_b)
        # 看板必须把异文列为未解决分歧
        dash = self.svc.dashboard(self.worker)
        divergence = [d for d in dash["disagreements"]
                      if d["type"] == "version_divergence"]
        self.assertEqual(len(divergence), 1)
        # 任一方都不能对"双方共同确认"之外的文本表决
        with self.assertRaises(StateError):
            self.svc.cast_vote(self.worker, neg["id"], vh_a, True)

    def test_linkage_violation_blocks_confirmation_and_vote(self):
        neg_id = self.svc.open_negotiation(self.worker, "失衡方案")["negotiation_id"]
        bad = {**GOOD_CLAUSES,
               "wages": {"monthly_min": 3000, "monthly_raise_pct": 50}}
        proposed = self.svc.propose_package(
            self.worker, neg_id, bad, assumptions=ASSUMPTIONS, bases=BASES)
        self.assertTrue(proposed["linkage"]["violations"])
        vh = proposed["version_hash"]
        with self.assertRaises(LinkageError):
            self.svc.confirm_version(self.worker, neg_id, vh)

    def test_signing_requires_both_votes(self):
        neg = self._prepare()
        vh = neg["version_hash"]
        self.svc.confirm_version(self.worker, neg["id"], vh)
        self.svc.confirm_version(self.company, neg["id"], vh)
        self.svc.cast_vote(self.worker, neg["id"], vh, True)
        self.svc.cast_vote(self.company, neg["id"], vh, False)
        with self.assertRaises(StateError):
            self.svc.sign_agreement(self.worker, neg["id"], vh)

    def test_single_signature_is_not_half_an_agreement(self):
        neg = self._prepare()
        vh = neg["version_hash"]
        self.svc.confirm_version(self.worker, neg["id"], vh)
        self.svc.confirm_version(self.company, neg["id"], vh)
        self.svc.cast_vote(self.worker, neg["id"], vh, True)
        self.svc.cast_vote(self.company, neg["id"], vh, True)
        result = self.svc.sign_agreement(self.worker, neg["id"], vh)
        self.assertFalse(result["sealed"])
        self.assertIn("waiting_for", result)
        self.assertEqual(self.svc.list_agreements(self.worker), [])

    def test_full_flow_seals_atomic_agreement(self):
        neg_id, vh, agreement = seal_agreement(
            self.svc, self.worker, self.company, title="完整流程")
        self.assertTrue(agreement["id"].startswith("agr-"))
        self.assertEqual(len(agreement["signatures"]), 2)
        self.assertEqual(agreement["version_hash"], vh)
        # 生效后协商关闭，不能再改
        with self.assertRaises(StateError):
            self.svc.submit_statement(self.worker, neg_id, "demands", {})

    def test_vote_cannot_be_silently_changed(self):
        neg = self._prepare()
        vh = neg["version_hash"]
        self.svc.confirm_version(self.worker, neg["id"], vh)
        self.svc.confirm_version(self.company, neg["id"], vh)
        self.svc.cast_vote(self.company, neg["id"], vh, False)
        with self.assertRaises(TextConflictError):
            self.svc.cast_vote(self.company, neg["id"], vh, True)


class IdempotencyTest(ServiceCase):
    def test_same_business_no_replays_original_result(self):
        first = self.svc.open_negotiation(
            self.worker, "标题", business_no="BN-1")
        second = self.svc.open_negotiation(
            self.worker, "标题", business_no="BN-1")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["negotiation_id"], second["negotiation_id"])

    def test_same_no_different_text_exposes_conflict(self):
        self.svc.open_negotiation(self.worker, "标题", business_no="BN-2")
        with self.assertRaises(TextConflictError):
            self.svc.open_negotiation(self.worker, "改了标题", business_no="BN-2")

    def test_same_no_across_representatives_exposes_conflict(self):
        self.svc.open_negotiation(self.worker, "标题", business_no="BN-3")
        with self.assertRaises(TextConflictError):
            self.svc.open_negotiation(self.company, "标题", business_no="BN-3")


class LedgerTest(ServiceCase):
    def setUp(self):
        super().setUp()
        _, _, self.agreement = seal_agreement(
            self.svc, self.worker, self.company, title="履约测试")
        self.aid = self.agreement["id"]

    def test_performance_appended_by_period(self):
        entry = self.svc.append_performance(
            self.company, self.aid, "2026-10", "full",
            {"wage_paid": 6300}, business_no="P-1")
        again = self.svc.append_performance(
            self.company, self.aid, "2026-10", "full",
            {"wage_paid": 6300}, business_no="P-1")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["entry_id"], entry["entry_id"])
        ledger = self.svc.list_ledger(self.worker, self.aid)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["kind"], "full")

    def test_duplicate_period_record_is_rejected_not_overwritten(self):
        self.svc.append_performance(
            self.company, self.aid, "2026-10", "full", {"a": 1})
        with self.assertRaises(ConflictError):
            self.svc.append_performance(
                self.company, self.aid, "2026-10", "partial", {"a": 2})
        # different kind in same period by same side also blocked; other side ok
        self.svc.append_performance(
            self.worker, self.aid, "2026-10", "full", {"b": 1})
        ledger = self.svc.list_ledger(self.company, self.aid)
        self.assertEqual(len(ledger), 2)

    def test_supplement_needs_both_consents_and_never_edits_clauses(self):
        sup = self.svc.append_supplement(
            self.worker, self.aid, "2026-11",
            {"item": "高温补贴", "amount": 200})
        self.assertFalse(sup["effective"])
        partial = self.svc.consent_supplement(self.worker, sup["entry_id"])
        self.assertFalse(partial["effective"])
        full = self.svc.consent_supplement(self.company, sup["entry_id"])
        self.assertTrue(full["effective"])
        # 原协议条款逐字未变
        agreement = self.svc.get_agreement(self.worker, self.aid)
        self.assertEqual(agreement["document"], self.agreement["document"])
        self.assertEqual(agreement["version_hash"], self.agreement["version_hash"])

    def test_dispute_shows_on_dashboard_until_resolved(self):
        self.svc.append_performance(
            self.worker, self.aid, "2026-11", "dispute",
            {"issue": "加班费未足额"})
        worker_dash = self.svc.dashboard(self.worker)
        company_dash = self.svc.dashboard(self.company)
        self.assertTrue(any(d["type"] == "unresolved_dispute"
                            for d in worker_dash["disagreements"]))
        self.assertTrue(any(t["type"] == "respond_dispute"
                            for t in company_dash["todos"]))
        # 双方以补充约定了结争议
        sup = self.svc.append_supplement(
            self.company, self.aid, "2026-11",
            {"resolves_entry_id": 1, "plan": "次月补发"})
        self.svc.consent_supplement(self.company, sup["entry_id"])
        self.svc.consent_supplement(self.worker, sup["entry_id"])
        self.assertFalse(any(d["type"] == "unresolved_dispute"
                             for d in self.svc.dashboard(self.worker)["disagreements"]))

    def test_append_requires_active_agreement(self):
        with self.assertRaises(NotFoundError):
            self.svc.append_performance(
                self.company, "agr-missing", "2026-10", "full", {})


class ReconsiderationTest(ServiceCase):
    def test_new_evidence_opens_negotiation_but_keeps_old_terms(self):
        _, _, agreement = seal_agreement(
            self.svc, self.worker, self.company, title="原协议")
        aid = agreement["id"]
        before = self.svc.get_agreement(self.company, aid)
        rec = self.svc.request_reconsideration(
            self.worker, aid, {"cpi": "上涨3%"}, business_no="RC-1")
        # 同号重试沿用原结果
        rec2 = self.svc.request_reconsideration(
            self.worker, aid, {"cpi": "上涨3%"}, business_no="RC-1")
        self.assertTrue(rec2["replayed"])
        self.assertEqual(rec["negotiation_id"], rec2["negotiation_id"])
        # 原协议仍为 active 且文本逐字未动
        during = self.svc.get_agreement(self.worker, aid)
        self.assertEqual(during["status"], "active")
        self.assertEqual(during["document"], before["document"])

    def test_cannot_sign_second_agreement_without_reconsideration(self):
        _, _, agreement = seal_agreement(
            self.svc, self.worker, self.company, title="协议一")
        neg_id = self.svc.open_negotiation(self.worker, "另起炉灶")["negotiation_id"]
        vh = self.svc.propose_package(
            self.worker, neg_id, GOOD_CLAUSES,
            assumptions=ASSUMPTIONS, bases=BASES)["version_hash"]
        self.svc.confirm_version(self.worker, neg_id, vh)
        self.svc.confirm_version(self.company, neg_id, vh)
        self.svc.cast_vote(self.worker, neg_id, vh, True)
        self.svc.cast_vote(self.company, neg_id, vh, True)
        self.svc.sign_agreement(self.worker, neg_id, vh)
        with self.assertRaises(ConflictError):
            self.svc.sign_agreement(self.company, neg_id, vh)
        # 原协议仍是唯一生效协议
        actives = [a for a in self.svc.list_agreements(self.worker)
                   if a["status"] == "active"]
        self.assertEqual([a["id"] for a in actives], [agreement["id"]])

    def test_sealed_reconsideration_supersedes_old_agreement(self):
        _, _, old = seal_agreement(
            self.svc, self.worker, self.company, title="旧协议")
        rec = self.svc.request_reconsideration(
            self.worker, old["id"], {"reason": "经营回暖"})
        neg_id = rec["negotiation_id"]
        new_clauses = {**GOOD_CLAUSES,
                       "wages": {"monthly_min": 3200, "monthly_raise_pct": 6}}
        new_assump = {**ASSUMPTIONS, "monthly_capacity": 500_000}
        vh = self.svc.propose_package(
            self.worker, neg_id, new_clauses,
            assumptions=new_assump, bases=BASES)["version_hash"]
        self.svc.confirm_version(self.worker, neg_id, vh)
        self.svc.confirm_version(self.company, neg_id, vh)
        self.svc.cast_vote(self.worker, neg_id, vh, True)
        self.svc.cast_vote(self.company, neg_id, vh, True)
        self.svc.sign_agreement(self.worker, neg_id, vh)
        sealed = self.svc.sign_agreement(self.company, neg_id, vh)
        self.assertEqual(sealed["superseded_agreement_id"], old["id"])
        statuses = {a["id"]: a["status"]
                    for a in self.svc.list_agreements(self.worker)}
        self.assertEqual(statuses[old["id"]], "superseded")
        self.assertEqual(statuses[sealed["agreement"]["id"]], "active")


class ImmutabilityTest(ServiceCase):
    def test_signed_and_ledger_rows_cannot_be_mutated_or_deleted(self):
        import sqlite3
        _, _, agreement = seal_agreement(
            self.svc, self.worker, self.company, title="不可变测试")
        aid = agreement["id"]
        self.svc.append_performance(
            self.company, aid, "2026-10", "full", {"ok": True})
        conn = sqlite3.connect(str(self.tmp / "ledger.db"))
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE agreements SET document_json = ? WHERE id = ?",
                    ('{"tampered": true}', aid))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM agreements WHERE id = ?", (aid,))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE ledger_entries SET content_json = ? "
                             "WHERE agreement_id = ?", ('{"x":1}', aid))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM ledger_entries WHERE agreement_id = ?",
                             (aid,))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE signatures SET name = ?", ("伪造",))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM signatures")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE statements SET content_json = ? WHERE 1",
                             ("{}",))
        finally:
            conn.close()


def seal_agreement_setup(svc, worker, company):
    """提交假设、口径与条款包，返回带 id/version_hash 的待确认协商。"""
    neg_id = svc.open_negotiation(worker, "待确认协商",
                                  business_no="setup-neg")["negotiation_id"]
    svc.submit_statement(company, neg_id, "assumptions", ASSUMPTIONS)
    base_stmt = svc.submit_statement(
        worker, neg_id, "calculation_bases", BASES)
    svc.accept_statement(company, base_stmt["statement_id"])
    proposed = svc.propose_package(
        worker, neg_id, GOOD_CLAUSES, assumptions=ASSUMPTIONS, bases=BASES)
    return {"id": neg_id, "version_hash": proposed["version_hash"]}


if __name__ == "__main__":
    unittest.main()
