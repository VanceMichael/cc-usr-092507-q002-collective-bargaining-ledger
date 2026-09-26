"""并发安全测试：并发签署不得产生两份有效协议或半份事务。

线程用例验证同进程多连接下的 BEGIN IMMEDIATE 串行化；
多进程用例模拟工会端与企业端真实分别调用接口。
"""

import json
import multiprocessing as mp
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collective_bargaining_ledger.service import BargainingService

STAFF = "concurrent-staff"
ASSUMPTIONS = {
    "headcount": 100, "monthly_capacity": 200_000,
    "legal_monthly_min": 2690, "legal_weekly_max": 40,
    "legal_overtime_monthly_max": 36,
}
BASES = {"current_monthly_wage": 6000}
CLAUSES = {
    "wages": {"monthly_min": 3000, "monthly_raise_pct": 5},
    "hours": {"weekly_max": 40, "overtime_monthly_max": 30},
    "benefits": {"monthly_cost_person": 100},
}


def _ready_negotiation(db_path):
    """准备一份双方已确认、已表决、只差签署的协商，返回 (neg_id, vh)。"""
    svc = BargainingService(db_path, staff_token=STAFF)
    w = svc.register_representative(STAFF, "worker", "w1", "王")["mandate_id"]
    c = svc.register_representative(STAFF, "company", "c1", "陈")["mandate_id"]
    neg_id = svc.open_negotiation(w, "并发签署")["negotiation_id"]
    svc.submit_statement(c, neg_id, "assumptions", ASSUMPTIONS)
    base = svc.submit_statement(w, neg_id, "calculation_bases", BASES)
    svc.accept_statement(c, base["statement_id"])
    vh = svc.propose_package(
        w, neg_id, CLAUSES, assumptions=ASSUMPTIONS, bases=BASES)["version_hash"]
    svc.confirm_version(w, neg_id, vh)
    svc.confirm_version(c, neg_id, vh)
    svc.cast_vote(w, neg_id, vh, True)
    svc.cast_vote(c, neg_id, vh, True)
    return neg_id, vh, w, c


def _sign_worker(db_path, mandate, neg_id, vh, business_no, out_queue):
    try:
        svc = BargainingService(db_path, staff_token=STAFF)
        result = svc.sign_agreement(
            mandate, neg_id, vh, business_no=business_no)
        out_queue.put(("ok", result.get("sealed"),
                       result.get("agreement", {}).get("id"),
                       bool(result.get("already"))))
    except Exception as exc:  # noqa: BLE001 - 跨进程回传类型与消息
        out_queue.put(("err", type(exc).__name__, str(exc), False))


class ConcurrentSigningTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._dir.name) / "ledger.db")

    def tearDown(self):
        self._dir.cleanup()

    def test_parallel_threads_produce_exactly_one_active_agreement(self):
        neg_id, vh, w, c = _ready_negotiation(self.db_path)
        results: list[tuple] = []
        errors: list[Exception] = []

        def sign(mandate, bn):
            try:
                svc = BargainingService(self.db_path, staff_token=STAFF)
                results.append(svc.sign_agreement(mandate, neg_id, vh,
                                                  business_no=bn))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        # 双方各开多个线程同时签署（含同方重复签署）
        threads = [threading.Thread(target=sign, args=(w, f"bn-w-{i}"))
                   for i in range(5)]
        threads += [threading.Thread(target=sign, args=(c, f"bn-c-{i}"))
                    for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        # 真正在本调用内完成双方齐备并创建协议的恰好一个；
        # 其余 sealed 结果只能是 already=True 的同协议回读
        sealed_results = [r for r in results if r.get("sealed")]
        completers = [r for r in sealed_results if not r.get("already")]
        self.assertEqual(len(completers), 1, results)
        self.assertEqual({r["agreement"]["id"] for r in sealed_results},
                         {completers[0]["agreement"]["id"]})
        svc = BargainingService(self.db_path, staff_token=STAFF)
        actives = [a for a in svc.list_agreements(w) if a["status"] == "active"]
        self.assertEqual(len(actives), 1)
        # 所有签署结果指向同一份协议
        agreement_ids = {r["agreement"]["id"] for r in results
                         if r.get("agreement")}
        self.assertEqual(agreement_ids, {actives[0]["id"]})
        # 签署记录恰好两条（每方一条），无重复、无半份
        conn = __import__("sqlite3").connect(self.db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM signatures WHERE negotiation_id = ?",
                (neg_id,)).fetchone()[0]
            self.assertEqual(n, 2)
            neg_status = conn.execute(
                "SELECT status FROM negotiations WHERE id = ?",
                (neg_id,)).fetchone()[0]
            self.assertEqual(neg_status, "sealed")
        finally:
            conn.close()

    def test_parallel_processes_produce_exactly_one_active_agreement(self):
        ctx = mp.get_context("spawn")
        neg_id, vh, w, c = _ready_negotiation(self.db_path)
        queue = ctx.Queue()
        jobs = []
        # 双方进程同时发起签署；同方多余进程要么成为等待方，要么收到冲突现状
        for i in range(3):
            jobs.append(ctx.Process(
                target=_sign_worker,
                args=(self.db_path, w, neg_id, vh, f"mp-w-{i}", queue)))
        for i in range(3):
            jobs.append(ctx.Process(
                target=_sign_worker,
                args=(self.db_path, c, neg_id, vh, f"mp-c-{i}", queue)))
        for p in jobs:
            p.start()
        for p in jobs:
            p.join(timeout=60)
            self.assertEqual(p.exitcode, 0, f"签署进程异常退出：{p.exitcode}")

        outcomes = [queue.get() for _ in jobs]
        errors = [o for o in outcomes if o[0] == "err"]
        self.assertEqual(errors, [])
        completers = [o for o in outcomes if o[1] is True and not o[3]]
        self.assertEqual(len(completers), 1, outcomes)
        agreement_ids = {o[2] for o in outcomes if o[0] == "ok" and o[2]}
        self.assertEqual(len(agreement_ids), 1)

        conn = __import__("sqlite3").connect(self.db_path)
        try:
            n_active = conn.execute(
                "SELECT COUNT(*) FROM agreements WHERE status = 'active'"
            ).fetchone()[0]
            n_total = conn.execute("SELECT COUNT(*) FROM agreements").fetchone()[0]
            n_sig = conn.execute("SELECT COUNT(*) FROM signatures").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n_active, 1)
        self.assertEqual(n_total, 1)
        self.assertEqual(n_sig, 2)


if __name__ == "__main__":
    unittest.main()
