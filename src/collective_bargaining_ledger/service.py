"""集体协商与履约应用服务。

调用约定
--------
1. 职工方/企业方的每个命令都携带 ``mandate_id``（授权凭据）。服务端实时校验
   授权仍有效；代表被更换后旧凭据立刻失效。
2. 每个命令可携带 ``business_no`` 业务号：同号同文重试直接返回首次结果，
   同号异文抛出 :class:`TextConflictError`，绝不静默重放或覆盖。
3. 签署、补充约定同意等关键动作以 ``BEGIN IMMEDIATE`` 短事务串行提交；
   只有双方签署齐备的同一事务内才会创建生效协议，外界永远看不到“半份协议”。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import (
    AuthorizationError,
    ConflictError,
    MandateEndedError,
    NotFoundError,
    StateError,
    TextConflictError,
    ValidationError,
)
from .model import (
    ALL_STATEMENTS,
    COMPANY,
    PERFORM_DISPUTE,
    PERFORM_FULL,
    PERFORM_KINDS,
    PERFORM_PARTIAL,
    SIDES,
    WORKER,
    Package,
    canonical_dumps,
    text_hash,
)
from .storage import connect, dumps, initialize, loads, transaction
from .validation import redact_all, require_valid_package


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class BargainingService:
    """对工会端与企业端提供同一套接口，数据按调用方角色过滤与脱敏。"""

    def __init__(self, db_path: str | Path, *, staff_token: str,
                 clock: Callable[[], str] = _now) -> None:
        self.db_path = str(db_path)
        self._clock = clock
        conn = connect(self.db_path)
        try:
            initialize(conn)
            token_hash = hashlib.sha256(
                (staff_token or "").encode("utf-8")).hexdigest()
            with transaction(conn):
                stored = conn.execute(
                    "SELECT value FROM meta WHERE key = 'staff_token_hash'"
                ).fetchone()
                if stored is None:
                    if not staff_token:
                        raise AuthorizationError(
                            "首次初始化必须提供园区工会人员凭据")
                    conn.execute(
                        "INSERT INTO meta(key, value) "
                        "VALUES ('staff_token_hash', ?)", (token_hash,))
                elif not hmac.compare_digest(stored["value"], token_hash):
                    raise AuthorizationError("园区工会人员凭据无效")
        finally:
            conn.close()

    # ---- 连接与鉴权 ----------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def _staff(self, token: str) -> None:
        conn = self._conn()
        try:
            stored = conn.execute(
                "SELECT value FROM meta WHERE key = 'staff_token_hash'"
            ).fetchone()
        finally:
            conn.close()
        token_hash = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
        if stored is None or not hmac.compare_digest(stored["value"], token_hash):
            raise AuthorizationError("园区工会人员凭据无效")

    def _auth(self, conn: sqlite3.Connection, mandate_id: str) -> dict[str, Any]:
        if not mandate_id:
            raise AuthorizationError("缺少授权凭据 mandate_id")
        row = conn.execute(
            "SELECT * FROM representatives WHERE mandate_id = ?",
            (mandate_id,),
        ).fetchone()
        if row is None:
            raise AuthorizationError("授权不存在")
        rep = dict(row)
        if rep["valid_to"] is not None:
            raise MandateEndedError(
                f"{rep['side']} 方代表授权已于 {rep['valid_to']} 终止，"
                "前任任内未被接受的意见不自动继承，请使用新授权重新提交")
        return rep

    # ---- 幂等包装 ------------------------------------------------------

    def _idempotent(
        self,
        business_no: str | None,
        request: dict[str, Any],
        actor_mandate: str,
        action: Callable[[sqlite3.Connection], dict[str, Any]],
    ) -> dict[str, Any]:
        """执行业务动作并按业务号记录结果；同号同文重放，异文/异人冲突。"""
        if not business_no:
            conn = self._conn()
            try:
                with transaction(conn):
                    return action(conn)
            finally:
                conn.close()

        request_hash = text_hash(request)
        conn = self._conn()
        try:
            with transaction(conn):
                existing = conn.execute(
                    "SELECT request_hash, actor_mandate, result_json "
                    "FROM idempotency WHERE business_no = ?",
                    (business_no,),
                ).fetchone()
                if existing is not None:
                    if existing["actor_mandate"] != actor_mandate:
                        raise TextConflictError(
                            f"业务号 {business_no} 已被其他代表使用，"
                            "请求被拒绝以暴露冲突")
                    if existing["request_hash"] != request_hash:
                        raise TextConflictError(
                            f"业务号 {business_no} 重试请求与首次请求异文，"
                            "已明确暴露冲突；相同业务号必须逐字一致",
                            loads(existing["result_json"]))
                    result = loads(existing["result_json"])
                    result["replayed"] = True
                    return result
                result = action(conn)
                result["replayed"] = False
                conn.execute(
                    "INSERT INTO idempotency(business_no, request_hash, "
                    "result_json, created_at, actor_mandate) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (business_no, request_hash, dumps(result),
                     self._clock(), actor_mandate),
                )
                return result
        finally:
            conn.close()

    # ---- 授权与代表更换 ------------------------------------------------

    def register_representative(
        self, staff_token: str, side: str, representative_id: str, name: str,
        *, mandate_id: str | None = None,
    ) -> dict[str, Any]:
        """园区工会登记一方代表；同方再次登记即更换，旧授权即刻终止。"""
        self._staff(staff_token)
        if side not in SIDES:
            raise ValidationError(f"未知协商方：{side}")
        if not representative_id or not name:
            raise ValidationError("代表编号与姓名不能为空")
        mandate_id = mandate_id or f"mandate-{uuid.uuid4().hex}"
        now = self._clock()
        conn = self._conn()
        try:
            with transaction(conn):
                if conn.execute(
                        "SELECT 1 FROM representatives WHERE mandate_id = ?",
                        (mandate_id,)).fetchone():
                    raise ConflictError(f"授权凭据已存在：{mandate_id}")
                prev = conn.execute(
                    "SELECT mandate_id FROM representatives "
                    "WHERE side = ? AND valid_to IS NULL ORDER BY valid_from",
                    (side,)).fetchall()
                for old in prev:
                    # 前任尚未被对方接受的意见随更换失效，记录保留为 lapsed
                    conn.execute(
                        "UPDATE statements SET status = 'lapsed' "
                        "WHERE mandate_id = ? AND status = 'open'",
                        (old["mandate_id"],))
                    conn.execute(
                        "UPDATE representatives SET valid_to = ? "
                        "WHERE mandate_id = ? AND valid_to IS NULL",
                        (now, old["mandate_id"]))
                conn.execute(
                    "INSERT INTO representatives(mandate_id, representative_id, "
                    "name, side, valid_from, valid_to) VALUES (?, ?, ?, ?, ?, NULL)",
                    (mandate_id, representative_id, name, side, now))
                return {
                    "mandate_id": mandate_id,
                    "representative_id": representative_id,
                    "name": name,
                    "side": side,
                    "valid_from": now,
                    "replaced_mandates": [r["mandate_id"] for r in prev],
                }
        finally:
            conn.close()

    def list_mandates(self, staff_token: str, side: str | None = None) -> list[dict]:
        self._staff(staff_token)
        conn = self._conn()
        try:
            if side:
                rows = conn.execute(
                    "SELECT * FROM representatives WHERE side = ? "
                    "ORDER BY valid_from", (side,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM representatives ORDER BY side, valid_from"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---- 协商开启与重新审议 --------------------------------------------

    def open_negotiation(self, mandate_id: str, title: str, *,
                         business_no: str | None = None) -> dict[str, Any]:
        request = {"op": "open_negotiation", "mandate_id": mandate_id,
                   "title": title}

        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            rep = self._auth(conn, mandate_id)
            if not title:
                raise ValidationError("协商标题不能为空")
            neg_id = f"neg-{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO negotiations(id, title, status, created_by_mandate, "
                "created_at) VALUES (?, ?, 'open', ?, ?)",
                (neg_id, title, mandate_id, self._clock()))
            return {"negotiation_id": neg_id, "title": title,
                    "opened_by": rep["side"], "status": "open"}

        return self._idempotent(business_no, request, mandate_id, action)

    def request_reconsideration(
        self, mandate_id: str, agreement_id: str, evidence: dict[str, Any], *,
        title: str | None = None, business_no: str | None = None,
    ) -> dict[str, Any]:
        """新证据只能发起重新审议：冻结旧协议，另开一条完整协商流程。"""
        request = {"op": "request_reconsideration", "mandate_id": mandate_id,
                   "agreement_id": agreement_id, "evidence": evidence,
                   "title": title}

        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            rep = self._auth(conn, mandate_id)
            agreement = conn.execute(
                "SELECT * FROM agreements WHERE id = ? AND status = 'active'",
                (agreement_id,)).fetchone()
            if agreement is None:
                raise NotFoundError("没有找到可供重新审议的生效协议")
            self._ensure_jsonable("evidence", evidence)
            recon_id = f"recon-{uuid.uuid4().hex[:12]}"
            neg_id = f"neg-{uuid.uuid4().hex[:12]}"
            now = self._clock()
            conn.execute(
                "INSERT INTO negotiations(id, title, status, source_agreement_id, "
                "created_by_mandate, created_at) VALUES (?, ?, 'open', ?, ?, ?)",
                (neg_id, title or f"对协议 {agreement_id} 的重新审议",
                 agreement_id, mandate_id, now))
            conn.execute(
                "INSERT INTO reconsiderations(id, agreement_id, negotiation_id, "
                "evidence_json, requested_by, mandate_id, created_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
                (recon_id, agreement_id, neg_id, dumps(evidence),
                 rep["side"], mandate_id, now))
            return {"reconsideration_id": recon_id,
                    "negotiation_id": neg_id,
                    "agreement_id": agreement_id,
                    "status": "open",
                    "notice": "原协议条款保持有效且不得修改；新协商完成签署后才会替换"}

        return self._idempotent(business_no, request, mandate_id, action)

    def abandon_negotiation(self, staff_token: str, negotiation_id: str,
                            reason: str = "") -> dict[str, Any]:
        """园区工会可终止无法继续的协商（如重新审议未达成一致，旧协议继续有效）。"""
        self._staff(staff_token)
        conn = self._conn()
        try:
            with transaction(conn):
                neg = self._get_open_negotiation(conn, negotiation_id)
                conn.execute(
                    "UPDATE negotiations SET status = 'abandoned' WHERE id = ?",
                    (negotiation_id,))
                return {"negotiation_id": negotiation_id,
                        "status": "abandoned", "reason": reason}
        finally:
            conn.close()

    # ---- 陈述：诉求 / 经营假设 / 测算口径 / 反建议 / 个人陈述 -----------

    def submit_statement(
        self, mandate_id: str, negotiation_id: str, kind: str,
        content: Any, *, sensitive: bool = False,
        business_no: str | None = None,
    ) -> dict[str, Any]:
        request = {"op": "submit_statement", "mandate_id": mandate_id,
                   "negotiation_id": negotiation_id, "kind": kind,
                   "content": content, "sensitive": sensitive}

        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            rep = self._auth(conn, mandate_id)
            self._get_open_negotiation(conn, negotiation_id)
            if kind not in ALL_STATEMENTS:
                raise ValidationError(f"未知陈述类型：{kind}")
            self._ensure_jsonable("content", content)
            cur = conn.execute(
                "INSERT INTO statements(negotiation_id, mandate_id, side, kind, "
                "content_hash, content_json, status, sensitive, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                (negotiation_id, mandate_id, rep["side"], kind,
                 text_hash(content), canonical_dumps(content),
                 1 if (sensitive or kind == "personal_statement") else 0,
                 self._clock()))
            return {"statement_id": cur.lastrowid,
                    "negotiation_id": negotiation_id,
                    "side": rep["side"], "kind": kind,
                    "content_hash": text_hash(content),
                    "status": "open"}

        return self._idempotent(business_no, request, mandate_id, action)

    def accept_statement(self, mandate_id: str, statement_id: int, *,
                         business_no: str | None = None) -> dict[str, Any]:
        """对方代表接受一条陈述；接受后成为双方共同事实，口径进入联动校验。"""
        request = {"op": "accept_statement", "mandate_id": mandate_id,
                   "statement_id": statement_id}

        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            rep = self._auth(conn, mandate_id)
            row = conn.execute(
                "SELECT * FROM statements WHERE id = ?", (statement_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"陈述不存在：{statement_id}")
            stmt = dict(row)
            if stmt["side"] == rep["side"]:
                raise ValidationError("只能接受对方提交的意见")
            if stmt["status"] == "lapsed":
                raise StateError("该意见因提交方代表更换而失效，不能被继承接受")
            if stmt["status"] == "accepted":
                return {"statement_id": statement_id, "status": "accepted",
                        "already": True}
            conn.execute(
                "UPDATE statements SET status = 'accepted', accepted_at = ?, "
                "accepted_by_mandate = ? WHERE id = ? AND status = 'open'",
                (self._clock(), mandate_id, statement_id))
            return {"statement_id": statement_id, "status": "accepted"}

        return self._idempotent(business_no, request, mandate_id, action)

    def list_statements(self, mandate_id: str, negotiation_id: str) -> list[dict]:
        """按查看者角色脱敏列出协商全部陈述。"""
        conn = self._conn()
        try:
            rep = self._auth(conn, mandate_id)
            self._get_negotiation(conn, negotiation_id)
            rows = conn.execute(
                "SELECT id, negotiation_id, mandate_id, side, kind, content_json, "
                "status, sensitive, created_at, accepted_at "
                "FROM statements WHERE negotiation_id = ? ORDER BY id",
                (negotiation_id,)).fetchall()
            statements = [{
                "statement_id": r["id"],
                "negotiation_id": r["negotiation_id"],
                "mandate_id": r["mandate_id"],
                "side": r["side"],
                "kind": r["kind"],
                "content": loads(r["content_json"]),
                "status": r["status"],
                "sensitive": bool(r["sensitive"]),
                "created_at": r["created_at"],
                "accepted_at": r["accepted_at"],
            } for r in rows]
            return redact_all(statements, rep["side"])
        finally:
            conn.close()

    # ---- 条款版本：逐字一致 → 各自确认 → 表决 --------------------------

    def propose_package(
        self, mandate_id: str, negotiation_id: str, clauses: dict[str, dict],
        *, assumptions: dict[str, Any] | None = None,
        bases: dict[str, Any] | None = None, note: str = "",
        business_no: str | None = None,
    ) -> dict[str, Any]:
        document = self._build_document(clauses, assumptions or {}, bases or {}, note)
        request = {"op": "propose_package", "mandate_id": mandate_id,
                   "negotiation_id": negotiation_id, "document": document}

        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            rep = self._auth(conn, mandate_id)
            self._get_open_negotiation(conn, negotiation_id)
            version_hash = text_hash(document)
            existing = conn.execute(
                "SELECT 1 FROM package_versions WHERE negotiation_id = ? "
                "AND version_hash = ?", (negotiation_id, version_hash)).fetchone()
            violations = self._linkage(conn, negotiation_id, document)
            linkage = {"violations": violations}
            if existing is None:
                conn.execute(
                    "INSERT INTO package_versions(negotiation_id, version_hash, "
                    "document_json, proposed_by, proposer_mandate, linkage_json, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (negotiation_id, version_hash, dumps(document), rep["side"],
                     mandate_id, dumps(linkage), self._clock()))
            return {"negotiation_id": negotiation_id,
                    "version_hash": version_hash,
                    "proposed_by": rep["side"],
                    "already_existed": bool(existing),
                    "linkage": linkage}

        return self._idempotent(business_no, request, mandate_id, action)

    def confirm_version(self, mandate_id: str, negotiation_id: str,
                        version_hash: str, *,
                        business_no: str | None = None) -> dict[str, Any]:
        request = {"op": "confirm_version", "mandate_id": mandate_id,
                   "negotiation_id": negotiation_id, "version_hash": version_hash}

        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            rep = self._auth(conn, mandate_id)
            self._get_open_negotiation(conn, negotiation_id)
            document = self._get_version_document(conn, negotiation_id, version_hash)
            violations = self._linkage(conn, negotiation_id, document)
            if violations:
                from .errors import LinkageError
                raise LinkageError(
                    "工资、工时、福利整体联动校验未通过，条款包不能进入确认/表决",
                    violations)
            already = conn.execute(
                "SELECT 1 FROM package_confirmations "
                "WHERE negotiation_id = ? AND version_hash = ? AND side = ? "
                "AND mandate_id IN (SELECT mandate_id FROM representatives "
                "WHERE side = ? AND valid_to IS NULL)",
                (negotiation_id, version_hash, rep["side"], rep["side"]),
            ).fetchone()
            if not already:
                conn.execute(
                    "INSERT INTO package_confirmations(negotiation_id, version_hash, "
                    "side, mandate_id, confirmed_at) VALUES (?, ?, ?, ?, ?)",
                    (negotiation_id, version_hash, rep["side"], mandate_id,
                     self._clock()))
            confirmed = self._active_confirmations(conn, negotiation_id, version_hash)
            return {"negotiation_id": negotiation_id,
                    "version_hash": version_hash,
                    "side": rep["side"],
                    "confirmed_by": sorted(confirmed),
                    "confirmed_by_both": set(confirmed) == set(SIDES)}

        return self._idempotent(business_no, request, mandate_id, action)

    def cast_vote(self, mandate_id: str, negotiation_id: str, version_hash: str,
                  yes: bool, *, business_no: str | None = None) -> dict[str, Any]:
        request = {"op": "cast_vote", "mandate_id": mandate_id,
                   "negotiation_id": negotiation_id, "version_hash": version_hash,
                   "yes": yes}

        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            rep = self._auth(conn, mandate_id)
            self._get_open_negotiation(conn, negotiation_id)
            document = self._get_version_document(conn, negotiation_id, version_hash)
            require_valid_package(document,
                                  self._accepted_bases(conn, negotiation_id))
            confirmed = self._active_confirmations(conn, negotiation_id, version_hash)
            if set(confirmed) != set(SIDES):
                raise StateError(
                    "只有逐字一致的版本经双方各自确认后才可进入表决")
            existing = conn.execute(
                "SELECT v.vote FROM votes v "
                "JOIN representatives r ON r.mandate_id = v.mandate_id "
                "WHERE v.negotiation_id = ? AND v.version_hash = ? "
                "AND v.side = ? AND r.valid_to IS NULL",
                (negotiation_id, version_hash, rep["side"])).fetchone()
            if existing is not None:
                if (existing["vote"] == "yes") != yes:
                    raise TextConflictError(
                        f"{rep['side']} 方对该版本已有相反表决，意向冲突必须明示，"
                        "不得悄悄改票")
                return {"negotiation_id": negotiation_id,
                        "version_hash": version_hash, "side": rep["side"],
                        "vote": existing["vote"], "already": True}
            vote = "yes" if yes else "no"
            try:
                conn.execute(
                    "INSERT INTO votes(negotiation_id, version_hash, side, "
                    "mandate_id, vote, voted_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (negotiation_id, version_hash, rep["side"], mandate_id,
                     vote, self._clock()))
            except sqlite3.IntegrityError as exc:
                raise TextConflictError(
                    f"{rep['side']} 方对该版本已有表决，意向冲突必须明示，"
                    "不得悄悄改票") from exc
            tally = self._vote_tally(conn, negotiation_id, version_hash)
            return {"negotiation_id": negotiation_id,
                    "version_hash": version_hash, "side": rep["side"],
                    "vote": vote, "tally": tally,
                    "passed": tally.get("yes", 0) == 2}

        return self._idempotent(business_no, request, mandate_id, action)

    def sign_agreement(self, mandate_id: str, negotiation_id: str,
                       version_hash: str, *, effective_from: str | None = None,
                       business_no: str | None = None) -> dict[str, Any]:
        """原子签署：仅在双方签署齐备的同一事务内创建生效协议。"""
        request = {"op": "sign_agreement", "mandate_id": mandate_id,
                   "negotiation_id": negotiation_id, "version_hash": version_hash,
                   "effective_from": effective_from}

        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            return self._sign(conn, mandate_id, negotiation_id, version_hash,
                              effective_from)

        try:
            return self._idempotent(business_no, request, mandate_id, action)
        except sqlite3.IntegrityError as exc:
            # 并发下被唯一索引拦截：不产生第二份协议，回读既有结果暴露现状
            conn = self._conn()
            try:
                agreement = conn.execute(
                    "SELECT * FROM agreements WHERE negotiation_id = ?",
                    (negotiation_id,)).fetchone()
                if agreement is not None:
                    result = self._agreement_view(conn, dict(agreement))
                    result["replayed"] = False
                    result["concurrent"] = True
                    return result
            finally:
                conn.close()
            raise ConflictError(f"签署并发冲突，事务已回滚：{exc}") from exc

    def _sign(self, conn: sqlite3.Connection, mandate_id: str, negotiation_id: str,
              version_hash: str, effective_from: str | None) -> dict[str, Any]:
        rep = self._auth(conn, mandate_id)
        neg = self._get_negotiation(conn, negotiation_id)
        # 协商已完成签署（常见于并发/重试）：直接回读同一份协议，不再报错
        if neg["status"] == "sealed":
            agreement = conn.execute(
                "SELECT * FROM agreements WHERE negotiation_id = ?",
                (negotiation_id,)).fetchone()
            if agreement is not None and agreement["version_hash"] == version_hash:
                return {"sealed": True, "already": True,
                        "agreement": self._agreement_view(conn, dict(agreement))}
            raise StateError("协商已 sealed，不能再签署其他版本")
        document = self._get_version_document(conn, negotiation_id, version_hash)
        require_valid_package(document,
                              self._accepted_bases(conn, negotiation_id))
        confirmed = self._active_confirmations(conn, negotiation_id, version_hash)
        if set(confirmed) != set(SIDES):
            raise StateError("版本未经双方逐字确认，不能签署")
        tally = self._vote_tally(conn, negotiation_id, version_hash)
        if tally.get("yes", 0) != 2:
            raise StateError("表决未获双方赞成，不能签署")
        now = self._clock()

        existing_sig = conn.execute(
            "SELECT 1 FROM signatures WHERE negotiation_id = ? "
            "AND version_hash = ? AND mandate_id = ?",
            (negotiation_id, version_hash, mandate_id)).fetchone()
        if not existing_sig:
            # 同授权并发重试时唯一索引可能抢先落库：忽略即可，签署事实仍只有一条
            conn.execute(
                "INSERT OR IGNORE INTO signatures(negotiation_id, version_hash, "
                "side, mandate_id, representative_id, name, signed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (negotiation_id, version_hash, rep["side"], mandate_id,
                 rep["representative_id"], rep["name"], now))

        # 只有当前在任代表的签署才能凑齐生效；前任签署保留为审计痕迹
        signed = {r["side"] for r in conn.execute(
            "SELECT s.side FROM signatures s "
            "JOIN representatives r ON r.mandate_id = s.mandate_id "
            "WHERE s.negotiation_id = ? AND s.version_hash = ? "
            "AND r.valid_to IS NULL",
            (negotiation_id, version_hash)).fetchall()}
        if set(signed) != set(SIDES):
            return {"negotiation_id": negotiation_id,
                    "version_hash": version_hash, "sealed": False,
                    "signed_by": sorted(signed),
                    "waiting_for": sorted(set(SIDES) - set(signed)),
                    "notice": "仅一方签署，尚不存在任何协议（无半份事务）"}

        agreement = conn.execute(
            "SELECT * FROM agreements WHERE negotiation_id = ?",
            (negotiation_id,)).fetchone()
        if agreement is not None:
            return {"sealed": True, "agreement": self._agreement_view(
                conn, dict(agreement)), "already": True}

        source_id = neg["source_agreement_id"]
        if source_id is not None:
            source = conn.execute(
                "SELECT id, status FROM agreements WHERE id = ?",
                (source_id,)).fetchone()
            if source is None or source["status"] != "active":
                raise StateError("重新审议所依据的原协议已不是生效协议")
        else:
            other_active = conn.execute(
                "SELECT id FROM agreements WHERE status = 'active'").fetchone()
            if other_active is not None:
                raise ConflictError(
                    f"已存在生效协议 {other_active['id']}，"
                    "变更条款必须通过重新审议而非新签")

        agreement_id = f"agr-{uuid.uuid4().hex[:12]}"
        if source_id is not None:
            conn.execute(
                "UPDATE agreements SET status = 'superseded' WHERE id = ? "
                "AND status = 'active'", (source_id,))
        conn.execute(
            "INSERT INTO agreements(id, negotiation_id, version_hash, "
            "document_json, signed_at, effective_from, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active')",
            (agreement_id, negotiation_id, version_hash, dumps(document),
             now, effective_from or now[:10]))
        conn.execute(
            "UPDATE negotiations SET status = 'sealed', sealed_at = ? WHERE id = ?",
            (now, negotiation_id))
        if source_id is not None:
            conn.execute(
                "UPDATE reconsiderations SET status = 'sealed' "
                "WHERE agreement_id = ? AND status = 'open'", (source_id,))
        view = self._agreement_view(conn, {
            "id": agreement_id, "negotiation_id": negotiation_id,
            "version_hash": version_hash, "document_json": dumps(document),
            "signed_at": now, "effective_from": effective_from or now[:10],
            "status": "active"})
        return {"sealed": True, "agreement": view,
                "superseded_agreement_id": source_id}

    # ---- 履约账本：按周期追加实际/部分/争议/补充约定 -------------------

    def append_performance(self, mandate_id: str, agreement_id: str, period: str,
                           kind: str, content: dict[str, Any], *,
                           business_no: str | None = None) -> dict[str, Any]:
        request = {"op": "append_performance", "mandate_id": mandate_id,
                   "agreement_id": agreement_id, "period": period,
                   "kind": kind, "content": content}

        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            rep = self._auth(conn, mandate_id)
            self._get_active_agreement(conn, agreement_id)
            if kind not in PERFORM_KINDS:
                raise ValidationError(f"未知履行结果类型：{kind}")
            if not period:
                raise ValidationError("履约周期不能为空")
            self._ensure_jsonable("content", content)
            return self._insert_ledger(
                conn, agreement_id, period, kind, rep, content)

        try:
            return self._idempotent(business_no, request, mandate_id, action)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                f"{agreement_id} 在 {period} 周期已有 {kind} 记录，"
                "账本只能追加，不能覆盖") from exc

    def append_supplement(self, mandate_id: str, agreement_id: str, period: str,
                          content: dict[str, Any], *,
                          business_no: str | None = None) -> dict[str, Any]:
        """追加补充约定提议；不修改原条款，须双方同意后才成为共同承诺。"""
        request = {"op": "append_supplement", "mandate_id": mandate_id,
                   "agreement_id": agreement_id, "period": period,
                   "content": content}

        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            rep = self._auth(conn, mandate_id)
            self._get_active_agreement(conn, agreement_id)
            self._ensure_jsonable("content", content)
            entry = self._insert_ledger(
                conn, agreement_id, period, "supplement", rep, content)
            entry["consents"] = []
            entry["effective"] = False
            return entry

        return self._idempotent(business_no, request, mandate_id, action)

    def consent_supplement(self, mandate_id: str, entry_id: int, *,
                           business_no: str | None = None) -> dict[str, Any]:
        request = {"op": "consent_supplement", "mandate_id": mandate_id,
                   "entry_id": entry_id}

        def action(conn: sqlite3.Connection) -> dict[str, Any]:
            rep = self._auth(conn, mandate_id)
            row = conn.execute(
                "SELECT le.*, a.status AS agreement_status FROM ledger_entries le "
                "JOIN agreements a ON a.id = le.agreement_id "
                "WHERE le.id = ? AND le.kind = 'supplement'", (entry_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"补充约定不存在：{entry_id}")
            if row["agreement_status"] != "active":
                raise StateError("协议已被替换，补充约定应在新协议下重新约定")
            conn.execute(
                "INSERT INTO supplement_consents(entry_id, side, mandate_id, "
                "consented_at) VALUES (?, ?, ?, ?)",
                (entry_id, rep["side"], mandate_id, self._clock()))
            consents = sorted(r["side"] for r in conn.execute(
                "SELECT sc.side FROM supplement_consents sc "
                "JOIN representatives r ON r.mandate_id = sc.mandate_id "
                "WHERE sc.entry_id = ? AND r.valid_to IS NULL",
                (entry_id,)).fetchall())
            return {"entry_id": entry_id, "consents": consents,
                    "effective": set(consents) == set(SIDES)}

        try:
            return self._idempotent(business_no, request, mandate_id, action)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("本方已同意该补充约定") from exc

    def _insert_ledger(self, conn: sqlite3.Connection, agreement_id: str,
                       period: str, kind: str, rep: dict[str, Any],
                       content: Any) -> dict[str, Any]:
        cur = conn.execute(
            "INSERT INTO ledger_entries(agreement_id, period, kind, reporter_side, "
            "mandate_id, content_json, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (agreement_id, period, kind, rep["side"], rep["mandate_id"],
             canonical_dumps(content), text_hash(content), self._clock()))
        return {"entry_id": cur.lastrowid, "agreement_id": agreement_id,
                "period": period, "kind": kind, "reporter_side": rep["side"],
                "content": content, "content_hash": text_hash(content)}

    # ---- 查询：协议、账本、双方看板 ------------------------------------

    def get_agreement(self, mandate_id: str, agreement_id: str) -> dict[str, Any]:
        conn = self._conn()
        try:
            self._auth(conn, mandate_id)
            row = conn.execute("SELECT * FROM agreements WHERE id = ?",
                               (agreement_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"协议不存在：{agreement_id}")
            return self._agreement_view(conn, dict(row))
        finally:
            conn.close()

    def list_agreements(self, mandate_id: str) -> list[dict]:
        conn = self._conn()
        try:
            self._auth(conn, mandate_id)
            rows = conn.execute(
                "SELECT * FROM agreements ORDER BY signed_at DESC").fetchall()
            return [self._agreement_view(conn, dict(r)) for r in rows]
        finally:
            conn.close()

    def list_ledger(self, mandate_id: str, agreement_id: str) -> list[dict]:
        conn = self._conn()
        try:
            self._auth(conn, mandate_id)
            if conn.execute("SELECT 1 FROM agreements WHERE id = ?",
                            (agreement_id,)).fetchone() is None:
                raise NotFoundError(f"协议不存在：{agreement_id}")
            rows = conn.execute(
                "SELECT * FROM ledger_entries WHERE agreement_id = ? "
                "ORDER BY period, id", (agreement_id,)).fetchall()
            return [self._ledger_view(conn, dict(r)) for r in rows]
        finally:
            conn.close()

    def dashboard(self, mandate_id: str) -> dict[str, Any]:
        """返回调用方视角的待办、未解决分歧与共同有效承诺。"""
        conn = self._conn()
        try:
            rep = self._auth(conn, mandate_id)
            side = rep["side"]
            todos: list[dict[str, Any]] = []
            disagreements: list[dict[str, Any]] = []

            open_negs = conn.execute(
                "SELECT * FROM negotiations WHERE status = 'open'").fetchall()
            for nrow in open_negs:
                neg = dict(nrow)
                versions = conn.execute(
                    "SELECT pv.version_hash, "
                    "(SELECT GROUP_CONCAT(pc.side) FROM package_confirmations pc "
                    "JOIN representatives r ON r.mandate_id = pc.mandate_id "
                    "WHERE pc.negotiation_id = pv.negotiation_id "
                    "AND pc.version_hash = pv.version_hash "
                    "AND r.valid_to IS NULL) AS sides "
                    "FROM package_versions pv WHERE pv.negotiation_id = ? "
                    "ORDER BY pv.created_at", (neg["id"],)).fetchall()
                if versions:
                    latest = versions[-1]
                    confirmed = set(filter(None, (latest["sides"] or "").split(",")))
                    if side not in confirmed:
                        todos.append({
                            "type": "confirm_version",
                            "negotiation_id": neg["id"],
                            "title": neg["title"],
                            "version_hash": latest["version_hash"],
                        })
                    if confirmed == set(SIDES):
                        my_vote = conn.execute(
                            "SELECT v.vote FROM votes v "
                            "JOIN representatives r ON r.mandate_id = v.mandate_id "
                            "WHERE v.negotiation_id = ? AND v.version_hash = ? "
                            "AND v.side = ? AND r.valid_to IS NULL",
                            (neg["id"], latest["version_hash"], side)).fetchone()
                        if my_vote is None:
                            todos.append({
                                "type": "vote",
                                "negotiation_id": neg["id"],
                                "version_hash": latest["version_hash"]})
                        elif my_vote["vote"] == "yes":
                            sig = conn.execute(
                                "SELECT 1 FROM signatures WHERE negotiation_id = ? "
                                "AND version_hash = ? AND side = ?",
                                (neg["id"], latest["version_hash"], side)).fetchone()
                            if sig is None:
                                todos.append({
                                    "type": "sign",
                                    "negotiation_id": neg["id"],
                                    "version_hash": latest["version_hash"]})
                    # 双方各自确认了文本，但没有任何一个版本被双方同时确认
                    confirmed_map = {s: set() for s in SIDES}
                    for v in versions:
                        for s in filter(None, (v["sides"] or "").split(",")):
                            confirmed_map[s].add(v["version_hash"])
                    if (confirmed_map[WORKER] and confirmed_map[COMPANY]
                            and not (confirmed_map[WORKER]
                                     & confirmed_map[COMPANY])):
                        disagreements.append({
                            "type": "version_divergence",
                            "negotiation_id": neg["id"],
                            "title": neg["title"],
                            "worker_versions": sorted(confirmed_map[WORKER]),
                            "company_versions": sorted(confirmed_map[COMPANY]),
                            "detail": "双方确认的不是同一份逐字文本"})
                else:
                    todos.append({"type": "submit_package",
                                  "negotiation_id": neg["id"],
                                  "title": neg["title"]})

                no_votes = conn.execute(
                    "SELECT v.side, v.version_hash FROM votes v "
                    "JOIN representatives r ON r.mandate_id = v.mandate_id "
                    "WHERE v.negotiation_id = ? AND v.vote = 'no' "
                    "AND r.valid_to IS NULL", (neg["id"],)).fetchall()
                for nv in no_votes:
                    disagreements.append({
                        "type": "rejected_vote",
                        "negotiation_id": neg["id"],
                        "version_hash": nv["version_hash"],
                        "vetoed_by": nv["side"]})

                recon = conn.execute(
                    "SELECT id FROM reconsiderations WHERE negotiation_id = ?",
                    (neg["id"],)).fetchone()
                if recon is not None:
                    todos.append({"type": "respond_reconsideration",
                                  "negotiation_id": neg["id"],
                                  "reconsideration_id": recon["id"]})

            active = conn.execute(
                "SELECT * FROM agreements WHERE status = 'active'").fetchone()
            if active is not None:
                active = dict(active)
                disputes = conn.execute(
                    "SELECT * FROM ledger_entries WHERE agreement_id = ? "
                    "AND kind = 'dispute' ORDER BY id", (active["id"],)).fetchall()
                for d in disputes:
                    if not self._dispute_resolved(conn, active["id"], d["id"]):
                        item = {"type": "unresolved_dispute",
                                "agreement_id": active["id"],
                                "period": d["period"],
                                "entry_id": d["id"],
                                "reported_by": d["reporter_side"]}
                        disagreements.append(item)
                        if d["reporter_side"] != side:
                            todos.append({"type": "respond_dispute",
                                          "agreement_id": active["id"],
                                          "period": d["period"],
                                          "entry_id": d["id"]})
                sup_rows = conn.execute(
                    "SELECT * FROM ledger_entries WHERE agreement_id = ? "
                    "AND kind = 'supplement' ORDER BY id",
                    (active["id"],)).fetchall()
                for srow in sup_rows:
                    consents = {r["side"] for r in conn.execute(
                        "SELECT sc.side FROM supplement_consents sc "
                        "JOIN representatives r ON r.mandate_id = sc.mandate_id "
                        "WHERE sc.entry_id = ? AND r.valid_to IS NULL",
                        (srow["id"],)).fetchall()}
                    if side not in consents:
                        todos.append({"type": "consent_supplement",
                                      "agreement_id": active["id"],
                                      "entry_id": srow["id"],
                                      "period": srow["period"]})

            return {
                "side": side,
                "representative": {
                    "mandate_id": rep["mandate_id"],
                    "representative_id": rep["representative_id"],
                    "name": rep["name"]},
                "todos": todos,
                "disagreements": disagreements,
                "commitments": self._commitments(conn),
            }
        finally:
            conn.close()

    def _commitments(self, conn: sqlite3.Connection) -> dict[str, Any]:
        active = conn.execute(
            "SELECT * FROM agreements WHERE status = 'active'").fetchone()
        if active is None:
            return {"active_agreement": None, "supplements": [],
                    "ledger": []}
        active = dict(active)
        supplements = []
        for row in conn.execute(
                "SELECT * FROM ledger_entries WHERE agreement_id = ? "
                "AND kind = 'supplement' ORDER BY id", (active["id"],)):
            consents = sorted(r["side"] for r in conn.execute(
                "SELECT sc.side FROM supplement_consents sc "
                "JOIN representatives r ON r.mandate_id = sc.mandate_id "
                "WHERE sc.entry_id = ? AND r.valid_to IS NULL",
                (row["id"],)).fetchall())
            if set(consents) == set(SIDES):
                view = self._ledger_view(conn, dict(row))
                view["consents"] = consents
                supplements.append(view)
        ledger = [self._ledger_view(conn, dict(r)) for r in conn.execute(
            "SELECT * FROM ledger_entries WHERE agreement_id = ? "
            "AND kind != 'supplement' ORDER BY period, id",
            (active["id"],)).fetchall()]
        return {"active_agreement": self._agreement_view(conn, active),
                "supplements": supplements, "ledger": ledger}

    def _dispute_resolved(self, conn: sqlite3.Connection, agreement_id: str,
                          dispute_entry_id: int) -> bool:
        row = conn.execute(
            "SELECT period FROM ledger_entries WHERE id = ?",
            (dispute_entry_id,)).fetchone()
        if row is None:
            return True
        period = row["period"]
        later_full = conn.execute(
            "SELECT 1 FROM ledger_entries WHERE agreement_id = ? AND period = ? "
            "AND kind = 'full' AND id > ?",
            (agreement_id, period, dispute_entry_id)).fetchone()
        if later_full:
            return True
        for srow in conn.execute(
                "SELECT id, content_json FROM ledger_entries "
                "WHERE agreement_id = ? AND kind = 'supplement' AND id > ?",
                (agreement_id, dispute_entry_id)):
            try:
                content = loads(srow["content_json"])
            except json.JSONDecodeError:
                continue
            resolves = content.get("resolves_entry_id") if isinstance(
                content, dict) else None
            if resolves != dispute_entry_id:
                continue
            consents = {r["side"] for r in conn.execute(
                "SELECT sc.side FROM supplement_consents sc "
                "JOIN representatives r ON r.mandate_id = sc.mandate_id "
                "WHERE sc.entry_id = ? AND r.valid_to IS NULL",
                (srow["id"],)).fetchall()}
            if consents == set(SIDES):
                return True
        return False

    # ---- 履约核查（可在 SIGKILL 后从检查点恢复） -----------------------

    def start_performance_check(
        self, staff_token: str, agreement_id: str, periods: Iterable[str], *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        self._staff(staff_token)
        periods = list(periods)
        if not periods:
            raise ValidationError("至少指定一个核查周期")
        if len(set(periods)) != len(periods):
            raise ValidationError("核查周期不能重复")
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        conn = self._conn()
        try:
            with transaction(conn):
                if conn.execute("SELECT 1 FROM agreements WHERE id = ?",
                                (agreement_id,)).fetchone() is None:
                    raise NotFoundError(f"协议不存在：{agreement_id}")
                existing = conn.execute(
                    "SELECT run_id, status, periods_json FROM check_runs "
                    "WHERE run_id = ?", (run_id,)).fetchone()
                if existing is None:
                    now = self._clock()
                    conn.execute(
                        "INSERT INTO check_runs(run_id, status, cursor_key, total, "
                        "periods_json, created_at, updated_at, agreement_id) "
                        "VALUES (?, 'running', NULL, ?, ?, ?, ?, ?)",
                        (run_id, len(periods), dumps(periods), now, now,
                         agreement_id))
                    return {"run_id": run_id, "agreement_id": agreement_id,
                            "periods": periods, "status": "running",
                            "resumed": False, "done": 0}
                if loads(existing["periods_json"]) != periods:
                    raise TextConflictError(
                        f"核查 {run_id} 已存在且周期清单与本次请求异文")
                done = conn.execute(
                    "SELECT COUNT(*) AS c FROM check_items WHERE run_id = ?",
                    (run_id,)).fetchone()["c"]
                return {"run_id": run_id, "agreement_id": agreement_id,
                        "periods": periods, "status": existing["status"],
                        "resumed": True, "done": done}
        finally:
            conn.close()

    def resume_performance_check(self, staff_token: str, run_id: str) -> dict[str, Any]:
        """从最后一个已提交检查点继续；已完成项跳过，绝不重复入账。"""
        self._staff(staff_token)
        kill_after = os.environ.get("CB_CHECK_KILL_AFTER")
        kill_after_n = int(kill_after) if kill_after else None
        conn = self._conn()
        try:
            run = conn.execute(
                "SELECT * FROM check_runs WHERE run_id = ?", (run_id,),
            ).fetchone()
            if run is None:
                raise NotFoundError(f"核查任务不存在：{run_id}")
            run = dict(run)
            if run["status"] == "done":
                items = conn.execute(
                    "SELECT * FROM check_items WHERE run_id = ? ORDER BY item_key",
                    (run_id,)).fetchall()
                return {"run_id": run_id, "status": "done",
                        "results": [loads(r["result_json"]) for r in items]}
            agreement_id = run["agreement_id"]
            periods = loads(run["periods_json"])
            results: list[dict[str, Any]] = []
            processed_this_turn = 0
            for period in periods:
                item_key = f"period:{period}"
                # 每项独立短事务：进程被 SIGKILL 后已提交项就是恢复点
                item_conn = self._conn()
                try:
                    with transaction(item_conn):
                        done_row = item_conn.execute(
                            "SELECT result_json FROM check_items WHERE run_id = ? "
                            "AND item_key = ?", (run_id, item_key)).fetchone()
                        if done_row is not None:
                            results.append(loads(done_row["result_json"]))
                            continue
                        result = self._check_period(item_conn, agreement_id, period)
                        now = self._clock()
                        item_conn.execute(
                            "INSERT INTO check_items(run_id, item_key, status, "
                            "result_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                            (run_id, item_key, result["status"],
                             dumps(result), now))
                        item_conn.execute(
                            "UPDATE check_runs SET cursor_key = ?, total = ?, "
                            "updated_at = ? WHERE run_id = ?",
                            (item_key, len(periods), now, run_id))
                        results.append(result)
                finally:
                    item_conn.close()
                processed_this_turn += 1
                if kill_after_n is not None and processed_this_turn >= kill_after_n:
                    # 模拟进程在检查中途被强制终止：已提交的检查点保留
                    os.kill(os.getpid(), 9)
            done_conn = self._conn()
            try:
                with transaction(done_conn):
                    done_conn.execute(
                        "UPDATE check_runs SET status = 'done', updated_at = ? "
                        "WHERE run_id = ?", (self._clock(), run_id))
            finally:
                done_conn.close()
            return {"run_id": run_id, "status": "done", "results": results}
        finally:
            conn.close()

    def check_status(self, staff_token: str, run_id: str) -> dict[str, Any]:
        self._staff(staff_token)
        conn = self._conn()
        try:
            run = conn.execute("SELECT * FROM check_runs WHERE run_id = ?",
                               (run_id,)).fetchone()
            if run is None:
                raise NotFoundError(f"核查任务不存在：{run_id}")
            run = dict(run)
            items = conn.execute(
                "SELECT * FROM check_items WHERE run_id = ? ORDER BY item_key",
                (run_id,)).fetchall()
            return {"run_id": run_id, "status": run["status"],
                    "agreement_id": run["agreement_id"],
                    "total": run["total"],
                    "done": len(items),
                    "cursor_key": run["cursor_key"],
                    "results": [loads(r["result_json"]) for r in items]}
        finally:
            conn.close()

    def _check_period(self, conn: sqlite3.Connection, agreement_id: str,
                      period: str) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT * FROM ledger_entries WHERE agreement_id = ? AND period = ? "
            "ORDER BY id", (agreement_id, period)).fetchall()
        kinds = [r["kind"] for r in rows]
        if PERFORM_FULL in kinds:
            status = "done"
            finding = "周期内有实际履行记录"
        elif PERFORM_DISPUTE in kinds:
            dispute_id = next(r["id"] for r in rows if r["kind"] == PERFORM_DISPUTE)
            if self._dispute_resolved(conn, agreement_id, dispute_id):
                status = "done"
                finding = "周期争议已有双方同意的补充约定或后续履行"
            else:
                status = "failed"
                finding = "周期存在尚未解决的争议"
        elif PERFORM_PARTIAL in kinds:
            status = "failed"
            finding = "周期仅为部分履行，需跟进补齐"
        else:
            status = "failed"
            finding = "周期缺少任何履约记录"
        return {"period": period, "status": status, "finding": finding,
                "entries": [r["id"] for r in rows]}

    # ---- 内部辅助 ------------------------------------------------------

    def _agreement_view(self, conn: sqlite3.Connection, agreement: dict) -> dict:
        signatures = [dict(r) for r in conn.execute(
            "SELECT side, mandate_id, representative_id, name, signed_at "
            "FROM signatures WHERE negotiation_id = ? ORDER BY side",
            (agreement["negotiation_id"],)).fetchall()]
        return {
            "id": agreement["id"],
            "negotiation_id": agreement["negotiation_id"],
            "version_hash": agreement["version_hash"],
            "document": loads(agreement["document_json"]),
            "signed_at": agreement["signed_at"],
            "effective_from": agreement["effective_from"],
            "status": agreement["status"],
            "signatures": signatures,
        }

    def _ledger_view(self, conn: sqlite3.Connection, row: dict) -> dict[str, Any]:
        consents = sorted(r["side"] for r in conn.execute(
            "SELECT sc.side FROM supplement_consents sc "
            "JOIN representatives r ON r.mandate_id = sc.mandate_id "
            "WHERE sc.entry_id = ? AND r.valid_to IS NULL",
            (row["id"],)).fetchall())
        return {
            "entry_id": row["id"],
            "agreement_id": row["agreement_id"],
            "period": row["period"],
            "kind": row["kind"],
            "reporter_side": row["reporter_side"],
            "content": loads(row["content_json"]),
            "content_hash": row["content_hash"],
            "created_at": row["created_at"],
            "consents": consents,
        }

    def _get_negotiation(self, conn: sqlite3.Connection,
                         negotiation_id: str) -> dict[str, Any]:
        neg = conn.execute("SELECT * FROM negotiations WHERE id = ?",
                           (negotiation_id,)).fetchone()
        if neg is None:
            raise NotFoundError(f"协商不存在：{negotiation_id}")
        return dict(neg)

    def _get_open_negotiation(self, conn: sqlite3.Connection,
                              negotiation_id: str) -> dict[str, Any]:
        neg = self._get_negotiation(conn, negotiation_id)
        if neg["status"] != "open":
            raise StateError(f"协商已 {neg['status']}，不能再变更")
        return neg

    def _get_active_agreement(self, conn: sqlite3.Connection,
                              agreement_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM agreements WHERE id = ? AND status = 'active'",
            (agreement_id,)).fetchone()
        if row is None:
            raise NotFoundError("没有找到生效协议；履约事实只能向生效协议追加")
        return dict(row)

    def _get_version_document(self, conn: sqlite3.Connection, negotiation_id: str,
                              version_hash: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT document_json FROM package_versions "
            "WHERE negotiation_id = ? AND version_hash = ?",
            (negotiation_id, version_hash)).fetchone()
        if row is None:
            raise NotFoundError("逐字版本不存在，请先提交条款包")
        return loads(row["document_json"])

    def _active_confirmations(self, conn: sqlite3.Connection, negotiation_id: str,
                              version_hash: str) -> set[str]:
        rows = conn.execute(
            "SELECT pc.side FROM package_confirmations pc "
            "JOIN representatives r ON r.mandate_id = pc.mandate_id "
            "WHERE pc.negotiation_id = ? AND pc.version_hash = ? "
            "AND r.valid_to IS NULL",
            (negotiation_id, version_hash)).fetchall()
        return {r["side"] for r in rows}

    def _vote_tally(self, conn: sqlite3.Connection, negotiation_id: str,
                    version_hash: str) -> dict[str, int]:
        rows = conn.execute(
            "SELECT v.vote FROM votes v "
            "JOIN representatives r ON r.mandate_id = v.mandate_id "
            "WHERE v.negotiation_id = ? AND v.version_hash = ? "
            "AND r.valid_to IS NULL",
            (negotiation_id, version_hash)).fetchall()
        tally = {"yes": 0, "no": 0}
        for r in rows:
            tally[r["vote"]] += 1
        return tally

    def _accepted_bases(self, conn: sqlite3.Connection,
                        negotiation_id: str) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            "SELECT side, content_json, accepted_at FROM statements "
            "WHERE negotiation_id = ? AND kind = 'calculation_bases' "
            "AND status = 'accepted' ORDER BY accepted_at, id",
            (negotiation_id,)).fetchall()
        merged: dict[str, dict[str, Any]] = {}
        for r in rows:
            content = loads(r["content_json"])
            if isinstance(content, dict):
                merged.setdefault(r["side"], {}).update(content)
        return merged

    def _linkage(self, conn: sqlite3.Connection, negotiation_id: str,
                 document: dict[str, Any]) -> list[dict[str, str]]:
        from .validation import validate_package
        return validate_package(document,
                                self._accepted_bases(conn, negotiation_id))

    def _build_document(self, clauses: dict[str, dict], assumptions: dict,
                        bases: dict, note: str) -> dict[str, Any]:
        if not isinstance(clauses, dict):
            raise ValidationError("条款必须是以工资/工时/福利为键的结构")
        try:
            package = Package(clauses=clauses, assumptions=assumptions,
                              bases=bases, note=note)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        document = package.to_document()
        self._ensure_jsonable("document", document)
        return document

    @staticmethod
    def _ensure_jsonable(name: str, value: Any) -> None:
        try:
            canonical_dumps(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{name} 不能序列化为逐字文本：{exc}") from exc
