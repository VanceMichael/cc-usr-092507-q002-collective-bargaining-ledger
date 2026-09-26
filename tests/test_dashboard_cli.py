import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tests.helpers import (
    ASSUMPTIONS,
    BASES,
    GOOD_CLAUSES,
    STAFF,
    make_service,
    register_both,
    seal_agreement,
)


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.svc = make_service(self.tmp)
        self.worker, self.company = register_both(self.svc)

    def tearDown(self):
        self._dir.cleanup()

    def test_each_side_sees_own_todos_and_shared_commitment(self):
        neg_id = self.svc.open_negotiation(
            self.worker, "看板协商")["negotiation_id"]
        # 企业方先提交经营假设，职工方有待办去响应；双方都有待办提交条款包
        self.svc.submit_statement(self.company, neg_id, "assumptions",
                                  ASSUMPTIONS)
        worker_dash = self.svc.dashboard(self.worker)
        company_dash = self.svc.dashboard(self.company)
        self.assertEqual(worker_dash["side"], "worker")
        self.assertTrue(any(t["type"] == "submit_package"
                            for t in worker_dash["todos"]))
        self.assertIsNone(worker_dash["commitments"]["active_agreement"])
        # 身份信息不串侧
        self.assertNotEqual(
            worker_dash["representative"]["mandate_id"],
            company_dash["representative"]["mandate_id"])

        _, _, agreement = seal_agreement(
            self.svc, self.worker, self.company, title="看板协议")
        # 前置的开放协商已无后续动作，由工会终止，避免干扰待办断言
        self.svc.abandon_negotiation(STAFF, neg_id, "另开正式协商")
        self.svc.append_performance(
            self.company, agreement["id"], "2026-10", "full", {"ok": 1})
        for side in (self.worker, self.company):
            dash = self.svc.dashboard(side)
            self.assertEqual(
                dash["commitments"]["active_agreement"]["id"], agreement["id"])
            self.assertEqual(dash["todos"], [])
            self.assertEqual(dash["disagreements"], [])

    def test_personal_statements_are_redacted_per_role(self):
        neg_id = self.svc.open_negotiation(
            self.worker, "个人陈述")["negotiation_id"]
        self.svc.submit_statement(
            self.worker, neg_id, "personal_statement",
            {"public": "愿意轮班", "sensitive": "病史细节"},
            sensitive=True)
        company_view = next(s for s in self.svc.list_statements(
            self.company, neg_id) if s["kind"] == "personal_statement")
        worker_view = next(s for s in self.svc.list_statements(
            self.worker, neg_id) if s["kind"] == "personal_statement")
        self.assertEqual(company_view["content"]["sensitive"], "［依角色脱敏］")
        self.assertNotIn("mandate_id", company_view)
        self.assertEqual(worker_view["content"]["sensitive"], "病史细节")


class CliTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = str(Path(self._dir.name) / "ledger.db")
        self.env = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
                    "CB_STAFF_TOKEN": STAFF, "CB_DB": self.db}

    def tearDown(self):
        self._dir.cleanup()

    def _run(self, *args, env_token=None):
        env = dict(self.env)
        if env_token is not None:
            env["CB_MANDATE"] = env_token
        proc = subprocess.run(
            [sys.executable, "-m",
             "collective_bargaining_ledger.cli", *args],
            env=env, capture_output=True, text=True)
        return proc

    def test_cli_end_to_end(self):
        p = self._run("register-rep", "--side", "worker",
                      "--rep-id", "w1", "--name", "王代表")
        self.assertEqual(p.returncode, 0, p.stderr)
        worker = json.loads(p.stdout)["mandate_id"]
        p = self._run("register-rep", "--side", "company",
                      "--rep-id", "c1", "--name", "陈代表")
        company = json.loads(p.stdout)["mandate_id"]

        p = self._run("open", "--title", "CLI协商", env_token=worker)
        self.assertEqual(p.returncode, 0, p.stderr)
        neg_id = json.loads(p.stdout)["negotiation_id"]

        p = self._run("submit", neg_id, "--kind", "assumptions",
                      "--content", json.dumps(ASSUMPTIONS), env_token=company)
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self._run("submit", neg_id, "--kind", "calculation_bases",
                      "--content", json.dumps(BASES), env_token=worker)
        base_id = json.loads(p.stdout)["statement_id"]
        self._run("accept", str(base_id), env_token=company)

        p = self._run("propose", neg_id, "--clauses", json.dumps(GOOD_CLAUSES),
                      "--assumptions", json.dumps(ASSUMPTIONS),
                      "--bases", json.dumps(BASES), env_token=worker)
        vh = json.loads(p.stdout)["version_hash"]
        self._run("confirm", neg_id, vh, env_token=worker)
        self._run("confirm", neg_id, vh, env_token=company)
        self._run("vote", neg_id, vh, "--yes", env_token=worker)
        self._run("vote", neg_id, vh, "--yes", env_token=company)
        first = self._run("sign", neg_id, vh, env_token=worker)
        self.assertFalse(json.loads(first.stdout)["sealed"])
        second = self._run("sign", neg_id, vh, env_token=company)
        sealed = json.loads(second.stdout)
        self.assertTrue(sealed["sealed"], second.stderr)
        aid = sealed["agreement"]["id"]

        p = self._run("performance", aid, "--period", "2026-10",
                      "--kind", "full", "--content", '{"paid": true}',
                      env_token=company)
        self.assertEqual(p.returncode, 0, p.stderr)

        p = self._run("dashboard", env_token=worker)
        dash = json.loads(p.stdout)
        self.assertEqual(dash["commitments"]["active_agreement"]["id"], aid)

        # 无授权凭据调用双方接口被拒绝
        bad_env = {k: v for k, v in self.env.items() if k != "CB_MANDATE"}
        proc = subprocess.run(
            [sys.executable, "-m",
             "collective_bargaining_ledger.cli", "dashboard"],
            env=bad_env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("授权", proc.stderr)

    def test_cli_rejects_bad_staff_token(self):
        # 先用正确凭据初始化账本（凭据哈希落库后，错误凭据不得通过）
        ok = subprocess.run(
            [sys.executable, "-m",
             "collective_bargaining_ledger.cli", "list-mandates"],
            env=self.env, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        env = {**self.env, "CB_STAFF_TOKEN": "wrong"}
        proc = subprocess.run(
            [sys.executable, "-m",
             "collective_bargaining_ledger.cli", "register-rep",
             "--side", "worker", "--rep-id", "w", "--name", "x"],
            env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("凭据无效", proc.stderr)


if __name__ == "__main__":
    unittest.main()
