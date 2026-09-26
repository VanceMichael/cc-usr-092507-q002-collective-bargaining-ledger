"""HTTP JSON 接口。

仅依赖标准库。工会与企业各自携带代表令牌调用同一组接口，
服务按令牌所属角色返回各自的待办、未决分歧与共同有效承诺。
所有写命令要求业务号（请求体 ``request_id`` 或 ``Idempotency-Key`` 头）。
"""

from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .errors import BargainingError
from .pipeline import PipelineRunner, bootstrap_artifacts, bootstrap_steps
from .service import BargainingService
from .storage import Store


class ApiContext:
    def __init__(self, store: Store):
        self.store = store
        self.service = BargainingService(store)


def _token_from(handler: BaseHTTPRequestHandler) -> str:
    raw = handler.headers.get("Authorization", "")
    if raw.startswith("Bearer "):
        return raw[7:].strip()
    return handler.headers.get("X-Auth-Token", "").strip()


def make_handler(ctx: ApiContext) -> type[BaseHTTPRequestHandler]:
    service = ctx.service

    class Handler(BaseHTTPRequestHandler):
        server_version = "CBLedger/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # 静音测试输出
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                raise BargainingError("请求体不是合法 JSON")
            if not isinstance(value, dict):
                raise BargainingError("请求体必须是 JSON 对象")
            header_key = self.headers.get("Idempotency-Key")
            if header_key and "request_id" not in value:
                value["request_id"] = header_key
            return value

        def _send(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, fn: Callable[[], Any]) -> None:
            try:
                result = fn()
            except BargainingError as exc:
                body = {"error": exc.code, "message": str(exc)}
                if hasattr(exc, "issues"):
                    body["issues"] = exc.issues
                if hasattr(exc, "original_operation"):
                    body["original_operation"] = exc.original_operation
                    body["original_payload_hash"] = exc.original_hash
                self._send(exc.http_status, body)
            except Exception as exc:  # 未知错误不吞，返回 500
                self._send(500, {"error": "internal_error", "message": repr(exc)})
            else:
                self._send(200, result if result is not None else {"ok": True})

        def _require_request_id(self, body: dict[str, Any]) -> str:
            request_id = body.get("request_id")
            if not request_id:
                raise BargainingError("写操作必须提供业务号 request_id 或 Idempotency-Key 头")
            return str(request_id)

        # 路由 ----------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            token = _token_from(self)

            def dispatch():
                if parts == ["dashboard"]:
                    return service.dashboard(token)
                if len(parts) == 2 and parts[0] == "rounds":
                    return service.round_view(token, parts[1])
                if len(parts) == 2 and parts[0] == "agreements":
                    return service.agreement_view(token, parts[1])
                if len(parts) == 3 and parts[0] == "rounds" and parts[2] == "evaluate":
                    from urllib.parse import parse_qs

                    query = parse_qs(parsed.query)
                    version_hash = query.get("version_hash", [None])[0]
                    return service.evaluate(token, parts[1], version_hash)
                raise BargainingError("未找到对应资源")

            self._handle(dispatch)

        def do_POST(self) -> None:  # noqa: N802
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            token = _token_from(self)
            body = self._read_json()

            def dispatch():
                return self._route_post(parts, token, body)

            self._handle(dispatch)

        def _route_post(self, parts: list[str], token: str, body: dict[str, Any]):
            service = ctx.service
            # 一次性初始化：尚无任何身份时建立主持人与复核人，令牌只下发一次。
            if parts == ["setup"]:
                return self._setup(body)
            if parts == ["bootstrap-pipeline"]:
                return self._bootstrap_pipeline(token, body)

            if parts == ["rounds"]:
                return service.open_round(
                    token, body["title"], self._require_request_id(body)
                )
            if len(parts) == 3 and parts[0] == "rounds" and parts[2] == "representatives":
                return service.appoint_representative(
                    token, parts[1], body["side"], body["token"],
                    body.get("display_name", "代表"), self._require_request_id(body),
                )
            # /rounds/{id}/<resource>
            if len(parts) == 3 and parts[0] == "rounds":
                rid, resource = parts[1], parts[2]
                request_id = self._require_request_id(body)
                if resource == "demands":
                    return service.submit_demand(token, rid, body["payload"], request_id)
                if resource == "assumptions":
                    return service.submit_assumptions(token, rid, body["payload"], request_id)
                if resource == "proposals":
                    return service.submit_proposal(token, rid, body["payload"], request_id)
                if resource == "confirmations":
                    return service.confirm_version(
                        token, rid, body["version_hash"], request_id
                    )
                if resource == "votes":
                    return service.cast_vote(token, rid, body["vote"], request_id)
                if resource == "signatures":
                    return service.sign(token, rid, request_id)
            if parts[:1] == ["agreements"]:
                return self._route_agreement(parts, token, body)
            if len(parts) == 3 and parts[0] == "reviews" and parts[2] == "decision":
                return service.decide_review(
                    token, int(parts[1]), body["decision"], body.get("note", ""),
                    self._require_request_id(body), title=body.get("title"),
                )
            raise BargainingError("未找到对应资源")

        def _route_agreement(self, parts: list[str], token: str, body: dict[str, Any]):
            service = ctx.service
            # parts: agreements / {id} / ...
            if len(parts) == 3:
                aid, resource = parts[1], parts[2]
                request_id = self._require_request_id(body)
                if resource == "performance":
                    return service.append_performance(
                        token, aid, body["period"], body["status"], body["facts"],
                        request_id, statement=body.get("statement"),
                    )
                if resource == "disputes":
                    return service.raise_dispute(
                        token, aid, body["period"], body["description"], request_id
                    )
                if resource == "supplements":
                    return service.propose_supplement(
                        token, aid, body["text"], request_id
                    )
                if resource == "reviews":
                    return service.request_review(
                        token, aid, body["reason"], body["evidence"], request_id,
                        proposed_payload=body.get("proposed_payload"),
                    )
            if len(parts) == 5 and parts[2] == "disputes" and parts[4] == "resolution":
                return service.resolve_dispute(
                    token, parts[1], int(parts[3]), body["resolution"],
                    self._require_request_id(body),
                )
            if len(parts) == 5 and parts[2] == "supplements" and parts[4] == "accept":
                return service.accept_supplement(
                    token, parts[1], int(parts[3]), self._require_request_id(body)
                )
            raise BargainingError("未找到对应资源")

        def _setup(self, body: dict[str, Any]) -> dict[str, Any]:
            count = ctx.store.query_one("SELECT COUNT(*) AS c FROM identities")["c"]
            if count:
                raise BargainingError("系统已初始化，不能重复建立主持人")
            facilitator_token = "fac-" + secrets.token_hex(12)
            reviewer_token = "rev-" + secrets.token_hex(12)
            service.appoint_officer(
                "facilitator", facilitator_token,
                body.get("facilitator_name", "园区工会主持人"),
            )
            service.appoint_officer(
                "reviewer", reviewer_token,
                body.get("reviewer_name", "协议复核人员"),
            )
            return {"facilitator_token": facilitator_token,
                    "reviewer_token": reviewer_token}

        def _bootstrap_pipeline(self, token: str, body: dict[str, Any]) -> dict[str, Any]:
            # 该接口本身是准备动作集合，每步业务号在 key_prefix 下固定；
            # 终止后再次调用即从检查点继续。
            key_prefix = body.get("key_prefix", "bootstrap-round")
            _, steps = bootstrap_steps(
                token, body.get("title", "新一轮工资集体协商"),
                key_prefix=key_prefix,
            )
            runner = PipelineRunner(ctx.store)
            result = runner.run(f"pipeline:{key_prefix}", key_prefix, steps)
            artifacts = bootstrap_artifacts(key_prefix)
            return {**result, "artifacts": artifacts}

    return Handler


def create_server(db_path: str, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    store = Store(db_path)
    ctx = ApiContext(store)
    server = ThreadingHTTPServer((host, port), make_handler(ctx))
    server.store = store  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="集体协商与履约账本 HTTP 服务")
    parser.add_argument("--db", default="ledger.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    server = create_server(args.db, args.host, args.port)
    print(f"集体协商服务监听 http://{args.host}:{args.port}（数据库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.store.close()


if __name__ == "__main__":
    main()
