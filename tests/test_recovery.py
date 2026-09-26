"""进程被强制终止后重新启动，履约核查从原检查点继续的证明性测试。

第一次恢复在子进程中执行，处理完第一个周期后由进程内 ``SIGKILL`` 自杀；
第二次通过命令行在全新进程中重启同一 run_id，已提交的周期跳过、不重复入账。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collective_bargaining_ledger.service import BargainingService
from tests.helpers import STAFF, make_service, register_both, seal_agreement

KILL_SCRIPT = (
    "import os, sys; sys.path.insert(0, %r);"
    "from collective_bargaining_ledger.service import BargainingService;"
    "svc = BargainingService(%r, staff_token=%r);"
    "svc.resume_performance_check(%r, %r)"
)


class PerformanceCheckRecoveryTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.db = str(self.tmp / "ledger.db")
        svc = make_service(self.tmp)
        worker, company = register_both(svc)
        _, _, agreement = seal_agreement(svc, worker, company, title="核查协议")
        self.aid = agreement["id"]
        # 10 月实际履行；11 月部分履行；12 月争议未决；2027-01 无记录
        svc.append_performance(company, self.aid, "2026-10", "full",
                               {"wage": "已足额"})
        svc.append_performance(company, self.aid, "2026-11", "partial",
                               {"paid_ratio": 0.7})
        svc.append_performance(worker, self.aid, "2026-12", "dispute",
                               {"issue": "福利未发放"})
        self.periods = ["2026-10", "2026-11", "2026-12", "2027-01"]
        self.run_id = "run-recovery-demo"
        started = svc.start_performance_check(
            STAFF, self.aid, self.periods, run_id=self.run_id)
        self.assertFalse(started["resumed"])

    def tearDown(self):
        self._dir.cleanup()

    def _kill_midway(self):
        env = {**os.environ, "CB_CHECK_KILL_AFTER": "1",
               "PYTHONPATH": str(ROOT / "src")}
        code = KILL_SCRIPT % (str(ROOT / "src"), self.db, STAFF,
                              STAFF, self.run_id)
        proc = subprocess.run([sys.executable, "-c", code], env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, -9, proc.stderr)

    def _cli_status(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
               "CB_STAFF_TOKEN": STAFF}
        proc = subprocess.run(
            [sys.executable, "-m", "collective_bargaining_ledger.cli",
             "--db", self.db, "check-status", self.run_id],
            env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def _cli_resume(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
               "CB_STAFF_TOKEN": STAFF}
        proc = subprocess.run(
            [sys.executable, "-m", "collective_bargaining_ledger.cli",
             "--db", self.db, "check-resume", self.run_id],
            env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_restart_continues_from_last_checkpoint(self):
        # 1. 子进程处理完首个周期即被 SIGKILL
        self._kill_midway()
        status = self._cli_status()
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["done"], 1)
        self.assertEqual(status["total"], 4)
        self.assertEqual(status["cursor_key"], "period:2026-10")

        # 2. 全新进程从命令行重启，跳过已完成周期，只处理剩余三个
        resumed = self._cli_resume()
        self.assertEqual(resumed["status"], "done")
        by_period = {r["period"]: r for r in resumed["results"]}
        self.assertEqual(len(by_period), 4)
        self.assertEqual(by_period["2026-10"]["status"], "done")
        self.assertEqual(by_period["2026-11"]["status"], "failed")
        self.assertEqual(by_period["2026-12"]["status"], "failed")
        self.assertEqual(by_period["2027-01"]["status"], "failed")

        # 3. 再次重启：run 已完成，结果原样返回且没有重复检查项
        again = self._cli_resume()
        self.assertEqual(again["results"], resumed["results"])
        final = self._cli_status()
        self.assertEqual(final["done"], 4)
        self.assertEqual(final["status"], "done")

        # 4. 直接核对落库的检查项：每个周期恰好一条
        import sqlite3
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(
                "SELECT item_key FROM check_items WHERE run_id = ? "
                "ORDER BY item_key", (self.run_id,)).fetchall()
        finally:
            conn.close()
        self.assertEqual([r[0] for r in rows],
                         [f"period:{p}" for p in self.periods])

    def test_resume_with_different_period_list_is_conflict(self):
        self._kill_midway()
        svc = BargainingService(self.db, staff_token=STAFF)
        from collective_bargaining_ledger.errors import TextConflictError
        with self.assertRaises(TextConflictError):
            svc.start_performance_check(
                STAFF, self.aid, ["2026-10", "2026-11"], run_id=self.run_id)

    def test_resuming_unknown_run_is_not_found(self):
        svc = BargainingService(self.db, staff_token=STAFF)
        from collective_bargaining_ledger.errors import NotFoundError
        with self.assertRaises(NotFoundError):
            svc.resume_performance_check(STAFF, "run-does-not-exist")


if __name__ == "__main__":
    unittest.main()
