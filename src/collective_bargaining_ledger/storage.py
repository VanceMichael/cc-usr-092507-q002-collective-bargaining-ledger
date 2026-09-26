"""SQLite 持久化。

并发正确性不依赖调用方自觉：
- WAL + busy_timeout 配合 ``BEGIN IMMEDIATE``，写事务在入口即排队取锁；
- 进程内再用一把互斥锁，避免同进程多线程互相撞上锁重试；
- 关键不变量由数据库约束兜底：一个回合至多一份协议、
  一个业务号只登记一次、确认/表决/签署按（版本, 方, 任次）唯一。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS rounds (
    round_id      TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    candidate_hash TEXT,
    agreement_id  TEXT,
    predecessor_agreement_id TEXT
);

CREATE TABLE IF NOT EXISTS identities (
    token_hash   TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    side         TEXT NOT NULL,
    generation   INTEGER NOT NULL DEFAULT 0,
    round_id     TEXT REFERENCES rounds(round_id),
    display_name TEXT NOT NULL,
    status       TEXT NOT NULL,
    appointed_at TEXT NOT NULL,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_identities_round_side
    ON identities(round_id, side, status);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id    TEXT NOT NULL REFERENCES rounds(round_id),
    doc_type    TEXT NOT NULL,
    doc_seq     INTEGER NOT NULL,
    author_side TEXT NOT NULL,
    generation  INTEGER NOT NULL,
    identity_hash TEXT,
    payload     TEXT NOT NULL,
    hash        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (round_id, hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_seq
    ON documents(round_id, doc_type, doc_seq);

CREATE TABLE IF NOT EXISTS confirmations (
    round_id      TEXT NOT NULL REFERENCES rounds(round_id),
    hash          TEXT NOT NULL,
    side          TEXT NOT NULL,
    generation    INTEGER NOT NULL,
    identity_hash TEXT NOT NULL,
    confirmed_at  TEXT NOT NULL,
    PRIMARY KEY (round_id, hash, side, generation)
);

CREATE TABLE IF NOT EXISTS linkage_reports (
    hash       TEXT NOT NULL,
    round_id   TEXT NOT NULL REFERENCES rounds(round_id),
    report     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (round_id, hash)
);

CREATE TABLE IF NOT EXISTS votes (
    round_id      TEXT NOT NULL REFERENCES rounds(round_id),
    hash          TEXT NOT NULL,
    side          TEXT NOT NULL,
    generation    INTEGER NOT NULL,
    vote          TEXT NOT NULL,
    identity_hash TEXT NOT NULL,
    voted_at      TEXT NOT NULL,
    PRIMARY KEY (round_id, hash, side, generation)
);

CREATE TABLE IF NOT EXISTS agreements (
    agreement_id   TEXT PRIMARY KEY,
    round_id       TEXT NOT NULL UNIQUE,
    version_hash   TEXT NOT NULL,
    payload        TEXT NOT NULL,
    clauses        TEXT NOT NULL,
    text           TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    status         TEXT NOT NULL,
    superseded_by  TEXT,
    signed_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signatures (
    round_id      TEXT NOT NULL REFERENCES rounds(round_id),
    side          TEXT NOT NULL,
    generation    INTEGER NOT NULL,
    identity_hash TEXT NOT NULL,
    signed_at     TEXT NOT NULL,
    PRIMARY KEY (round_id, side, generation)
);

CREATE TABLE IF NOT EXISTS performance_records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    period       TEXT NOT NULL,
    status       TEXT NOT NULL,
    facts        TEXT NOT NULL,
    author_side  TEXT NOT NULL,
    generation   INTEGER NOT NULL,
    statement    TEXT,
    statement_side TEXT,
    statement_generation INTEGER,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_perf_agreement ON performance_records(agreement_id, id);

CREATE TABLE IF NOT EXISTS disputes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    period       TEXT NOT NULL,
    description  TEXT NOT NULL,
    status       TEXT NOT NULL,
    resolution   TEXT,
    raised_by    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    resolved_at  TEXT
);

CREATE TABLE IF NOT EXISTS supplements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    seq          INTEGER NOT NULL,
    text         TEXT NOT NULL,
    hash         TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL,
    proposed_by  TEXT NOT NULL,
    generation   INTEGER NOT NULL,
    accepted_by  TEXT,
    accepted_generation INTEGER,
    created_at   TEXT NOT NULL,
    accepted_at  TEXT,
    UNIQUE (agreement_id, seq)
);

CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    reason       TEXT NOT NULL,
    evidence     TEXT NOT NULL,
    proposed_payload TEXT,
    requested_by TEXT NOT NULL,
    status       TEXT NOT NULL,
    successor_round_id TEXT REFERENCES rounds(round_id),
    created_at   TEXT NOT NULL,
    decided_at   TEXT,
    decision_note TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id  TEXT NOT NULL REFERENCES rounds(round_id),
    seq       INTEGER NOT NULL,
    at        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    kind      TEXT NOT NULL,
    detail    TEXT NOT NULL,
    UNIQUE (round_id, seq)
);

CREATE TABLE IF NOT EXISTS idempotency (
    request_id   TEXT PRIMARY KEY,
    operation    TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    result       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_state (
    pipeline_id  TEXT PRIMARY KEY,
    round_id     TEXT NOT NULL,
    status       TEXT NOT NULL,
    current_step INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
    pipeline_id TEXT NOT NULL,
    step_index  INTEGER NOT NULL,
    step_name   TEXT NOT NULL,
    attempts    INTEGER NOT NULL,
    detail      TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY (pipeline_id, step_index)
);
"""


class Store:
    """封装连接与事务，服务层只面对参数化 SQL。"""

    def __init__(self, path: str | Path = ":memory:"):
        self._path = str(path)
        self._lock = threading.RLock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,
            timeout=10,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def tx(self) -> "Transaction":
        return Transaction(self._conn, self._lock)

    # 便捷读方法 ----------------------------------------------------------

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())


class Transaction:
    """``BEGIN IMMEDIATE`` 事务：入口取写锁，提交或回滚原子可见。"""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock
        self._began = False

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        # 极端情况下 busy_timeout 内仍可能抛 locked，做有限次重试。
        for attempt in range(5):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._began = True
                return self._conn
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc) and attempt < 4:
                    time.sleep(0.02 * (attempt + 1))
                    continue
                self._lock.release()
                raise
        raise RuntimeError("unreachable")

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._conn.execute("COMMIT")
            elif self._began:
                self._conn.execute("ROLLBACK")
        finally:
            self._lock.release()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(text: str) -> Any:
    return json.loads(text)
