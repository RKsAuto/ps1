"""SQLite persistence. All workflow state is durable: a process restart resumes
in-flight checks, scheduled retries and pending jobs from these tables."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    legal_name TEXT,
    business_name TEXT,
    business_type TEXT,
    category TEXT,
    monthly_volume_inr REAL,
    gst_registered INTEGER NOT NULL DEFAULT 0,
    email TEXT,
    phone TEXT,
    city TEXT,
    ifsc TEXT,
    account_holder TEXT,
    merchant_id TEXT,
    capabilities_json TEXT,
    payouts_on_hold INTEGER NOT NULL DEFAULT 0,
    field_report_json TEXT,
    field_updated_json TEXT NOT NULL DEFAULT '{}',
    sandbox_scenario TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    state_entered_at REAL NOT NULL
);

-- Sensitive identifiers: encrypted at rest, masked for display, keyed-hash for matching.
CREATE TABLE IF NOT EXISTS pii_vault (
    application_id TEXT NOT NULL,
    field TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    masked TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'form',
    updated_at REAL NOT NULL,
    PRIMARY KEY (application_id, field)
);
CREATE INDEX IF NOT EXISTS ix_pii_fp ON pii_vault(field, fingerprint);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    filename TEXT,
    media_type TEXT,
    sha256 TEXT,
    method TEXT,
    status TEXT NOT NULL,            -- ACTIVE | SUPERSEDED
    readable INTEGER NOT NULL,
    quality_json TEXT,
    extracted_enc TEXT,              -- raw extracted fields, encrypted
    extracted_masked_json TEXT,      -- masked copy for the UI
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS checks (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    check_type TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    request_id TEXT,
    input_hash TEXT,
    deadline_at REAL,
    reason_code TEXT,
    result_json TEXT,
    waiver_json TEXT,
    updated_at REAL NOT NULL,
    UNIQUE (application_id, check_type)
);

CREATE TABLE IF NOT EXISTS check_requests (
    request_id TEXT PRIMARY KEY,
    check_id TEXT NOT NULL,
    application_id TEXT NOT NULL,
    check_type TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL,            -- SENT | RESPONDED | TIMED_OUT | SUPERSEDED
    sent_at REAL NOT NULL,
    deadline_at REAL NOT NULL,
    responded_at REAL
);

-- Inbox pattern: every callback is recorded exactly once by provider event id.
CREATE TABLE IF NOT EXISTS callback_inbox (
    callback_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    request_id TEXT,
    application_id TEXT,
    outcome TEXT NOT NULL,           -- APPLIED | LATE_APPLIED | DUPLICATE | STALE_IGNORED | UNKNOWN_REQUEST
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    received_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    kind TEXT NOT NULL,              -- MISSING_INFO | EXCEPTION
    code TEXT NOT NULL,
    field TEXT,
    message TEXT NOT NULL,
    severity TEXT NOT NULL,
    source TEXT NOT NULL,            -- validation | dedupe | check:<TYPE> | review | system
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,            -- OPEN | RESOLVED
    resolved_by TEXT,
    created_at REAL NOT NULL,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS review_tasks (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    priority TEXT NOT NULL,
    sla_due_at REAL NOT NULL,
    status TEXT NOT NULL,            -- OPEN | DONE | SUPERSEDED
    decision TEXT,
    reviewer TEXT,
    note TEXT,
    created_at REAL NOT NULL,
    decided_at REAL
);

CREATE TABLE IF NOT EXISTS state_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    event TEXT NOT NULL,
    at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    details_json TEXT NOT NULL,
    at REAL NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);

-- Durable job queue (outbox): dispatching checks and provisioning merchants.
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    application_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    run_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,            -- PENDING | DONE | DEAD
    last_error TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dead_letters (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    application_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    error TEXT NOT NULL,
    status TEXT NOT NULL,            -- OPEN | REPLAYED
    created_at REAL NOT NULL,
    replayed_at REAL
);

"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._local = threading.local()
        self._lock = threading.RLock()

    @contextmanager
    def tx(self):
        """Re-entrant transaction: only the outermost block commits / rolls back."""
        with self._lock:
            depth = getattr(self._local, "depth", 0)
            if depth == 0:
                self.conn.execute("BEGIN IMMEDIATE")
            self._local.depth = depth + 1
            try:
                yield self
            except BaseException:
                self._local.depth = depth
                if depth == 0:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._local.depth = depth
                if depth == 0:
                    self.conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def scalar(self, sql: str, params: tuple | dict = ()):
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return row[0] if row else None


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def loads(text: str | None, default=None):
    if text is None:
        return default
    return json.loads(text)
