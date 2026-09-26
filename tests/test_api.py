"""HTTP 接口：工会与企业各自调用，看到各自的待办、分歧与共同承诺。"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collective_bargaining_ledger.api import create_server  # noqa: E402

from helpers import VALID_ASSUMPTIONS, proposal_payload  # noqa: E402


class ApiTest(unittest.TestCase):
    """每个用例一个独立服务实例（独立内存库），互不污染。"""

    def setUp(self) -> None:
        self.server = create_server(":memory:")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        _, setup = self.call("POST", "/setup", {})
        self.fac = setup["facilitator_token"]
        self.rev = setup["reviewer_token"]
        _, r = self.call(
            "POST", "/rounds", {"title": "HTTP协商回合", "request_id": "api:open"}, self.fac
        )
        self.rid = r["round_id"]
        for side, token, name, req in (
            ("union", "api-u", "职工代表", "api:u"),
            ("enterprise", "api-e", "企业代表", "api:e"),
        ):
            status, resp = self.call(
                "POST", f"/rounds/{self.rid}/representatives",
                {"side": side, "token": token, "display_name": name, "request_id": req},
                self.fac,
            )
            self.assertEqual(status, 200, resp)
        self.u, self.e = "api-u", "api-e"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server.store.close()

    def call(self, method: str, path: str, body=None, token=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method
        )
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(req, data=data, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_negotiation_over_http(self):
        status, d = self.call(
            "POST", f"/rounds/{self.rid}/demands",
            {"payload": {"items": ["月薪上调至5200元"]}, "request_id": "api:d1"}, self.u,
        )
        self.assertEqual(status, 200, d)
        status, _ = self.call(
            "POST", f"/rounds/{self.rid}/assumptions",
            {"payload": dict(VALID_ASSUMPTIONS), "request_id": "api:a1"}, self.e,
        )
        self.assertEqual(status, 200)
        status, p = self.call(
            "POST", f"/rounds/{self.rid}/proposals",
            {"payload": proposal_payload(), "request_id": "api:p1"}, self.u,
        )
        self.assertEqual(status, 200, p)
        self.assertTrue(p["linkage"]["ok"], p["linkage"]["issues"])
        h = p["hash"]
        c = None
        for token, req in ((self.u, "api:c1"), (self.e, "api:c2")):
            status, c = self.call(
                "POST", f"/rounds/{self.rid}/confirmations",
                {"version_hash": h, "request_id": req}, token,
            )
            self.assertEqual(status, 200, c)
        self.assertIsNotNone(c["frozen"])
        v = None
        for token, req in ((self.u, "api:v1"), (self.e, "api:v2")):
            status, v = self.call(
                "POST", f"/rounds/{self.rid}/votes",
                {"vote": "approve", "request_id": req}, token,
            )
            self.assertEqual(status, 200, v)
        self.assertEqual(v["outcome"], "ratified")
        status, s1 = self.call(
            "POST", f"/rounds/{self.rid}/signatures", {"request_id": "api:s1"}, self.u
        )
        self.assertEqual(s1["state"], "pending_counterparty")
        status, s2 = self.call(
            "POST", f"/rounds/{self.rid}/signatures", {"request_id": "api:s2"}, self.e
        )
        self.assertEqual(s2["state"], "effective")
        aid = s2["agreement_id"]

        # 履约与脱敏：职工方附个人陈述，企业方视图必须脱敏。
        status, _ = self.call(
            "POST", f"/agreements/{aid}/performance",
            {"period": "2026-10", "status": "full",
             "facts": {"paid_wage": "5300"},
             "statement": "我是李四，工号A1024", "request_id": "api:perf1"},
            self.u,
        )
        self.assertEqual(status, 200)
        _, enterprise_view = self.call("GET", f"/agreements/{aid}", token=self.e)
        statement = enterprise_view["ledger"]["performance"][0]["personal_statement"]
        self.assertNotIn("李四", statement)
        _, union_view = self.call("GET", f"/agreements/{aid}", token=self.u)
        self.assertIn("李四", union_view["ledger"]["performance"][0]["personal_statement"])
        self.assertEqual(
            enterprise_view["ledger"]["clauses"], union_view["ledger"]["clauses"]
        )

    def test_role_scoped_views(self):
        self.call(
            "POST", f"/rounds/{self.rid}/demands",
            {"payload": {"items": ["涨薪"]}, "request_id": "api2:d1"}, self.u,
        )
        _, union_view = self.call("GET", f"/rounds/{self.rid}", token=self.u)
        _, enterprise_view = self.call("GET", f"/rounds/{self.rid}", token=self.e)
        self.assertTrue(any("经营假设" in t for t in enterprise_view["todos"]))
        self.assertFalse(any("经营假设" in t for t in union_view["todos"]))
        _, dash_u = self.call("GET", "/dashboard", token=self.u)
        _, dash_e = self.call("GET", "/dashboard", token=self.e)
        self.assertEqual(dash_u["identity"]["side"], "union")
        self.assertEqual(dash_e["identity"]["side"], "enterprise")

    def test_authentication_and_authorization(self):
        status, err = self.call("GET", f"/rounds/{self.rid}")
        self.assertEqual(status, 401)
        status, err = self.call(
            "POST", f"/rounds/{self.rid}/demands",
            {"payload": {"items": ["越权"]}, "request_id": "api3:d1"}, self.e,
        )
        self.assertEqual(status, 403)
        self.assertEqual(err["error"], "authorization_error")

    def test_idempotency_over_http(self):
        body = {"payload": {"items": ["涨薪"]}, "request_id": "api4:d1"}
        _, first = self.call("POST", f"/rounds/{self.rid}/demands", body, self.u)
        _, again = self.call("POST", f"/rounds/{self.rid}/demands", body, self.u)
        self.assertEqual(first, again)
        status, conflict = self.call(
            "POST", f"/rounds/{self.rid}/demands",
            {"payload": {"items": ["不同内容"]}, "request_id": "api4:d1"}, self.u,
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "idempotency_conflict")
        self.assertEqual(conflict["original_operation"], "submit_demand")

    def test_idempotency_key_header(self):
        status, _ = self.call(
            "POST", f"/rounds/{self.rid}/demands",
            {"payload": {"items": ["头业务号"]}}, self.u,
        )
        self.assertEqual(status, 400)  # 缺少业务号
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/rounds/{self.rid}/demands", method="POST"
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.u}")
        req.add_header("Idempotency-Key", "api5:header")
        with urllib.request.urlopen(
            req, data=json.dumps({"payload": {"items": ["头业务号"]}}).encode(), timeout=10
        ) as resp:
            result = json.loads(resp.read())
        self.assertIn("hash", result)

    def test_bootstrap_pipeline_endpoint_resumes(self):
        # 接口形式的检查点流水线：重复调用从检查点继续，不重复开回合。
        body = {"title": "接口流水线回合", "key_prefix": "kp-http"}
        status, first = self.call("POST", "/bootstrap-pipeline", body, self.fac)
        self.assertEqual(status, 200, first)
        self.assertEqual(first["state"]["status"], "completed")
        status, second = self.call("POST", "/bootstrap-pipeline", body, self.fac)
        self.assertEqual(status, 200, second)
        self.assertEqual(second["executed"], [])
        self.assertEqual(len(second["resumed_skipped"]), 3)
        # bootstrap 与 /setup 的回合之外只新增了一个回合。
        rows = self.server.store.query_all("SELECT round_id FROM rounds")
        self.assertEqual(len(rows), 1 + 1)


if __name__ == "__main__":
    unittest.main()
