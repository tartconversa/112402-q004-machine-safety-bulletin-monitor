"""调度台查询：当前状态、适用理由、待执行工作与历史变化。

查询函数返回字典列表，供 CLI 或其他前端渲染；所有“当前状态”均取
最新评估/决策，历史明细从只追加表中取全量。
"""
from __future__ import annotations

from .storage import Database


def _like(value: str | None) -> str:
    return f"%{value}%" if value else "%"


def case_status(db: Database, customer: str | None = None,
                asset_id: str | None = None,
                notice_id: str | None = None) -> list[dict]:
    """按客户、设备、通告查明风险事项当前状态与适用理由。"""
    sql = (
        "SELECT c.case_id, c.asset_id, a.customer, a.site, c.notice_id, "
        "r.revision_no, c.status, c.open_reason, c.close_reason, "
        "c.reopened_count, c.updated_at, e.result AS latest_result, "
        "e.reason_code, e.reason_text "
        "FROM risk_cases c "
        "JOIN assets a ON a.asset_id=c.asset_id "
        "JOIN notice_revisions r ON r.revision_id=c.revision_id "
        "LEFT JOIN applicability_evals e ON e.eval_id=( "
        "  SELECT MAX(eval_id) FROM applicability_evals "
        "  WHERE asset_id=c.asset_id AND notice_id=c.notice_id) "
        "WHERE a.customer LIKE ? AND c.asset_id LIKE ? AND c.notice_id LIKE ? "
        "ORDER BY a.customer, c.asset_id, c.notice_id"
    )
    rows = db.query(sql, (_like(customer), _like(asset_id), _like(notice_id)))
    result = [dict(row) for row in rows]
    for item in result:
        decision = db.query_one(
            "SELECT action, rationale, decided_by, decided_at FROM engineer_decisions "
            "WHERE case_id=? ORDER BY decision_id DESC LIMIT 1",
            (item["case_id"],),
        )
        item["latest_decision"] = dict(decision) if decision else None
    return result


def pending_work(db: Database, customer: str | None = None) -> list[dict]:
    """待执行工作：未完成工作单与待发送通知。"""
    orders = db.query(
        "SELECT w.work_order_id, w.case_id, w.asset_id, a.customer, a.site, "
        "c.notice_id AS notice_ref, w.action, w.status, w.revision_id, "
        "r.revision_no, w.created_at FROM work_orders w "
        "JOIN assets a ON a.asset_id=w.asset_id "
        "JOIN risk_cases c ON c.case_id=w.case_id "
        "JOIN notice_revisions r ON r.revision_id=w.revision_id "
        "WHERE w.status IN ('open','in-progress') AND a.customer LIKE ? "
        "ORDER BY a.customer, w.work_order_id",
        (_like(customer),),
    )
    notifications = db.query(
        "SELECT n.notification_id, n.case_id, n.customer, n.site, n.channel, "
        "n.status, n.created_at FROM notifications n "
        "WHERE n.status='pending' AND n.customer LIKE ? "
        "ORDER BY n.customer, n.notification_id",
        (_like(customer),),
    )
    return {
        "open_work_orders": [dict(row) for row in orders],
        "pending_notifications": [dict(row) for row in notifications],
    }


def applicability_history(db: Database, asset_id: str, notice_id: str) -> list[dict]:
    """某设备对某通告的历次评估与适用理由（含每个通告版本）。"""
    rows = db.query(
        "SELECT e.eval_id, e.revision_id, r.revision_no, e.result, e.reason_code, "
        "e.reason_text, e.evaluated_at FROM applicability_evals e "
        "JOIN notice_revisions r ON r.revision_id=e.revision_id "
        "WHERE e.asset_id=? AND e.notice_id=? ORDER BY e.eval_id",
        (asset_id, notice_id),
    )
    return [dict(row) for row in rows]


