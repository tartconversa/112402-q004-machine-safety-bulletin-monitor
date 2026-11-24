"""SQLite 持久层：表结构、连接管理与审计事件。

所有业务表都带审计线索；历史表（评估、决策、通知、工作单、检修、解除）
只追加不覆盖，风险事项与设备表通过状态字段反映当前状态。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .evidence import utcnow

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sources (
    source_id     TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('http','file')),
    url           TEXT NOT NULL,
    parser        TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_etag     TEXT,
    last_modified TEXT
);

CREATE TABLE IF NOT EXISTS fetch_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     TEXT NOT NULL REFERENCES sources(source_id),
    fetched_at    TEXT NOT NULL,
    http_status   INTEGER,
    content_sha   TEXT REFERENCES evidence_refs(sha256),
    ok            INTEGER NOT NULL,
    error         TEXT,
    is_conditional_hit INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS evidence_refs (
    sha256        TEXT PRIMARY KEY,
    media_type    TEXT,
    size_bytes    INTEGER NOT NULL,
    stored_at     TEXT NOT NULL,
    origin        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notices (
    notice_id     TEXT PRIMARY KEY,
    issuer        TEXT NOT NULL,
    title         TEXT,
    current_revision_id INTEGER,
    current_state TEXT NOT NULL DEFAULT 'active'
                  CHECK (current_state IN ('active','withdrawn')),
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notice_revisions (
    revision_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id     TEXT NOT NULL REFERENCES notices(notice_id),
    revision_no   TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('active','withdrawn')),
    content_sha   TEXT NOT NULL,
    run_id        INTEGER REFERENCES fetch_runs(run_id),
    fetched_at    TEXT NOT NULL,
    change_kind   TEXT NOT NULL,
    UNIQUE(notice_id, revision_no, content_sha)
);

CREATE TABLE IF NOT EXISTS notice_scope (
    scope_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id   INTEGER NOT NULL REFERENCES notice_revisions(revision_id),
    model         TEXT NOT NULL,
    serial_from   TEXT,
    serial_to     TEXT,
    firmware      TEXT
);

CREATE TABLE IF NOT EXISTS attachments (
    attachment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id     TEXT NOT NULL REFERENCES notices(notice_id),
    revision_id   INTEGER REFERENCES notice_revisions(revision_id),
    rel_url       TEXT NOT NULL,
    content_sha   TEXT NOT NULL,
    media_type    TEXT,
    fetched_at    TEXT NOT NULL,
    replaced_attachment_id INTEGER REFERENCES attachments(attachment_id),
    UNIQUE(notice_id, rel_url, content_sha)
);

CREATE TABLE IF NOT EXISTS model_aliases (
    alias         TEXT PRIMARY KEY,
    canonical     TEXT NOT NULL,
    note          TEXT,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS asset_imports (
    import_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_label  TEXT NOT NULL,
    content_sha   TEXT,
    imported_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id      TEXT PRIMARY KEY,
    customer      TEXT NOT NULL,
    canonical_model TEXT NOT NULL,
    raw_model     TEXT NOT NULL,
    serial        TEXT,
    firmware      TEXT,
    site          TEXT,
    import_id     INTEGER REFERENCES asset_imports(import_id),
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verification_items (
    item_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id      TEXT NOT NULL REFERENCES assets(asset_id),
    kind          TEXT NOT NULL CHECK (kind IN
                  ('missing-serial','conflicting-record','unknown-model')),
    detail        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','resolved','ignored')),
    created_at    TEXT NOT NULL,
    resolved_at   TEXT,
    resolution    TEXT
);

CREATE TABLE IF NOT EXISTS applicability_evals (
    eval_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id      TEXT NOT NULL REFERENCES assets(asset_id),
    notice_id     TEXT NOT NULL REFERENCES notices(notice_id),
    revision_id   INTEGER NOT NULL REFERENCES notice_revisions(revision_id),
    result        TEXT NOT NULL CHECK (result IN
                  ('applicable','not-applicable','unverifiable')),
    reason_code   TEXT NOT NULL,
    reason_text   TEXT NOT NULL,
    evaluated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_cases (
    case_id       TEXT PRIMARY KEY,
    asset_id      TEXT NOT NULL REFERENCES assets(asset_id),
    notice_id     TEXT NOT NULL REFERENCES notices(notice_id),
    status        TEXT NOT NULL CHECK (status IN
                  ('open','in-progress','closed')),
    open_reason   TEXT NOT NULL,
    close_reason  TEXT,
    revision_id   INTEGER NOT NULL REFERENCES notice_revisions(revision_id),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    closed_at     TEXT,
    reopened_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(asset_id, notice_id)
);

CREATE TABLE IF NOT EXISTS engineer_decisions (
    decision_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id       TEXT NOT NULL REFERENCES risk_cases(case_id),
    revision_id   INTEGER NOT NULL REFERENCES notice_revisions(revision_id),
    action        TEXT NOT NULL CHECK (action IN
                  ('stop','inspect','upgrade','no-action')),
    rationale     TEXT NOT NULL,
    decided_by    TEXT NOT NULL,
    decided_at    TEXT NOT NULL,
    policy_warning TEXT
);

CREATE TABLE IF NOT EXISTS site_policies (
    policy_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    customer      TEXT NOT NULL,
    site          TEXT,
    allowed_actions TEXT NOT NULL,
    note          TEXT,
    updated_at    TEXT NOT NULL,
    UNIQUE(customer, site)
);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    case_id       TEXT NOT NULL REFERENCES risk_cases(case_id),
    customer      TEXT NOT NULL,
    site          TEXT,
    revision_id   INTEGER NOT NULL REFERENCES notice_revisions(revision_id),
    channel       TEXT NOT NULL,
    message       TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN
                  ('pending','sent','cancelled')),
    created_at    TEXT NOT NULL,
    sent_at       TEXT,
    cancelled_at  TEXT,
    cancel_reason TEXT
);

CREATE TABLE IF NOT EXISTS work_orders (
    work_order_id TEXT PRIMARY KEY,
    case_id       TEXT NOT NULL REFERENCES risk_cases(case_id),
    notification_id TEXT REFERENCES notifications(notification_id),
    asset_id      TEXT NOT NULL REFERENCES assets(asset_id),
    action        TEXT NOT NULL,
    revision_id   INTEGER NOT NULL REFERENCES notice_revisions(revision_id),
    status        TEXT NOT NULL CHECK (status IN
                  ('open','in-progress','done','cancelled')),
    created_at    TEXT NOT NULL,
    cancelled_at  TEXT,
    cancel_reason TEXT
);

CREATE TABLE IF NOT EXISTS inspections (
    inspection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),
    case_id       TEXT NOT NULL REFERENCES risk_cases(case_id),
    observed_firmware TEXT,
    findings      TEXT NOT NULL,
    technician    TEXT NOT NULL,
    submitted_at  TEXT NOT NULL,
    evidence_sha  TEXT
);

CREATE TABLE IF NOT EXISTS release_reviews (
    review_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id       TEXT NOT NULL REFERENCES risk_cases(case_id),
    inspection_id INTEGER REFERENCES inspections(inspection_id),
    approved      INTEGER NOT NULL,
    rationale     TEXT NOT NULL,
    reviewer      TEXT NOT NULL,
    reviewed_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at   TEXT NOT NULL,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    entity_type   TEXT NOT NULL,
    entity_id     TEXT,
    detail_json   TEXT
);

CREATE TABLE IF NOT EXISTS id_sequences (
    name          TEXT PRIMARY KEY,
    next_value    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scope_revision ON notice_scope(revision_id);
CREATE INDEX IF NOT EXISTS idx_revisions_notice ON notice_revisions(notice_id);
CREATE INDEX IF NOT EXISTS idx_evals_notice ON applicability_evals(notice_id);
CREATE INDEX IF NOT EXISTS idx_evals_asset ON applicability_evals(asset_id);
CREATE INDEX IF NOT EXISTS idx_cases_notice ON risk_cases(notice_id);
CREATE INDEX IF NOT EXISTS idx_work_orders_case ON work_orders(case_id);
CREATE INDEX IF NOT EXISTS idx_notifications_case ON notifications(case_id);
"""


