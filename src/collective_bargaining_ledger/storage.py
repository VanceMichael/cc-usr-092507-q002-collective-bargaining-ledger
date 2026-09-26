"""SQLite 持久化层。

并发模型：WAL 日志 + ``busy_timeout``，每个用例使用独立短连接，写事务以
``BEGIN IMMEDIATE`` 开始；SQLite 的单写者串行化保证跨线程、跨进程的原子性。

不可变保证：生效协议、签署记录、已确认的逐字版本、履约账本与重新审议申请
均由触发器拒绝 UPDATE/DELETE（协议只允许 active→superseded 的整条状态迁移）。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 园区工会人员凭据的 SHA-256：首次初始化时写入，之后所有工会端操作以库内值为准，
-- 避免每个调用进程自带凭据自证通过。

CREATE TABLE IF NOT EXISTS representatives (
    mandate_id TEXT PRIMARY KEY,
    representative_id TEXT NOT NULL,
    name TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('worker', 'company')),
    valid_from TEXT NOT NULL,
    valid_to TEXT
);

CREATE TABLE IF NOT EXISTS negotiations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'sealed', 'abandoned')),
    source_agreement_id TEXT,
    created_by_mandate TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sealed_at TEXT
);

CREATE TABLE IF NOT EXISTS statements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    negotiation_id TEXT NOT NULL REFERENCES negotiations(id),
    mandate_id TEXT NOT NULL,
    side TEXT NOT NULL,
    kind TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'accepted', 'lapsed')),
    sensitive INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    accepted_at TEXT,
    accepted_by_mandate TEXT
);

CREATE TABLE IF NOT EXISTS package_versions (
    negotiation_id TEXT NOT NULL,
    version_hash TEXT NOT NULL,
    document_json TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    proposer_mandate TEXT NOT NULL,
    linkage_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (negotiation_id, version_hash)
);

CREATE TABLE IF NOT EXISTS package_confirmations (
    negotiation_id TEXT NOT NULL,
    version_hash TEXT NOT NULL,
    side TEXT NOT NULL,
    mandate_id TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    -- 主键落到授权级：代表更换后旧确认保留为审计痕迹，但不再算作有效确认，
    -- 新任代表必须重新确认（前任未完成的意见不自动继承）。
    PRIMARY KEY (negotiation_id, version_hash, mandate_id)
);

CREATE TABLE IF NOT EXISTS votes (
    negotiation_id TEXT NOT NULL,
    version_hash TEXT NOT NULL,
    side TEXT NOT NULL,
    mandate_id TEXT NOT NULL,
    vote TEXT NOT NULL CHECK (vote IN ('yes', 'no')),
    voted_at TEXT NOT NULL,
    PRIMARY KEY (negotiation_id, version_hash, mandate_id)
);

-- 签署记录在双方签署时逐行落库；任何一个事务提交后外界都能看到完整状态。
CREATE TABLE IF NOT EXISTS signatures (
    negotiation_id TEXT NOT NULL,
    version_hash TEXT NOT NULL,
    side TEXT NOT NULL,
    mandate_id TEXT NOT NULL,
    representative_id TEXT NOT NULL,
    name TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    PRIMARY KEY (negotiation_id, version_hash, mandate_id)
);

CREATE TABLE IF NOT EXISTS agreements (
    id TEXT PRIMARY KEY,
    negotiation_id TEXT NOT NULL UNIQUE,
    version_hash TEXT NOT NULL,
    document_json TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded'))
);
-- 全库任意时刻至多一份生效协议，从数据库层面杜绝并发产生两份有效协议。
CREATE UNIQUE INDEX IF NOT EXISTS agreements_one_active
    ON agreements (status) WHERE status = 'active';

-- 履约账本：只按周期追加，永不修改。
CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id TEXT NOT NULL REFERENCES agreements(id),
    period TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('full', 'partial', 'dispute', 'supplement')),
    reporter_side TEXT NOT NULL,
    mandate_id TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_by_agreement
    ON ledger_entries (agreement_id, period, id);
-- 同一协议同一周期，一方对履行结果（实际/部分/争议）只能追加一条记录
CREATE UNIQUE INDEX IF NOT EXISTS ledger_period_once
    ON ledger_entries (agreement_id, period, reporter_side)
    WHERE kind IN ('full', 'partial', 'dispute');

-- 补充约定的双方同意（账本条目本身不可变，同意只追加）
CREATE TABLE IF NOT EXISTS supplement_consents (
    entry_id INTEGER NOT NULL REFERENCES ledger_entries(id),
    side TEXT NOT NULL,
    mandate_id TEXT NOT NULL,
    consented_at TEXT NOT NULL,
    PRIMARY KEY (entry_id, mandate_id)
);

CREATE TABLE IF NOT EXISTS reconsiderations (
    id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL,
    negotiation_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    mandate_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'sealed'))
);

-- 业务号幂等：同号同文沿用原结果，同号异文由服务层比对后暴露冲突。
CREATE TABLE IF NOT EXISTS idempotency (
    business_no TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    actor_mandate TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 履约核查进度：进程被杀后按 run_id 从最后检查点继续。
CREATE TABLE IF NOT EXISTS check_runs (
    run_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES agreements(id),
    status TEXT NOT NULL CHECK (status IN ('running', 'done')),
    cursor_key TEXT,
    total INTEGER NOT NULL DEFAULT 0,
    periods_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS check_items (
    run_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('done', 'failed')),
    result_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, item_key)
);
"""

_IMMUTABLE_TABLES = (
    "package_versions",
    "package_confirmations",
    "votes",
    "signatures",
    "ledger_entries",
    "supplement_consents",
    "idempotency",
)

