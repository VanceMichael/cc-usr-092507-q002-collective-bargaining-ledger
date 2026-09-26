import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

STAFF = "staff-token"

ASSUMPTIONS = {
    "headcount": 100,
    "monthly_capacity": 200_000,
    "legal_monthly_min": 2690,
    "legal_weekly_max": 40,
    "legal_overtime_monthly_max": 36,
}
BASES = {"current_monthly_wage": 6000}

GOOD_CLAUSES = {
    "wages": {"monthly_min": 3000, "monthly_raise_pct": 5},
    "hours": {"weekly_max": 40, "overtime_monthly_max": 30},
    "benefits": {"monthly_cost_person": 100},
}


def make_service(tmp_path):
    from collective_bargaining_ledger.service import BargainingService
    return BargainingService(str(tmp_path / "ledger.db"), staff_token=STAFF)


def register_both(svc):
    w = svc.register_representative(STAFF, "worker", "w-001", "王代表")
    c = svc.register_representative(STAFF, "company", "c-001", "陈代表")
    return w["mandate_id"], c["mandate_id"]


def seal_agreement(svc, w, c, *, clauses=None, assumptions=None, bases=None,
                   title="年度工资集体协商", agreement_only=False):
    """走完整流程：陈述→口径接受→同文确认→表决→原子签署。"""
    assumptions = assumptions or ASSUMPTIONS
    bases = bases or BASES
    clauses = clauses or GOOD_CLAUSES
    neg = svc.open_negotiation(w, title, business_no=f"open-{title}")
    neg_id = neg["negotiation_id"]
    svc.submit_statement(c, neg_id, "assumptions", assumptions)
    base_stmt = svc.submit_statement(w, neg_id, "calculation_bases", bases)
    svc.accept_statement(c, base_stmt["statement_id"])
    proposed = svc.propose_package(
        w, neg_id, clauses, assumptions=assumptions, bases=bases,
        business_no=f"propose-{title}")
    assert proposed["linkage"]["violations"] == [], proposed["linkage"]
    vh = proposed["version_hash"]
    svc.confirm_version(w, neg_id, vh)
    svc.confirm_version(c, neg_id, vh)
    svc.cast_vote(w, neg_id, vh, True)
    svc.cast_vote(c, neg_id, vh, True)
    first = svc.sign_agreement(w, neg_id, vh)
    assert first["sealed"] is False
    second = svc.sign_agreement(c, neg_id, vh)
    assert second["sealed"] is True
    agreement = second["agreement"]
    if agreement_only:
        return agreement
    return neg_id, vh, agreement
