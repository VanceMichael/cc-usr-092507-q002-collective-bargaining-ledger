"""检查点流水线：进程被终止后重启，从原检查进度继续，不产生重复副作用。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collective_bargaining_ledger import (  # noqa: E402
    BargainingService,
    PipelineRunner,
    Step,
    Store,
    bootstrap_artifacts,
    bootstrap_steps,
)

SRC = str(Path(__file__).resolve().parents[1] / "src")


class PipelineRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.directory.name, "ledger.db")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _run_child(self, script: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONPATH=SRC)
        return subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, env=env, timeout=60,
        )

    def test_restart_resumes_from_checkpoint_after_sigkill(self):
        # 第一段：跑到 appoint_union 检查点后被硬终止（os._exit，无清理）。
        first = self._run_child(textwrap.dedent(f"""
            from collective_bargaining_ledger import BargainingService, Store, PipelineRunner, bootstrap_steps
            store = Store({self.db_path!r})
            svc = BargainingService(store)
            svc.appoint_officer("facilitator", "fac-token", "主持人")
            _, steps = bootstrap_steps("fac-token", "恢复测试回合", key_prefix="kp1")
            PipelineRunner(store).run("pipeline:kp1", "kp1", steps, crash_after="appoint_union")
        """))
        self.assertEqual(first.returncode, 86, first.stderr)

        # 第二段：新进程、同一数据库，应从检查点继续。
        second = self._run_child(textwrap.dedent(f"""
            from collective_bargaining_ledger import Store, PipelineRunner, bootstrap_steps
            store = Store({self.db_path!r})
            runner = PipelineRunner(store)
            before = runner.state("pipeline:kp1")
            assert before["status"] == "running", before
            assert [c["step_name"] for c in before["checkpoints"]] == ["open_round", "appoint_union"], before
            _, steps = bootstrap_steps("fac-token", "恢复测试回合", key_prefix="kp1")
            result = runner.run("pipeline:kp1", "kp1", steps)
            assert result["executed"] == ["appoint_enterprise"], result
            assert result["resumed_skipped"] == ["open_round", "appoint_union"], result
            assert result["state"]["status"] == "completed", result
            # 副作用精确一次：只有一个回合、每方只有一任代表。
            assert store.query_one("SELECT COUNT(*) AS c FROM rounds")["c"] == 1
            assert store.query_one("SELECT COUNT(*) AS c FROM identities WHERE kind='representative'")["c"] == 2
            attempts = {{c["step_name"]: c["attempts"] for c in result["state"]["checkpoints"]}}
            assert attempts == {{"open_round": 1, "appoint_union": 1, "appoint_enterprise": 1}}, attempts
            print("RESUMED_OK")
        """))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("RESUMED_OK", second.stdout)

    def test_crash_between_action_and_checkpoint_reuses_idempotent_result(self):
        # 崩溃窗口：动作已提交、检查点尚未落盘。重试用同一业务号，沿用原结果。
        first = self._run_child(textwrap.dedent(f"""
            import os
            from collective_bargaining_ledger import BargainingService, PipelineRunner, Step, Store
            store = Store({self.db_path!r})
            svc = BargainingService(store)
            svc.appoint_officer("facilitator", "fac-token", "主持人")

            def open_then_die(service):
                result = service.open_round("fac-token", "窗口崩溃回合", "w1:open", round_id="R-window")
                os._exit(87)  # 动作已提交，检查点未写

            def appoint(service):
                return service.appoint_representative(
                    "fac-token", "R-window", "union", "w-u", "职工代表", "w1:u")

            steps = [Step("open", "w1:open", open_then_die), Step("appoint", "w1:u", appoint)]
            PipelineRunner(store).run("pipeline:w1", "w1", steps)
        """))
        self.assertEqual(first.returncode, 87, first.stderr)

        second = self._run_child(textwrap.dedent(f"""
            from collective_bargaining_ledger import BargainingService, PipelineRunner, Step, Store
            store = Store({self.db_path!r})
            svc = BargainingService(store)

            def open_again(service):
                # 同一业务号重试：必须沿用原结果而不是报"回合已存在"。
                return service.open_round("fac-token", "窗口崩溃回合", "w1:open", round_id="R-window")

            def appoint(service):
                return service.appoint_representative(
                    "fac-token", "R-window", "union", "w-u", "职工代表", "w1:u")

            steps = [Step("open", "w1:open", open_again), Step("appoint", "w1:u", appoint)]
            result = PipelineRunner(store).run("pipeline:w1", "w1", steps)
            assert result["state"]["status"] == "completed", result
            assert store.query_one("SELECT COUNT(*) AS c FROM rounds")["c"] == 1
            attempts = {{c["step_name"]: c["attempts"] for c in result["state"]["checkpoints"]}}
            assert attempts["open"] == 2, attempts  # 第二次尝试复用了幂等结果
            assert attempts["appoint"] == 1, attempts
            print("WINDOW_OK")
        """))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("WINDOW_OK", second.stdout)

    def test_completed_pipeline_is_noop_on_restart(self):
        store = Store(self.db_path)
        svc = BargainingService(store)
        svc.appoint_officer("facilitator", "fac-token", "主持人")
        _, steps = bootstrap_steps("fac-token", "幂等回合", key_prefix="kp2")
        runner = PipelineRunner(store)
        first = runner.run("pipeline:kp2", "kp2", steps)
        self.assertEqual(len(first["executed"]), 3)
        # 模拟进程重启后再次运行：全部跳过。
        store.close()
        store2 = Store(self.db_path)
        second = PipelineRunner(store2).run("pipeline:kp2", "kp2", steps)
        self.assertEqual(second["executed"], [])
        self.assertEqual(len(second["resumed_skipped"]), 3)
        self.assertEqual(store2.query_one("SELECT COUNT(*) AS c FROM rounds")["c"], 1)
        store2.close()

    def test_resumed_round_is_usable(self):
        # 第一段（子进程）：开启回合后被硬终止。
        first = self._run_child(textwrap.dedent(f"""
            from collective_bargaining_ledger import BargainingService, Store, PipelineRunner, bootstrap_steps
            store = Store({self.db_path!r})
            svc = BargainingService(store)
            svc.appoint_officer("facilitator", "fac-token", "主持人")
            _, steps = bootstrap_steps("fac-token", "可用性回合", key_prefix="kp3")
            PipelineRunner(store).run("pipeline:kp3", "kp3", steps, crash_after="open_round")
        """))
        self.assertEqual(first.returncode, 86, first.stderr)
        # 第二段：重启续跑，恢复出的回合可直接进入协商。
        store2 = Store(self.db_path)
        svc2 = BargainingService(store2)
        _, steps = bootstrap_steps("fac-token", "可用性回合", key_prefix="kp3")
        result = PipelineRunner(store2).run("pipeline:kp3", "kp3", steps)
        self.assertEqual(result["resumed_skipped"], ["open_round"])
        artifacts = bootstrap_artifacts("kp3")
        view = svc2.round_view(artifacts["union_token"], artifacts["round_id"])
        self.assertEqual(view["status"], "in_negotiation")
        self.assertTrue(view["todos"])
        store2.close()


if __name__ == "__main__":
    unittest.main()
