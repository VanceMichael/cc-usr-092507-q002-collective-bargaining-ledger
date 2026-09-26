"""集体协商与履约服务。

一条主线贯穿全部命令：

    授权任命 → 各自提交（诉求/经营假设/方案与口径）→ 逐字确认同一版本
    → 工资工时福利整体联动校验 → 双方表决 → 原子会签
    → 按周期追加履约事实/争议/补充约定 →（新证据）发起重新审议，不改旧协议

所有写命令都接受业务号 ``request_id``：相同业务号+相同正文返回原结果，
相同业务号+不同正文直接抛出 :class:`IdempotencyConflict`。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Callable

from .canonical import canonical_json, content_hash
from .errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    IdempotencyConflict,
    NotFoundError,
    ValidationError,
)
from .linkage import evaluate_linkage
from .masking import view_statement
from .render import (
    derive_clauses,
    render_agreement,
    render_supplement,
    render_version,
)
from .storage import Store, loads

SIDES = ("union", "enterprise")
SIDE_LABEL = {"union": "职工方", "enterprise": "企业方"}
PERF_STATUSES = {"full", "partial", "disputed"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rowdict(row) -> dict[str, Any]:
    return dict(row) if row is not None else None


class BargainingService:
    def __init__(self, store: Store):
        self.store = store

    # 身份与授权 ----------------------------------------------------------

    def _hash_token(self, token: str) -> str:
        return content_hash({"token": token})

    def appoint_officer(self, kind: str, token: str, display_name: str) -> dict[str, Any]:
        """登记园区工会主持人或协议复核人员（全局性中立身份）。"""

        if kind not in ("facilitator", "reviewer"):
            raise ValidationError("中立人员类型只能是 facilitator 或 reviewer")
        token_hash = self._hash_token(token)
        with self.store.tx() as conn:
            if conn.execute("SELECT 1 FROM identities WHERE token_hash=?", (token_hash,)).fetchone():
                raise ConflictError("该令牌已登记")
            conn.execute(
                "INSERT INTO identities(token_hash,kind,side,generation,round_id,"
                "display_name,status,appointed_at) VALUES(?,?,?,0,NULL,?,'active',?)",
                (token_hash, kind, kind, display_name, _now()),
            )
        return {"kind": kind, "display_name": display_name, "generation": 0}

    def open_round(
        self, token: str, title: str, request_id: str, round_id: str | None = None
    ) -> dict[str, Any]:
        """园区工会主持人开启新一轮协商。

        ``round_id`` 可由调用方确定性指定（如可恢复流水线）；为空则随机生成。
        """

        actor = self._authenticate(token)
        self._require_kind(actor, "facilitator")
        # 幂等载荷只含调用方输入；round_id 未指定时由系统生成，
        # 重试必须沿用首次生成的回合号而不是再生成一个。
        payload = {"title": title, "round_id": round_id}
        round_id = round_id or ("R-" + secrets.token_hex(6))

        def do(conn) -> dict[str, Any]:
            if conn.execute(
                "SELECT 1 FROM rounds WHERE round_id=?", (round_id,)
            ).fetchone():
                raise ConflictError("回合编号已存在")
            conn.execute(
                "INSERT INTO rounds(round_id,title,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (round_id, title, "in_negotiation", _now(), _now()),
            )
            self._event(conn, round_id, actor, "round_opened", {"title": title})
            return {"round_id": round_id, "status": "in_negotiation"}

        return self._idempotent(request_id, "open_round", payload, do)

    def appoint_representative(
        self, token: str, round_id: str, side: str, new_token: str, display_name: str,
        request_id: str,
    ) -> dict[str, Any]:
        """任命某一方新任代表：前任即时卸任，前任尚未被接受的意见不自动继承。"""

        actor = self._authenticate(token)
        self._require_kind(actor, "facilitator")
        if side not in SIDES:
            raise ValidationError("side 只能是 union 或 enterprise")
        self._require_round(round_id)
        token_hash = self._hash_token(new_token)
        payload = {"round_id": round_id, "side": side, "display_name": display_name}

        def do(conn) -> dict[str, Any]:
            if conn.execute(
                "SELECT 1 FROM identities WHERE token_hash=?", (token_hash,)
            ).fetchone():
                raise ConflictError("该令牌已登记")
            row = conn.execute(
                "SELECT COALESCE(MAX(generation),0) AS g FROM identities "
                "WHERE round_id=? AND side=?",
                (round_id, side),
            ).fetchone()
            generation = row["g"] + 1
            conn.execute(
                "UPDATE identities SET status='revoked',revoked_at=? "
                "WHERE round_id=? AND side=? AND status='active'",
                (_now(), round_id, side),
            )
            conn.execute(
                "INSERT INTO identities(token_hash,kind,side,generation,round_id,"
                "display_name,status,appointed_at) VALUES(?,?,?,?,?,?,'active',?)",
                (token_hash, "representative", side, generation, round_id,
                 display_name, _now()),
            )
            # 人员更换：冻结候选与状态回退。历史确认/表决按任次留痕，
            # 但只有现任任次的确认与表决才算数——意见不自动继承。
            conn.execute(
                "UPDATE rounds SET status='in_negotiation',candidate_hash=NULL,"
                "updated_at=? WHERE round_id=? AND status!='signed'",
                (_now(), round_id),
            )
            self._event(
                conn, round_id, actor, "representative_replaced",
                {"side": side, "generation": generation, "display_name": display_name},
            )
            return {"round_id": round_id, "side": side, "generation": generation}

        return self._idempotent(request_id, "appoint_representative", payload, do)

    # 协商材料提交 --------------------------------------------------------

    def submit_demand(
        self, token: str, round_id: str, payload: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        """职工方在授权内提交诉求。"""

        actor = self._authenticate(token)
        rep = self._require_representative(actor, round_id, "union")
        self._require_round_editable(round_id)
        issues = self._validate_demand(payload)
        if issues:
            raise ValidationError("诉求结构不合法", issues)
        body = {"kind": "demand", "items": list(payload["items"]), "note": payload.get("note")}
        return self._submit_document(round_id, rep, "demand", body, request_id)

    def submit_assumptions(
        self, token: str, round_id: str, payload: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        """企业方在授权内提交经营假设。"""

        actor = self._authenticate(token)
        rep = self._require_representative(actor, round_id, "enterprise")
        self._require_round_editable(round_id)
        issues = self._validate_assumptions(payload)
        if issues:
            raise ValidationError("经营假设结构不合法", issues)
        body = {"kind": "assumptions", **payload}
        return self._submit_document(round_id, rep, "assumptions", body, request_id)

    def submit_proposal(
        self, token: str, round_id: str, payload: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        """任一方提交方案（整包：工资/工时/福利）与测算口径，即反建议载体。"""

        actor = self._authenticate(token)
        rep = self._require_representative(actor, round_id)
        self._require_round_editable(round_id)
        issues = self._validate_proposal_payload(payload)
        if issues:
            raise ValidationError("方案必须整体包含工资、工时、福利与测算口径", issues)
        body = {
            "kind": "proposal",
            "package": payload["package"],
            "caliber": payload["caliber"],
            "remark": payload.get("remark"),
        }
        result = self._submit_document(round_id, rep, "proposal", body, request_id)
        # 提交即试算，双方在同一份口径下看到联动结果。
        result["linkage"] = self._linkage_for(round_id, body)
        return result

    def confirm_version(
        self, token: str, round_id: str, version_hash: str, request_id: str
    ) -> dict[str, Any]:
        """现任代表对某一逐字版本表示确认；双方现任确认同一哈希才冻结候选。"""

        actor = self._authenticate(token)
        rep = self._require_representative(actor, round_id)
        self._require_round_editable(round_id)

        def do(conn) -> dict[str, Any]:
            doc = conn.execute(
                "SELECT * FROM documents WHERE round_id=? AND doc_type='proposal' AND hash=?",
                (round_id, version_hash),
            ).fetchone()
            if doc is None:
                raise NotFoundError("该版本哈希不属于本回合的任何方案")
            exists = conn.execute(
                "SELECT 1 FROM confirmations WHERE round_id=? AND hash=? AND side=? "
                "AND generation=?",
                (round_id, version_hash, rep["side"], rep["generation"]),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO confirmations(hash,round_id,side,generation,"
                    "identity_hash,confirmed_at) VALUES(?,?,?,?,?,?)",
                    (version_hash, round_id, rep["side"], rep["generation"],
                     rep["token_hash"], _now()),
                )
                self._event(
                    conn, round_id, rep, "version_confirmed",
                    {"hash": version_hash},
                )
            frozen = self._try_freeze(conn, round_id)
            if frozen is not None and frozen.get("status") == "blocked_by_linkage":
                return {
                    "round_id": round_id,
                    "confirmed_hash": version_hash,
                    "frozen": None,
                    "blocked_by_linkage": frozen["linkage"],
                }
            return {"round_id": round_id, "confirmed_hash": version_hash, "frozen": frozen}

        payload = {"round_id": round_id, "version_hash": version_hash}
        return self._idempotent(request_id, "confirm_version", payload, do)

    def _try_freeze(self, conn, round_id: str) -> dict[str, Any] | None:
        """若双方现任任次都确认了同一版本，则做联动校验并冻结为交付表决候选。"""

        round_row = conn.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
        if round_row["candidate_hash"]:
            return {"candidate_hash": round_row["candidate_hash"], "status": round_row["status"]}
        row = conn.execute(
            """
            SELECT c.hash AS hash, COUNT(DISTINCT c.side) AS sides FROM confirmations c
            JOIN identities i
              ON i.round_id=? AND i.side=c.side AND i.generation=c.generation
                 AND i.status='active' AND i.kind='representative'
            WHERE c.round_id=?
              AND NOT EXISTS (
                  SELECT 1 FROM votes v
                  JOIN identities vi
                    ON vi.round_id=v.round_id AND vi.side=v.side
                       AND vi.generation=v.generation AND vi.status='active'
                       AND vi.kind='representative'
                  WHERE v.round_id=c.round_id AND v.hash=c.hash AND v.vote='reject'
              )
            GROUP BY c.hash HAVING sides=2
            ORDER BY c.hash LIMIT 1
            """,
            (round_id, round_id),
        ).fetchone()
        if row is None:
            return None
        version_hash = row["hash"]
        doc = conn.execute(
            "SELECT * FROM documents WHERE round_id=? AND hash=?", (round_id, version_hash)
        ).fetchone()
        body = loads(doc["payload"])
        report = self._evaluate_round_linkage(conn, round_id, body)
        conn.execute(
            "INSERT OR IGNORE INTO linkage_reports(hash,round_id,report,created_at) "
            "VALUES(?,?,?,?)",
            (version_hash, round_id, canonical_json(report), _now()),
        )
        if not report["ok"]:
            # 确认本身有效并留痕，但版本不能冻结交付表决；分歧明确暴露给双方。
            self._event(
                conn, round_id, {"display_name": "系统", "token_hash": "", "kind": "system"},
                "freeze_blocked", {"hash": version_hash, "issues": report["issues"]},
            )
            return {"candidate_hash": None, "status": "blocked_by_linkage",
                    "linkage": report}
        conn.execute(
            "UPDATE rounds SET candidate_hash=?,status='awaiting_ratification',updated_at=? "
            "WHERE round_id=?",
            (version_hash, _now(), round_id),
        )
        self._event(
            conn, round_id, {"display_name": "系统", "token_hash": "", "kind": "system"},
            "candidate_frozen", {"hash": version_hash, "metrics": report["metrics"]},
        )
        return {"candidate_hash": version_hash, "status": "awaiting_ratification",
                "linkage": report}

    def evaluate(self, token: str, round_id: str, version_hash: str | None = None) -> dict[str, Any]:
        """随时查看某版本（默认当前候选）在共同口径下的联动测算。"""

        self._authenticate(token)
        with self.store.tx() as conn:
            h = version_hash
            if h is None:
                r = conn.execute("SELECT candidate_hash FROM rounds WHERE round_id=?", (round_id,)).fetchone()
                if r is None:
                    raise NotFoundError("协商回合不存在")
                h = r["candidate_hash"]
            if not h:
                raise ConflictError("尚无候选版本，请指定 version_hash")
            doc = conn.execute(
                "SELECT * FROM documents WHERE round_id=? AND hash=?", (round_id, h)
            ).fetchone()
            if doc is None:
                raise NotFoundError("版本不存在")
            return self._evaluate_round_linkage(conn, round_id, loads(doc["payload"]))

    # 表决与签署 ----------------------------------------------------------

    def cast_vote(
        self, token: str, round_id: str, vote: str, request_id: str
    ) -> dict[str, Any]:
        """对冻结候选表决；只有现任代表的赞成票计入，联动校验先于此门禁。"""

        actor = self._authenticate(token)
        rep = self._require_representative(actor, round_id)
        if vote not in ("approve", "reject"):
            raise ValidationError("vote 只能是 approve 或 reject")

        def do(conn) -> dict[str, Any]:
            rnd = conn.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            candidate = rnd["candidate_hash"]
            if not candidate:
                raise ConflictError("尚无双方逐字确认且联动通过的候选版本，不能表决")
            conn.execute(
                "INSERT INTO votes(round_id,hash,side,generation,vote,identity_hash,voted_at) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(round_id,hash,side,generation) DO UPDATE SET vote=excluded.vote,"
                "identity_hash=excluded.identity_hash,voted_at=excluded.voted_at",
                (round_id, candidate, rep["side"], rep["generation"], vote,
                 rep["token_hash"], _now()),
            )
            self._event(conn, round_id, rep, "vote_cast", {"hash": candidate, "vote": vote})
            if vote == "reject":
                conn.execute(
                    "UPDATE rounds SET status='in_negotiation',candidate_hash=NULL,updated_at=? "
                    "WHERE round_id=?",
                    (_now(), round_id),
                )
                return {"round_id": round_id, "outcome": "rejected_back_to_negotiation"}
            approvals = self._current_approvals(conn, round_id, candidate)
            if set(approvals) == set(SIDES):
                conn.execute(
                    "UPDATE rounds SET status='ratified',updated_at=? WHERE round_id=?",
                    (_now(), round_id),
                )
                self._event(conn, round_id, rep, "ratified", {"hash": candidate})
                return {"round_id": round_id, "outcome": "ratified"}
            return {"round_id": round_id, "outcome": "awaiting_other_side",
                    "approved_by": approvals}

        payload = {"round_id": round_id, "vote": vote}
        return self._idempotent(request_id, "cast_vote", payload, do)

    def sign(self, token: str, round_id: str, request_id: str) -> dict[str, Any]:
        """现任代表会签。第二个签名在同一事务内创建协议，杜绝两份协议或半份事务。"""

        actor = self._authenticate(token)
        rep = self._require_representative(actor, round_id)

        def do(conn) -> dict[str, Any]:
            rnd = conn.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if rnd is None:
                raise NotFoundError("协商回合不存在")
            existing = conn.execute(
                "SELECT agreement_id FROM agreements WHERE round_id=?", (round_id,)
            ).fetchone()
            if existing:
                return {"agreement_id": existing["agreement_id"], "state": "already_signed"}
            candidate = rnd["candidate_hash"]
            if rnd["status"] != "ratified" or not candidate:
                raise ConflictError("协议尚未完成双方表决批准，不能签署")
            approvals = self._current_approvals(conn, round_id, candidate)
            if set(approvals) != set(SIDES):
                # 前任表决过、现任未承接：退回复议，而不是拿前任意见凑数。
                conn.execute(
                    "UPDATE rounds SET status='in_negotiation',candidate_hash=NULL,updated_at=? "
                    "WHERE round_id=?",
                    (_now(), round_id),
                )
                raise ConflictError("现任代表未完成表决，前任批准不自动继承")
            conn.execute(
                "INSERT OR IGNORE INTO signatures(round_id,side,generation,"
                "identity_hash,signed_at) VALUES(?,?,?,?,?)",
                (round_id, rep["side"], rep["generation"], rep["token_hash"], _now()),
            )
            sigs = conn.execute(
                "SELECT side FROM signatures WHERE round_id=?", (round_id,)
            ).fetchall()
            sig_sides = {s["side"] for s in sigs}
            self._event(conn, round_id, rep, "signed", {"side": rep["side"]})
            if sig_sides != set(SIDES):
                return {"agreement_id": round_id, "state": "pending_counterparty",
                        "signed_by": sorted(sig_sides)}
            # 同一事务内生成协议：唯一约束 round_id UNIQUE 兜底并发。
            doc = conn.execute(
                "SELECT * FROM documents WHERE round_id=? AND hash=?",
                (round_id, candidate),
            ).fetchone()
            version_payload = loads(doc["payload"])
            gens = {
                row["side"]: row["generation"]
                for row in conn.execute(
                    "SELECT side,generation FROM signatures WHERE round_id=?", (round_id,)
                )
            }
            clauses = derive_clauses(version_payload)
            agreement_id = "A-" + round_id
            text = render_agreement(
                round_id, candidate, version_payload, clauses,
                effective_from=_now()[:7], generations=gens,
            )
            conn.execute(
                "INSERT INTO agreements(agreement_id,round_id,version_hash,payload,clauses,"
                "text,effective_from,status,signed_at) VALUES(?,?,?,?,?,?,?, 'effective',?)",
                (agreement_id, round_id, candidate, canonical_json(version_payload),
                 canonical_json(clauses), text, _now()[:7], _now()),
            )
            conn.execute(
                "UPDATE rounds SET status='signed',agreement_id=?,updated_at=? WHERE round_id=?",
                (agreement_id, _now(), round_id),
            )
            self._event(conn, round_id, rep, "agreement_effective",
                        {"agreement_id": agreement_id, "hash": candidate})
            return {"agreement_id": agreement_id, "state": "effective",
                    "version_hash": candidate, "clauses": clauses}

        return self._idempotent(request_id, "sign", {"round_id": round_id}, do)

    # 履约账本 ------------------------------------------------------------

    def append_performance(
        self,
        token: str,
        agreement_id: str,
        period: str,
        status: str,
        facts: dict[str, Any],
        request_id: str,
        statement: str | None = None,
    ) -> dict[str, Any]:
        """按周期追加实际履行/部分履行/争议事实。只能追加，不能改写历史。"""

        actor = self._authenticate(token)
        rep = self._require_agreement_party(actor, agreement_id)
        if status not in PERF_STATUSES:
            raise ValidationError("履行状态只能是 full/partial/disputed")
        if not isinstance(period, str) or not period.strip():
            raise ValidationError("period 必填，如 2026-09")
        if not isinstance(facts, dict) or not facts:
            raise ValidationError("facts 必须是非空结构化事实")

        def do(conn) -> dict[str, Any]:
            cur = conn.execute(
                "SELECT COALESCE(MAX(id),0) AS m FROM performance_records WHERE agreement_id=?",
                (agreement_id,),
            ).fetchone()["m"]
            conn.execute(
                "INSERT INTO performance_records(agreement_id,period,status,facts,"
                "author_side,generation,statement,statement_side,statement_generation,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (agreement_id, period, status, canonical_json(facts),
                 rep["side"], rep["generation"], statement,
                 rep["side"] if statement else None,
                 rep["generation"] if statement else None, _now()),
            )
            self._event_by_agreement(conn, agreement_id, rep, "performance_appended",
                                     {"period": period, "status": status, "seq": cur + 1})
            return {"agreement_id": agreement_id, "period": period, "status": status,
                    "seq": cur + 1}

        payload = {"agreement_id": agreement_id, "period": period, "status": status,
                   "facts": facts, "statement": statement}
        return self._idempotent(request_id, "append_performance", payload, do)

    def raise_dispute(
        self, token: str, agreement_id: str, period: str, description: str,
        request_id: str,
    ) -> dict[str, Any]:
        """就某周期履约情况登记争议。"""

        actor = self._authenticate(token)
        rep = self._require_agreement_party(actor, agreement_id)
        if not description.strip():
            raise ValidationError("争议描述不能为空")

        def do(conn) -> dict[str, Any]:
            cur = conn.execute(
                "SELECT COALESCE(MAX(id),0) AS m FROM disputes WHERE agreement_id=?",
                (agreement_id,),
            ).fetchone()["m"]
            conn.execute(
                "INSERT INTO disputes(agreement_id,period,description,status,raised_by,"
                "created_at) VALUES(?,?,?, 'open', ?,?)",
                (agreement_id, period, description, rep["side"], _now()),
            )
            self._event_by_agreement(conn, agreement_id, rep, "dispute_raised",
                                     {"period": period, "seq": cur + 1})
            return {"agreement_id": agreement_id, "period": period, "status": "open",
                    "dispute_seq": cur + 1}

        payload = {"agreement_id": agreement_id, "period": period, "description": description}
        return self._idempotent(request_id, "raise_dispute", payload, do)

    def resolve_dispute(
        self, token: str, agreement_id: str, dispute_seq: int, resolution: str,
        request_id: str,
    ) -> dict[str, Any]:
        """争议经双方到场后由主持人记录结论并关闭。"""

        actor = self._authenticate(token)
        self._require_kind(actor, "facilitator")
        agreement = self._get_agreement(agreement_id)

        def do(conn) -> dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM disputes WHERE agreement_id=? ORDER BY id LIMIT 1 OFFSET ?",
                (agreement_id, dispute_seq - 1),
            ).fetchone()
            if row is None:
                raise NotFoundError("争议不存在")
            conn.execute(
                "UPDATE disputes SET status='resolved',resolution=?,resolved_at=? WHERE id=?",
                (resolution, _now(), row["id"]),
            )
            self._event_by_agreement(conn, agreement_id, actor, "dispute_resolved",
                                     {"seq": dispute_seq})
            return {"agreement_id": agreement_id, "dispute_seq": dispute_seq,
                    "status": "resolved"}

        payload = {"agreement_id": agreement_id, "dispute_seq": dispute_seq,
                   "resolution": resolution}
        return self._idempotent(request_id, "resolve_dispute", payload, do)

    def propose_supplement(
        self, token: str, agreement_id: str, text: str, request_id: str
    ) -> dict[str, Any]:
        """一方提出补充约定（追加，不改动原条款），对方接受后生效。"""

        actor = self._authenticate(token)
        rep = self._require_agreement_party(actor, agreement_id)
        if not text.strip():
            raise ValidationError("补充约定正文不能为空")

        def do(conn) -> dict[str, Any]:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS s FROM supplements WHERE agreement_id=?",
                (agreement_id,),
            ).fetchone()["s"]
            h = content_hash({"agreement_id": agreement_id, "seq": seq, "text": text})
            rendered = render_supplement(agreement_id, seq, text)
            conn.execute(
                "INSERT INTO supplements(agreement_id,seq,text,hash,status,proposed_by,"
                "generation,created_at) VALUES(?,?,?,?, 'proposed', ?,?,?)",
                (agreement_id, seq, rendered, h, rep["side"], rep["generation"], _now()),
            )
            self._event_by_agreement(conn, agreement_id, rep, "supplement_proposed",
                                     {"seq": seq})
            return {"agreement_id": agreement_id, "seq": seq, "status": "proposed"}

        payload = {"agreement_id": agreement_id, "text": text}
        return self._idempotent(request_id, "propose_supplement", payload, do)

    def accept_supplement(
        self, token: str, agreement_id: str, seq: int, request_id: str
    ) -> dict[str, Any]:
        """对方现任代表接受补充约定，自此作为共同有效承诺的一部分。"""

        actor = self._authenticate(token)
        rep = self._require_agreement_party(actor, agreement_id)

        def do(conn) -> dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM supplements WHERE agreement_id=? AND seq=?", (agreement_id, seq)
            ).fetchone()
            if row is None:
                raise NotFoundError("补充约定不存在")
            if row["proposed_by"] == rep["side"]:
                raise ConflictError("提出方不能自行接受，须由对方确认")
            if row["status"] == "effective":
                return {"agreement_id": agreement_id, "seq": seq, "status": "effective"}
            conn.execute(
                "UPDATE supplements SET status='effective',accepted_by=?,"
                "accepted_generation=?,accepted_at=? WHERE id=?",
                (rep["side"], rep["generation"], _now(), row["id"]),
            )
            self._event_by_agreement(conn, agreement_id, rep, "supplement_effective",
                                     {"seq": seq})
            return {"agreement_id": agreement_id, "seq": seq, "status": "effective"}

        payload = {"agreement_id": agreement_id, "seq": seq}
        return self._idempotent(request_id, "accept_supplement", payload, do)

    # 新证据与重新审议 ----------------------------------------------------

    def request_review(
        self,
        token: str,
        agreement_id: str,
        reason: str,
        evidence: str,
        request_id: str,
        proposed_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """新证据只能发起重新审议；原协议文本与条款保持原样，绝不就地修改。"""

        actor = self._authenticate(token)
        rep = self._require_agreement_party(actor, agreement_id)
        if not reason.strip() or not evidence.strip():
            raise ValidationError("重新审议需要说明理由并附证据")
        if proposed_payload is not None:
            issues = self._validate_proposal_payload(proposed_payload)
            if issues:
                raise ValidationError("随附的修改方案结构不合法", issues)

        def do(conn) -> dict[str, Any]:
            cur = conn.execute(
                "SELECT COALESCE(MAX(id),0) AS m FROM reviews WHERE agreement_id=?",
                (agreement_id,),
            ).fetchone()["m"]
            conn.execute(
                "INSERT INTO reviews(agreement_id,reason,evidence,proposed_payload,"
                "requested_by,status,created_at) VALUES(?,?,?,?,?, 'requested',?)",
                (agreement_id, reason, evidence,
                 canonical_json(proposed_payload) if proposed_payload else None,
                 rep["side"], _now()),
            )
            review_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            self._event_by_agreement(conn, agreement_id, rep, "review_requested",
                                     {"review_id": review_id, "seq": cur + 1})
            return {"review_id": review_id, "agreement_id": agreement_id,
                    "status": "requested"}

        payload = {"agreement_id": agreement_id, "reason": reason, "evidence": evidence,
                   "proposed_payload": proposed_payload}
        return self._idempotent(request_id, "request_review", payload, do)

    def decide_review(
        self,
        token: str,
        review_id: int,
        decision: str,
        note: str,
        request_id: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        """复核人员裁定：受理则开启新的协商回合，原协议条款原样保留。"""

        actor = self._authenticate(token)
        self._require_kind(actor, "reviewer")
        if decision not in ("accepted", "rejected"):
            raise ValidationError("decision 只能是 accepted 或 rejected")

        def do(conn) -> dict[str, Any]:
            review = conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            if review is None:
                raise NotFoundError("重新审议申请不存在")
            if review["status"] != "requested":
                raise ConflictError("该申请已裁定")
            conn.execute(
                "UPDATE reviews SET status=?,decided_at=?,decision_note=? WHERE id=?",
                (decision, _now(), note, review_id),
            )
            result = {"review_id": review_id, "decision": decision}
            if decision == "accepted":
                import secrets as _s
                new_round_id = "R-" + _s.token_hex(6)
                agreement = conn.execute(
                    "SELECT * FROM agreements WHERE agreement_id=?",
                    (review["agreement_id"],),
                ).fetchone()
                conn.execute(
                    "INSERT INTO rounds(round_id,title,status,created_at,updated_at,"
                    "predecessor_agreement_id) VALUES(?,?, 'in_negotiation',?,?,?)",
                    (new_round_id, title or f"重新审议（基于 {review['agreement_id']}）",
                     _now(), _now(), review["agreement_id"]),
                )
                conn.execute(
                    "UPDATE reviews SET successor_round_id=? WHERE id=?",
                    (new_round_id, review_id),
                )
                self._event(conn, new_round_id, actor, "review_round_opened",
                            {"predecessor_agreement_id": review["agreement_id"],
                             "review_id": review_id})
                result["successor_round_id"] = new_round_id
                result["predecessor_agreement_id"] = review["agreement_id"]
                result["note"] = (
                    "新回合须重新任命双方代表、重新逐字确认与表决；原协议条款不得改动"
                )
            return result

        payload = {"review_id": review_id, "decision": decision, "note": note}
        return self._idempotent(request_id, "decide_review", payload, do)

    # 角色视图 ------------------------------------------------------------

    def round_view(self, token: str, round_id: str) -> dict[str, Any]:
        """按调用方角色返回：待办、未决分歧、共同有效承诺（个人陈述按角色脱敏）。"""

        viewer = self._authenticate(token)
        with self.store.tx() as conn:
            rnd = conn.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
            if rnd is None:
                raise NotFoundError("协商回合不存在")
            view: dict[str, Any] = {
                "round_id": round_id,
                "title": rnd["title"],
                "status": rnd["status"],
                "viewer": {"kind": viewer["kind"], "side": viewer["side"],
                           "generation": viewer["generation"]},
                "todos": [],
                "documents": [],
                "open_disagreements": [],
                "effective_commitments": [],
            }
            current_reps = {
                row["side"]: row["generation"]
                for row in conn.execute(
                    "SELECT side,generation FROM identities WHERE round_id=? "
                    "AND kind='representative' AND status='active'",
                    (round_id,),
                )
            }
            # 材料与“是否现任任次所提/所确认”。
            confirmed = {
                (row["hash"], row["side"])
                for row in conn.execute(
                    "SELECT hash,side FROM confirmations WHERE round_id=?", (round_id,)
                )
            }
            # 只有现任任次代表的确认才算数，人员更换后前任确认不进入该集合。
            current_confirmed = {
                (row["hash"], row["side"])
                for row in conn.execute(
                    """
                    SELECT c.hash AS hash, c.side AS side FROM confirmations c
                    JOIN identities i
                      ON i.round_id=c.round_id AND i.side=c.side
                         AND i.generation=c.generation AND i.status='active'
                         AND i.kind='representative'
                    WHERE c.round_id=?
                    """,
                    (round_id,),
                )
            }
            for doc in conn.execute(
                "SELECT * FROM documents WHERE round_id=? ORDER BY id", (round_id,)
            ).fetchall():
                body = loads(doc["payload"])
                view["documents"].append({
                    "seq": doc["doc_seq"],
                    "type": doc["doc_type"],
                    "author_side": doc["author_side"],
                    "author_generation": doc["generation"],
                    "from_current_representative":
                        current_reps.get(doc["author_side"]) == doc["generation"],
                    "hash": doc["hash"],
                    "rendered_text": render_version(doc["doc_type"], body),
                    "confirmed_by": sorted(
                        side for (h, side) in confirmed if h == doc["hash"]
                    ),
                    "confirmed_by_current": sorted(
                        side for (h, side) in current_confirmed if h == doc["hash"]
                    ),
                })
            # 未决分歧。
            candidate = rnd["candidate_hash"]
            if rnd["status"] == "in_negotiation":
                latest_proposal = conn.execute(
                    "SELECT * FROM documents WHERE round_id=? AND doc_type='proposal' "
                    "ORDER BY doc_seq DESC LIMIT 1",
                    (round_id,),
                ).fetchone()
                if latest_proposal is not None:
                    missing = [
                        s for s in SIDES
                        if (latest_proposal["hash"], s) not in confirmed
                        or not self._current_generation_confirmed(
                            conn, round_id, latest_proposal["hash"], s, current_reps.get(s)
                        )
                    ]
                    if missing:
                        view["open_disagreements"].append({
                            "kind": "version_not_confirmed",
                            "hash": latest_proposal["hash"],
                            "awaiting_sides": missing,
                            "detail": "最新方案版本尚未经双方现任代表逐字确认",
                        })
            if rnd["status"] == "awaiting_ratification" and candidate:
                votes = {
                    row["side"]: row["vote"]
                    for row in conn.execute(
                        "SELECT side,vote FROM votes WHERE hash=? AND round_id=?",
                        (candidate, round_id),
                    )
                    if row["generation"] == current_reps.get(row["side"])
                }
                for side in SIDES:
                    if votes.get(side) != "approve":
                        view["open_disagreements"].append({
                            "kind": "awaiting_vote",
                            "hash": candidate,
                            "awaiting_side": side,
                        })
            # 待办。
            if viewer["kind"] == "representative" and viewer["round_id"] == round_id:
                side = viewer["side"]
                if not self._side_has_current_doc(conn, round_id, side, "demand" if side == "union" else "assumptions"):
                    view["todos"].append(
                        "提交诉求" if side == "union" else "提交经营假设与测算边界")
                if rnd["status"] == "in_negotiation":
                    latest = conn.execute(
                        "SELECT hash FROM documents WHERE round_id=? AND doc_type='proposal' "
                        "ORDER BY doc_seq DESC LIMIT 1",
                        (round_id,),
                    ).fetchone()
                    if latest is None:
                        view["todos"].append("提出整包方案（工资/工时/福利）或对对方版本提出反建议")
                    elif not self._current_generation_confirmed(
                        conn, round_id, latest["hash"], side, viewer["generation"]
                    ):
                        view["todos"].append("逐字核对并确认最新方案版本（或提出反建议）")
                if rnd["status"] == "awaiting_ratification" and candidate and not self._current_generation_voted(
                    conn, round_id, candidate, side, viewer["generation"], "approve"
                ):
                    view["todos"].append("对冻结候选版本投票表决")
                if rnd["status"] == "ratified" and not self._has_signed(conn, round_id, side):
                    view["todos"].append("完成签署")
            elif viewer["kind"] == "facilitator":
                if len(current_reps) < 2 and rnd["status"] != "signed":
                    view["todos"].append("任命双方授权代表")
            # 共同有效承诺。
            if rnd["agreement_id"]:
                view["effective_commitments"] = self._ledger_view(
                    conn, rnd["agreement_id"], viewer
                )
            return view

    def dashboard(self, token: str) -> dict[str, Any]:
        """跨回合汇总该调用方能看到的待办、分歧与共同有效承诺。"""

        viewer = self._authenticate(token)
        if viewer["kind"] == "representative":
            round_ids = [viewer["round_id"]]
        else:
            round_ids = [
                row["round_id"]
                for row in self.store.query_all(
                    "SELECT round_id FROM rounds ORDER BY created_at"
                )
            ]
        items = [self.round_view(token, rid) for rid in round_ids]
        todos = [
            {"round_id": item["round_id"], "todo": todo}
            for item in items for todo in item["todos"]
        ]
        disagreements = [
            {"round_id": item["round_id"], **d}
            for item in items for d in item["open_disagreements"]
        ]
        commitments = []
        for item in items:
            ledger = item["effective_commitments"]
            if ledger:
                commitments.append({"round_id": item["round_id"], "ledger": ledger})
        return {"identity": {"kind": viewer["kind"], "side": viewer["side"],
                             "generation": viewer["generation"]},
                "todos": todos,
                "open_disagreements": disagreements,
                "effective_commitments": commitments}

    def agreement_view(self, token: str, agreement_id: str) -> dict[str, Any]:
        viewer = self._authenticate(token)
        with self.store.tx() as conn:
            return {"agreement_id": agreement_id,
                    "ledger": self._ledger_view(conn, agreement_id, viewer)}

    # 内部辅助 ------------------------------------------------------------

    def _ledger_view(self, conn, agreement_id: str, viewer) -> dict[str, Any]:
        agreement = conn.execute(
            "SELECT * FROM agreements WHERE agreement_id=?", (agreement_id,)
        ).fetchone()
        if agreement is None:
            raise NotFoundError("协议不存在")
        clauses = loads(agreement["clauses"])
        performance = []
        for row in conn.execute(
            "SELECT * FROM performance_records WHERE agreement_id=? ORDER BY id",
            (agreement_id,),
        ):
            performance.append({
                "seq": row["id"],
                "period": row["period"],
                "status": row["status"],
                "facts": loads(row["facts"]),
                "reported_by": row["author_side"],
                "reporter_generation": row["generation"],
                "personal_statement": view_statement(
                    row["statement"], row["statement_side"],
                    row["statement_generation"] or 0, viewer["side"]
                    if viewer["kind"] == "representative" else viewer["kind"],
                ),
            })
        disputes = [
            {"seq": seq, "period": row["period"], "status": row["status"],
             "description": row["description"],
             "resolution": row["resolution"], "raised_by": row["raised_by"]}
            for seq, row in enumerate(conn.execute(
                "SELECT * FROM disputes WHERE agreement_id=? ORDER BY id", (agreement_id,)
            ), start=1)
        ]
        supplements = [
            {"seq": row["seq"], "text": row["text"], "status": row["status"],
             "proposed_by": row["proposed_by"], "accepted_by": row["accepted_by"]}
            for row in conn.execute(
                "SELECT * FROM supplements WHERE agreement_id=? ORDER BY seq",
                (agreement_id,),
            )
        ]
        reviews = [
            {"review_id": row["id"], "reason": row["reason"], "status": row["status"],
             "successor_round_id": row["successor_round_id"],
             "decision_note": row["decision_note"]}
            for row in conn.execute(
                "SELECT * FROM reviews WHERE agreement_id=? ORDER BY id", (agreement_id,)
            )
        ]
        return {
            "status": agreement["status"],
            "version_hash": agreement["version_hash"],
            "effective_from": agreement["effective_from"],
            "text": agreement["text"],
            "clauses": clauses,
            "performance": performance,
            "disputes": disputes,
            "supplements": supplements,
            "reviews": reviews,
        }

    def _submit_document(
        self, round_id: str, rep: dict[str, Any], doc_type: str,
        body: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        def do(conn) -> dict[str, Any]:
            seq = conn.execute(
                "SELECT COALESCE(MAX(doc_seq),0)+1 AS s FROM documents "
                "WHERE round_id=? AND doc_type=?",
                (round_id, doc_type),
            ).fetchone()["s"]
            h = content_hash(body)
            dup = conn.execute(
                "SELECT id,doc_seq FROM documents WHERE round_id=? AND hash=?",
                (round_id, h),
            ).fetchone()
            if dup is None:
                conn.execute(
                    "INSERT INTO documents(round_id,doc_type,doc_seq,author_side,"
                    "generation,identity_hash,payload,hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (round_id, doc_type, seq, rep["side"], rep["generation"],
                     rep["token_hash"], canonical_json(body), h, _now()),
                )
                self._event(conn, round_id, rep, f"{doc_type}_submitted",
                            {"seq": seq, "hash": h})
            else:
                seq = dup["doc_seq"]
            return {"round_id": round_id, "type": doc_type, "seq": seq, "hash": h,
                    "rendered_text": render_version(doc_type, body)}

        payload = {"round_id": round_id, "doc_type": doc_type, "body": body}
        return self._idempotent(request_id, f"submit_{doc_type}", payload, do)

    def _linkage_for(self, round_id: str, proposal_body: dict[str, Any]) -> dict[str, Any]:
        with self.store.tx() as conn:
            return self._evaluate_round_linkage(conn, round_id, proposal_body)

    def _evaluate_round_linkage(self, conn, round_id: str, proposal_body: dict[str, Any]):
        assumptions_row = conn.execute(
            "SELECT payload FROM documents WHERE round_id=? AND doc_type='assumptions' "
            "ORDER BY doc_seq DESC LIMIT 1",
            (round_id,),
        ).fetchone()
        if assumptions_row is None:
            return {"ok": False,
                    "issues": ["企业方尚未提交经营假设，无法在共同口径下整体测算"],
                    "metrics": None}
        assumptions = loads(assumptions_row["payload"])
        assumptions.pop("kind", None)
        return evaluate_linkage(
            proposal_body["package"], assumptions, proposal_body["caliber"]
        )

    def _current_approvals(self, conn, round_id: str, candidate: str) -> list[str]:
        return [
            row["side"] for row in conn.execute(
                """
                SELECT v.side FROM votes v
                JOIN identities i
                  ON i.round_id=? AND i.side=v.side AND i.generation=v.generation
                     AND i.status='active' AND i.kind='representative'
                WHERE v.round_id=? AND v.hash=? AND v.vote='approve'
                """,
                (round_id, round_id, candidate),
            )
        ]

    def _current_generation_confirmed(self, conn, round_id, h, side, generation) -> bool:
        if generation is None:
            return False
        return conn.execute(
            "SELECT 1 FROM confirmations WHERE round_id=? AND hash=? AND side=? "
            "AND generation=?",
            (round_id, h, side, generation),
        ).fetchone() is not None

    def _current_generation_voted(self, conn, round_id, h, side, generation, vote) -> bool:
        return conn.execute(
            "SELECT 1 FROM votes WHERE round_id=? AND hash=? AND side=? AND generation=? "
            "AND vote=?",
            (round_id, h, side, generation, vote),
        ).fetchone() is not None

    def _has_signed(self, conn, round_id: str, side: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM signatures s JOIN identities i ON i.token_hash=s.identity_hash "
            "WHERE s.round_id=? AND i.side=? AND i.status='active'",
            (round_id, side),
        ).fetchone() is not None

    def _side_has_current_doc(self, conn, round_id, side, doc_type) -> bool:
        row = conn.execute(
            "SELECT i.generation FROM identities i WHERE i.round_id=? AND i.side=? "
            "AND i.status='active'",
            (round_id, side),
        ).fetchone()
        if row is None:
            return False
        return conn.execute(
            "SELECT 1 FROM documents WHERE round_id=? AND doc_type=? AND author_side=? "
            "AND generation=?",
            (round_id, doc_type, side, row["generation"]),
        ).fetchone() is not None

    def _event(self, conn, round_id, actor, kind, detail) -> None:
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS s FROM events WHERE round_id=?", (round_id,)
        ).fetchone()["s"]
        conn.execute(
            "INSERT INTO events(round_id,seq,at,actor,kind,detail) VALUES(?,?,?,?,?,?)",
            (round_id, seq, _now(), actor.get("display_name", "系统"), kind,
             canonical_json(detail)),
        )

    def _event_by_agreement(self, conn, agreement_id, actor, kind, detail) -> None:
        row = conn.execute("SELECT round_id FROM agreements WHERE agreement_id=?",
                           (agreement_id,)).fetchone()
        if row:
            self._event(conn, row["round_id"], actor, kind, detail)

    def _idempotent(
        self, request_id: str, operation: str, payload: Any,
        callback: Callable[[Any], dict[str, Any]],
    ) -> dict[str, Any]:
        if not request_id or not isinstance(request_id, str):
            raise ValidationError("业务号 request_id 必填")
        payload_hash = content_hash({"operation": operation, "payload": payload})
        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT operation,payload_hash,result FROM idempotency WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is not None:
                if row["payload_hash"] != payload_hash or row["operation"] != operation:
                    raise IdempotencyConflict(
                        request_id, row["operation"], row["payload_hash"]
                    )
                return loads(row["result"])
            result = callback(conn)
            conn.execute(
                "INSERT INTO idempotency(request_id,operation,payload_hash,result,created_at) "
                "VALUES(?,?,?,?,?)",
                (request_id, operation, payload_hash, canonical_json(result), _now()),
            )
            return result

    # 鉴权辅助 ------------------------------------------------------------

    def _authenticate(self, token: str) -> dict[str, Any]:
        if not token:
            raise AuthenticationError("缺少令牌")
        row = self.store.query_one(
            "SELECT * FROM identities WHERE token_hash=? AND status='active'",
            (self._hash_token(token),),
        )
        if row is None:
            raise AuthenticationError("令牌无效或已随卸任失效")
        return _rowdict(row)

    def _require_kind(self, actor: dict[str, Any], kind: str) -> None:
        if actor["kind"] != kind:
            raise AuthorizationError(f"该操作仅允许 {kind} 执行")

    def _require_representative(
        self, actor: dict[str, Any], round_id: str, side: str | None = None
    ) -> dict[str, Any]:
        if actor["kind"] != "representative" or actor["round_id"] != round_id:
            raise AuthorizationError("该操作仅允许本回合授权代表执行")
        if side is not None and actor["side"] != side:
            raise AuthorizationError(f"该操作仅允许 {SIDE_LABEL[side]}代表执行")
        return actor

    def _require_agreement_party(self, actor: dict[str, Any], agreement_id: str) -> dict[str, Any]:
        agreement = self._get_agreement(agreement_id)
        return self._require_representative(actor, agreement["round_id"])

    def _get_agreement(self, agreement_id: str):
        agreement = self.store.query_one(
            "SELECT * FROM agreements WHERE agreement_id=?", (agreement_id,)
        )
        if agreement is None:
            raise NotFoundError("协议不存在")
        return _rowdict(agreement)

    def _require_round(self, round_id: str) -> None:
        row = self.store.query_one("SELECT 1 FROM rounds WHERE round_id=?", (round_id,))
        if row is None:
            raise NotFoundError("协商回合不存在")

    def _require_round_editable(self, round_id: str) -> None:
        row = self.store.query_one("SELECT status FROM rounds WHERE round_id=?", (round_id,))
        if row is None:
            raise NotFoundError("协商回合不存在")
        if row["status"] == "signed":
            raise ConflictError("协议已签署，不得在原回合内修改；新证据请发起重新审议")

    # 结构校验 ------------------------------------------------------------

    def _validate_demand(self, payload: Any) -> list[str]:
        issues: list[str] = []
        if not isinstance(payload, dict):
            return ["诉求必须是结构化对象"]
        items = payload.get("items")
        if not isinstance(items, list) or not items or not all(
            isinstance(i, str) and i.strip() for i in items
        ):
            issues.append("items 必须是非空字符串列表")
        return issues

    def _validate_assumptions(self, payload: Any) -> list[str]:
        issues: list[str] = []
        if not isinstance(payload, dict):
            return ["经营假设必须是结构化对象"]
        for key in ("headcount", "current_monthly_wage_per_head",
                    "current_standard_hours_month", "local_minimum_wage_monthly",
                    "monthly_affordability_budget"):
            if key not in payload:
                issues.append(f"缺少必要经营假设：{key}")
        return issues

    def _validate_proposal_payload(self, payload: Any) -> list[str]:
        issues: list[str] = []
        if not isinstance(payload, dict):
            return ["方案必须是结构化对象"]
        package = payload.get("package")
        caliber = payload.get("caliber")
        if not isinstance(package, dict):
            issues.append("缺少 package（工资/工时/福利整包）")
            return issues
        for side in ("wages", "hours", "benefits"):
            if not isinstance(package.get(side), dict):
                issues.append(f"package 必须整体包含 {side} 部分")
        if not isinstance(caliber, dict):
            issues.append("缺少 caliber（测算口径）")
        else:
            from .linkage import validate_caliber
            issues.extend(validate_caliber(caliber))
        return issues
