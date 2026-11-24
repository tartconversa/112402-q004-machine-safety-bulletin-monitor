"""采集服务：注册源、轮询、差异识别并固化原始证据。

差异类型（写入 ``notice_revisions.change_kind`` / 审计事件）：
- new: 新通告；
- new-revision: 通告新增版本（范围更正等）；
- revision-corrected: 同版本号内容哈希变化（发布方静默更正）；
- withdrawn: 通告撤回；
- reactivated: 撤回通告重新生效；
- unchanged: 内容未变化（仅保留抓取记录）。
附件在通告同版本内哈希变化时标记为 replaced，并保留旧附件行。
"""
from __future__ import annotations

from dataclasses import dataclass

from .evidence import EvidenceStore, utcnow
from .sources import FetchResult, get_fetcher, get_parser
from .storage import Database

VALID_CHANGE_KINDS = {
    "new",
    "new-revision",
    "revision-corrected",
    "withdrawn",
    "reactivated",
    "unchanged",
}


@dataclass
class CollectionReport:
    source_id: str
    ok: bool
    fetched: bool
    change_kinds: list[str]
    attachment_events: list[str]
    error: str | None = None


def register_source(
    db: Database,
    source_id: str,
    name: str,
    kind: str,
    url: str,
    parser: str,
    actor: str = "system",
) -> None:
    if parser not in ("csv_feed", "json_feed", "html_page"):
        raise ValueError(f"未知解析器 {parser}")
    if kind not in ("http", "file"):
        raise ValueError(f"未知源类型 {kind}")
    now = utcnow()
    existing = db.query_one(
        "SELECT source_id FROM sources WHERE source_id=?", (source_id,)
    )
    if existing:
        db.execute(
            "UPDATE sources SET name=?, kind=?, url=?, parser=?, enabled=1 "
            "WHERE source_id=?",
            (name, kind, url, parser, source_id),
        )
        action = "source-updated"
    else:
        db.execute(
            "INSERT INTO sources(source_id, name, kind, url, parser, enabled, "
            "created_at) VALUES(?,?,?,?,?,1,?)",
            (source_id, name, kind, url, parser, now),
        )
        action = "source-registered"
    db.audit(actor, action, "source", source_id, {"url": url, "parser": parser})
    db.commit()


def _store_evidence(
    db: Database, store: EvidenceStore, data: bytes, media_type: str | None,
    origin: str,
) -> str:
    record = store.put(data, media_type)
    if not db.query_one(
        "SELECT sha256 FROM evidence_refs WHERE sha256=?", (record.sha256,)
    ):
        db.execute(
            "INSERT INTO evidence_refs(sha256, media_type, size_bytes, stored_at, "
            "origin) VALUES(?,?,?,?,?)",
            (record.sha256, media_type, record.size_bytes, utcnow(), origin),
        )
    return record.sha256


def _fetch_attachment(
    db: Database, store: EvidenceStore, fetcher, base_url: str, rel_url: str
) -> tuple[str, str]:
    absolute = fetcher.resolve_attachment(base_url, rel_url)
    result = fetcher.fetch(absolute)
    if not result.ok or result.data is None:
        raise IOError(f"附件抓取失败 {absolute}: {result.error}")
    digest = _store_evidence(
        db, store, result.data, result.media_type, absolute
    )
    return digest, result.media_type or "application/octet-stream"