def case_timeline(db: Database, case_id: str) -> list[dict]:
    """风险事项的全量历史：审计事件、决策、通知、工单、检修、解除。"""
    timeline: list[dict] = []
    for row in db.query(
        "SELECT occurred_at, actor, action, entity_type, entity_id, detail_json "
        "FROM audit_events WHERE entity_id=? OR detail_json LIKE ? "
        "ORDER BY event_id",
        (case_id, f'%"{case_id}"%'),
    ):
        item = dict(row)
        item["stream"] = "audit"
        timeline.append(item)
    for row in db.query(
        "SELECT decided_at AS occurred_at, decided_by AS actor, action, rationale "
        "FROM engineer_decisions WHERE case_id=? ORDER BY decision_id",
        (case_id,),
    ):
        item = dict(row)
        item["stream"] = "decision"
        timeline.append(item)
    for row in db.query(
        "SELECT notification_id, status, channel, created_at, sent_at, "
        "cancelled_at, cancel_reason FROM notifications WHERE case_id=? "
        "ORDER BY created_at",
        (case_id,),
    ):
        item = dict(row)
        item["stream"] = "notification"
        timeline.append(item)
    for row in db.query(
        "SELECT w.work_order_id, w.status, w.action, w.created_at, w.cancelled_at, "
        "w.cancel_reason, r.revision_no FROM work_orders w "
        "JOIN notice_revisions r ON r.revision_id=w.revision_id "
        "WHERE w.case_id=? ORDER BY w.created_at",
        (case_id,),
    ):
        item = dict(row)
        item["stream"] = "work_order"
        timeline.append(item)
    for row in db.query(
        "SELECT i.inspection_id, i.technician, i.observed_firmware, i.findings, "
        "i.submitted_at, i.evidence_sha FROM inspections i WHERE i.case_id=? "
        "ORDER BY i.inspection_id",
        (case_id,),
    ):
        item = dict(row)
        item["stream"] = "inspection"
        timeline.append(item)
    for row in db.query(
        "SELECT review_id, approved, rationale, reviewer, reviewed_at "
        "FROM release_reviews WHERE case_id=? ORDER BY review_id",
        (case_id,),
    ):
        item = dict(row)
        item["stream"] = "release_review"
        timeline.append(item)
    timeline.sort(key=lambda item: item.get("occurred_at")
                  or item.get("created_at") or item.get("submitted_at")
                  or item.get("reviewed_at") or "")
    return timeline


def verification_queue(db: Database, status: str = "open") -> list[dict]:
    rows = db.query(
        "SELECT v.item_id, v.asset_id, a.customer, a.site, a.raw_model, "
        "a.canonical_model, a.serial, v.kind, v.detail, v.status, v.created_at "
        "FROM verification_items v JOIN assets a ON a.asset_id=v.asset_id "
        "WHERE v.status=? ORDER BY v.item_id",
        (status,),
    )
    return [dict(row) for row in rows]


def notice_overview(db: Database) -> list[dict]:
    rows = db.query(
        "SELECT n.notice_id, n.issuer, n.current_state, r.revision_no, "
        "r.change_kind, r.fetched_at, COUNT(DISTINCT s.scope_id) AS scope_rows, "
        "COUNT(DISTINCT a.attachment_id) AS attachment_count "
        "FROM notices n JOIN notice_revisions r ON r.revision_id=n.current_revision_id "
        "LEFT JOIN notice_scope s ON s.revision_id=r.revision_id "
        "LEFT JOIN attachments a ON a.revision_id=r.revision_id "
        "GROUP BY n.notice_id ORDER BY n.notice_id"
    )
    return [dict(row) for row in rows]


def evidence_trail(db: Database, notice_id: str) -> list[dict]:
    """通告每个版本对应的原始证据哈希与附件替换链。"""
    revisions = db.query(
        "SELECT r.revision_id, r.revision_no, r.state, r.change_kind, r.content_sha, "
        "r.fetched_at, e.media_type, e.origin FROM notice_revisions r "
        "LEFT JOIN evidence_refs e ON e.sha256=r.content_sha "
        "WHERE r.notice_id=? ORDER BY r.revision_id",
        (notice_id,),
    )
    result = []
    for row in revisions:
        item = dict(row)
        item["scope"] = [
            dict(scope) for scope in db.query(
                "SELECT model, serial_from, serial_to, firmware FROM notice_scope "
                "WHERE revision_id=?", (row["revision_id"],)
            )
        ]
        item["attachments"] = [
            dict(attachment) for attachment in db.query(
                "SELECT attachment_id, rel_url, content_sha, media_type, fetched_at, "
                "replaced_attachment_id FROM attachments WHERE notice_id=? "
                "ORDER BY attachment_id", (notice_id,)
            )
        ]
        result.append(item)
    return result