_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS statements_content_frozen
BEFORE UPDATE ON statements
WHEN NOT (
    OLD.id = NEW.id
    AND OLD.negotiation_id = NEW.negotiation_id
    AND OLD.mandate_id = NEW.mandate_id
    AND OLD.side = NEW.side
    AND OLD.kind = NEW.kind
    AND OLD.content_hash = NEW.content_hash
    AND OLD.content_json = NEW.content_json
    AND OLD.created_at = NEW.created_at
    AND OLD.sensitive = NEW.sensitive
    AND CASE OLD.status
        WHEN 'open' THEN NEW.status IN ('open', 'accepted', 'lapsed')
        ELSE NEW.status = OLD.status END
)
BEGIN
    SELECT RAISE(ABORT, '陈述内容不可修改；新证据请提交新陈述或发起重新审议');
END;
CREATE TRIGGER IF NOT EXISTS statements_no_delete
BEFORE DELETE ON statements
BEGIN
    SELECT RAISE(ABORT, '陈述为只追加记录，禁止删除');
END;
CREATE TRIGGER IF NOT EXISTS representatives_valid_to_once
BEFORE UPDATE ON representatives
WHEN NOT (
    OLD.mandate_id = NEW.mandate_id
    AND OLD.representative_id = NEW.representative_id
    AND OLD.name = NEW.name
    AND OLD.side = NEW.side
    AND OLD.valid_from = NEW.valid_from
    AND (
        OLD.valid_to IS NEW.valid_to
        OR (OLD.valid_to IS NULL AND NEW.valid_to IS NOT NULL)
    )
)
BEGIN
    SELECT RAISE(ABORT, '授权记录不可变更；更换代表请签发新授权');
END;
CREATE TRIGGER IF NOT EXISTS representatives_no_delete
BEFORE DELETE ON representatives
BEGIN
    SELECT RAISE(ABORT, '授权记录禁止删除');
END;
CREATE TRIGGER IF NOT EXISTS negotiations_status_forward
BEFORE UPDATE ON negotiations
WHEN NOT (
    OLD.id = NEW.id
    AND OLD.title = NEW.title
    AND OLD.source_agreement_id IS NEW.source_agreement_id
    AND OLD.created_by_mandate = NEW.created_by_mandate
    AND OLD.created_at = NEW.created_at
    AND CASE WHEN OLD.status = 'open'
        THEN NEW.status IN ('sealed', 'abandoned')
        ELSE NEW.status = OLD.status END
)
BEGIN
    SELECT RAISE(ABORT, '协商状态只能向前流转，内容不可修改');
END;
CREATE TRIGGER IF NOT EXISTS negotiations_no_delete
BEFORE DELETE ON negotiations
BEGIN
    SELECT RAISE(ABORT, '协商记录禁止删除');
END;
CREATE TRIGGER IF NOT EXISTS agreements_no_delete
BEFORE DELETE ON agreements
BEGIN
    SELECT RAISE(ABORT, '生效协议不可删除');
END;
CREATE TRIGGER IF NOT EXISTS agreements_frozen
BEFORE UPDATE ON agreements
WHEN NOT (
    OLD.status = 'active' AND NEW.status = 'superseded'
    AND OLD.id = NEW.id
    AND OLD.negotiation_id = NEW.negotiation_id
    AND OLD.version_hash = NEW.version_hash
    AND OLD.document_json = NEW.document_json
    AND OLD.signed_at = NEW.signed_at
    AND OLD.effective_from = NEW.effective_from
)
BEGIN
    SELECT RAISE(ABORT, '已签署条款不可修改；新证据只能发起重新审议');
END;
""" + "\n".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {table}_no_update
BEFORE UPDATE ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} 为只追加记录，禁止修改');
END;
CREATE TRIGGER IF NOT EXISTS {table}_no_delete
BEFORE DELETE ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} 为只追加记录，禁止删除');
END;
"""
    for table in _IMMUTABLE_TABLES
) + """
CREATE TRIGGER IF NOT EXISTS reconsiderations_frozen
BEFORE UPDATE ON reconsiderations
WHEN NOT (
    OLD.id = NEW.id
    AND OLD.agreement_id = NEW.agreement_id
    AND OLD.negotiation_id = NEW.negotiation_id
    AND OLD.evidence_json = NEW.evidence_json
    AND OLD.requested_by = NEW.requested_by
    AND OLD.mandate_id = NEW.mandate_id
    AND OLD.created_at = NEW.created_at
    AND (
        OLD.status = NEW.status
        OR (OLD.status = 'open' AND NEW.status = 'sealed')
    )
)
BEGIN
    SELECT RAISE(ABORT, '重新审议证据不可修改；状态仅可由 open 转为 sealed');
END;
CREATE TRIGGER IF NOT EXISTS reconsiderations_no_delete
BEFORE DELETE ON reconsiderations
BEGIN
    SELECT RAISE(ABORT, '重新审议申请为只追加记录，禁止删除');
END;
"""


def connect(db_path: str | Path, *, timeout: float = 30.0) -> sqlite3.Connection:
    path = str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    conn.execute("PRAGMA foreign_keys = ON")
    if path == ":memory:":
        conn.execute("PRAGMA journal_mode = MEMORY")
    else:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """建表并安装不可变触发器（可重复调用）。

    ``executescript`` 会先隐式提交，因此 DDL 与 meta 写入分开处理：
    schema 均为 IF NOT EXISTS 的幂等语句，重复执行不会改动既有数据。
    """
    conn.executescript(SCHEMA)
    conn.executescript(_TRIGGERS)
    with transaction(conn):
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value "
            "WHERE meta.value != excluded.value",
            (SCHEMA_VERSION,),
        )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def loads(value: str) -> Any:
    return json.loads(value)