def collect_source(
    db: Database,
    store: EvidenceStore,
    source_id: str,
    actor: str = "collector",
    fetch_attachments: bool = True,
) -> CollectionReport:
    """轮询单个源并把差异落库；任何异常都记录为失败的抓取而不抛出。"""
    source = db.query_one(
        "SELECT * FROM sources WHERE source_id=? AND enabled=1", (source_id,)
    )
    if source is None:
        return CollectionReport(source_id, False, False, [], [], "源不存在或已停用")

    fetcher = get_fetcher(source["kind"])
    result: FetchResult = fetcher.fetch(
        source["url"], source["last_etag"], source["last_modified"]
    )
    if not result.ok:
        run_id = _record_run(db, source_id, result, None)
        db.audit(
            actor, "fetch-failed", "source", source_id,
            {"run_id": run_id, "error": result.error},
        )
        db.commit()
        return CollectionReport(
            source_id, False, False, [], [], result.error
        )
    if result.conditional_hit:
        run_id = _record_run(db, source_id, result, None)
        db.commit()
        return CollectionReport(source_id, True, False, [], [])

    assert result.data is not None
    digest = _store_evidence(
        db, store, result.data, result.media_type, source["url"]
    )
    run_id = _record_run(db, source_id, result, digest)

    parser = get_parser(source["parser"])
    try:
        records = parser.parse(result.data)
    except (ValueError, KeyError, UnicodeDecodeError) as exc:
        db.audit(
            actor, "parse-failed", "source", source_id,
            {"run_id": run_id, "error": str(exc)},
        )
        db.commit()
        return CollectionReport(
            source_id, False, True, [], [], f"解析失败: {exc}"
        )

    change_kinds: list[str] = []
    attachment_events: list[str] = []
    for record in records:
        kind = _persist_record(
            db, store, fetcher, source, record, digest, run_id,
            actor, fetch_attachments, attachment_events,
        )
        change_kinds.append(kind)

    db.execute(
        "UPDATE sources SET last_etag=?, last_modified=? WHERE source_id=?",
        (result.etag, result.last_modified, source_id),
    )
    db.commit()
    return CollectionReport(
        source_id, True, True, change_kinds, attachment_events
    )


def _record_run(
    db: Database, source_id: str, result: FetchResult, digest: str | None
) -> int:
    cursor = db.execute(
        "INSERT INTO fetch_runs(source_id, fetched_at, http_status, content_sha, "
        "ok, error, is_conditional_hit) VALUES(?,?,?,?,?,?,?)",
        (
            source_id,
            utcnow(),
            result.http_status,
            digest,
            1 if result.ok else 0,
            result.error,
            1 if result.conditional_hit else 0,
        ),
    )
    return int(cursor.lastrowid)


def _persist_record(
    db: Database,
    store: EvidenceStore,
    fetcher,
    source,
    record,
    content_sha: str,
    run_id: int,
    actor: str,
    fetch_attachments: bool,
    attachment_events: list[str],
) -> str:
    now = utcnow()
    notice = db.query_one(
        "SELECT * FROM notices WHERE notice_id=?", (record.notice_id,)
    )
    if notice is None:
        db.execute(
            "INSERT INTO notices(notice_id, issuer, title, current_state, "
            "first_seen_at, updated_at) VALUES(?,?,?, 'active', ?, ?)",
            (record.notice_id, record.issuer, record.title, now, now),
        )
        notice = db.query_one(
            "SELECT * FROM notices WHERE notice_id=?", (record.notice_id,)
        )
        change_kind = "new"
    else:
        change_kind = _classify_change(db, record, notice, content_sha)

    revision = db.query_one(
        "SELECT * FROM notice_revisions WHERE notice_id=? AND revision_no=? "
        "AND content_sha=?",
        (record.notice_id, record.revision_no, content_sha),
    )
    if revision is None:
        cursor = db.execute(
            "INSERT INTO notice_revisions(notice_id, revision_no, state, "
            "content_sha, run_id, fetched_at, change_kind) VALUES(?,?,?,?,?,?,?)",
            (
                record.notice_id,
                record.revision_no,
                record.state,
                content_sha,
                run_id,
                now,
                change_kind,
            ),
        )
        revision_id = int(cursor.lastrowid)
        for entry in record.scope:
            db.execute(
                "INSERT INTO notice_scope(revision_id, model, serial_from, "
                "serial_to, firmware) VALUES(?,?,?,?,?)",
                (
                    revision_id,
                    entry.model,
                    entry.serial_from,
                    entry.serial_to,
                    entry.firmware,
                ),
            )
        db.audit(
            actor, f"notice-{change_kind}", "notice", record.notice_id,
            {
                "revision_no": record.revision_no,
                "state": record.state,
                "run_id": run_id,
                "scope": [entry.as_dict() for entry in record.scope],
            },
        )
    else:
        revision_id = revision["revision_id"]

    if fetch_attachments:
        for attachment in record.attachments:
            try:
                event = _persist_attachment(
                    db, store, fetcher, source, record.notice_id,
                    revision_id, attachment.rel_url,
                )
            except OSError as exc:
                # 单个附件失败不影响通告正文与其他附件，记录审计后继续
                db.audit(
                    actor, "attachment-fetch-failed", "notice",
                    record.notice_id,
                    {"rel_url": attachment.rel_url, "error": str(exc),
                     "revision_id": revision_id},
                )
                attachment_events.append(f"failed:{record.notice_id}:{attachment.rel_url}")
                continue
            if event:
                attachment_events.append(event)

    db.execute(
        "UPDATE notices SET issuer=?, title=COALESCE(?, title), "
        "current_revision_id=?, current_state=?, updated_at=? WHERE notice_id=?",
        (
            record.issuer,
            record.title,
            revision_id,
            record.state,
            now,
            record.notice_id,
        ),
    )
    return change_kind