class Database:
    """SQLite 连接封装，外键与 WAL 默认开启。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")

    def initialize(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO id_sequences(name, next_value) VALUES (?, ?)",
            ("work_order", 1),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- 基础辅助 -------------------------------------------------------
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def commit(self) -> None:
        self.conn.commit()

    def next_id(self, name: str, prefix: str, width: int = 4) -> str:
        row = self.query_one(
            "SELECT next_value FROM id_sequences WHERE name=?", (name,)
        )
        if row is None:
            self.execute(
                "INSERT INTO id_sequences(name, next_value) VALUES(?, ?)",
                (name, 2),
            )
            value = 1
        else:
            value = row["next_value"]
            self.execute(
                "UPDATE id_sequences SET next_value=? WHERE name=?",
                (value + 1, name),
            )
        return f"{prefix}{value:0{width}d}"

    def audit(
        self,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str | None,
        detail: dict | None = None,
    ) -> None:
        import json

        self.execute(
            "INSERT INTO audit_events(occurred_at, actor, action, entity_type, "
            "entity_id, detail_json) VALUES(?,?,?,?,?,?)",
            (
                utcnow(),
                actor,
                action,
                entity_type,
                entity_id,
                json.dumps(detail, ensure_ascii=False) if detail else None,
            ),
        )