def _classify_change(db, record, notice, content_sha: str) -> str:
    """根据版本号、状态与快照哈希判断本次抓取的差异类型。"""
    if record.state == "withdrawn" and notice["current_state"] != "withdrawn":
        return "withdrawn"
    if record.state == "active" and notice["current_state"] == "withdrawn":
        return "reactivated"
    same_hash = db.query_one(
        "SELECT 1 FROM notice_revisions WHERE notice_id=? AND revision_no=? "
        "AND content_sha=? LIMIT 1",
        (record.notice_id, record.revision_no, content_sha),
    )
    if same_hash is not None:
        return "unchanged"
    same_revision = db.query_one(
        "SELECT 1 FROM notice_revisions WHERE notice_id=? AND revision_no=? LIMIT 1",
        (record.notice_id, record.revision_no),
    )
    # 同版本号但快照哈希不同：发布方静默更正；否则是新版本（范围更正）。
    return "revision-corrected" if same_revision is not None else "new-revision"



def _persist_attachment(
    db: Database,
    store: EvidenceStore,
    fetcher,
    source,
    notice_id: str,
    revision_id: int,
    rel_url: str,
) -> str | None:
    digest, media_type = _fetch_attachment(
        db, store, fetcher, source["url"], rel_url
    )
    existing = db.query_one(
        "SELECT * FROM attachments WHERE notice_id=? AND rel_url=? AND content_sha=?",
        (notice_id, rel_url, digest),
    )
    now = utcnow()
    if existing is None:
        previous = db.query_one(
            "SELECT * FROM attachments WHERE notice_id=? AND rel_url=? "
            "ORDER BY attachment_id DESC LIMIT 1",
            (notice_id, rel_url),
        )
        replaced_id = previous["attachment_id"] if previous else None
        db.execute(
            "INSERT INTO attachments(notice_id, revision_id, rel_url, content_sha, "
            "media_type, fetched_at, replaced_attachment_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                notice_id,
                revision_id,
                rel_url,
                digest,
                media_type,
                now,
                replaced_id,
            ),
        )
        if replaced_id is None:
            event = f"added:{notice_id}:{rel_url}"
            db.audit(
                "collector", "attachment-added", "notice", notice_id,
                {"rel_url": rel_url, "sha256": digest, "revision_id": revision_id},
            )
        else:
            event = f"replaced:{notice_id}:{rel_url}"
            db.audit(
                "collector", "attachment-replaced", "notice", notice_id,
                {
                    "rel_url": rel_url,
                    "old_sha256": previous["content_sha"],
                    "new_sha256": digest,
                    "replaced_attachment_id": replaced_id,
                },
            )
        return event
    return None


def collect_all(
    db: Database, store: EvidenceStore, actor: str = "collector"
) -> list[CollectionReport]:
    reports: list[CollectionReport] = []
    for source in db.query("SELECT source_id FROM sources WHERE enabled=1"):
        reports.append(collect_source(db, store, source["source_id"], actor))
    return reports
