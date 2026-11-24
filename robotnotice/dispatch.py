"""服务调度：通知受影响工厂、生成工作单、检修回执与解除评审。

工作单必须基于安全工程师的显式决策生成；通知在实际发出前可因通告
撤回/范围缩窄被取消。工作单与通知都锚定创建时的通告版本，后续修订
不回写这些记录，从而保留历史处置所依据的通告版本。
"""
from __future__ import annotations

from .evidence import utcnow
from .storage import Database
from .workflow import ACTION_LABELS, latest_decision


def _require_active_case(db: Database, case_id: str):
    case = db.query_one("SELECT * FROM risk_cases WHERE case_id=?", (case_id,))
    if case is None:
        raise ValueError(f"风险事项 {case_id} 不存在")
    if case["status"] == "closed":
        raise ValueError(f"风险事项 {case_id} 已关闭")
    return case


def notify_factory(
    db: Database,
    case_id: str,
    channel: str,
    message: str,
    actor: str = "dispatcher",
) -> str:
    """为风险事项创建待发送通知；尚未发送前允许取消。"""
    case = _require_active_case(db, case_id)
    asset = db.query_one(
        "SELECT * FROM assets WHERE asset_id=?", (case["asset_id"],)
    )
    notification_id = db.next_id("notification", "NT-")
    now = utcnow()
    db.execute(
        "INSERT INTO notifications(notification_id, case_id, customer, site, "
        "revision_id, channel, message, status, created_at) "
        "VALUES(?,?,?,?,?,?,?, 'pending', ?)",
        (
            notification_id,
            case_id,
            asset["customer"],
            asset["site"],
            case["revision_id"],
            channel,
            message,
            now,
        ),
    )
    db.audit(
        actor, "notification-created", "notification", notification_id,
        {"case_id": case_id, "customer": asset["customer"],
         "site": asset["site"], "channel": channel,
         "revision_id": case["revision_id"]},
    )
    db.commit()
    return notification_id


def mark_notification_sent(
    db: Database, notification_id: str, actor: str = "dispatcher"
) -> None:
    row = db.query_one(
        "SELECT * FROM notifications WHERE notification_id=?", (notification_id,)
    )
    if row is None:
        raise ValueError(f"通知 {notification_id} 不存在")
    if row["status"] != "pending":
        raise ValueError(f"通知 {notification_id} 状态为 {row['status']}，不能标记发送")
    now = utcnow()
    db.execute(
        "UPDATE notifications SET status='sent', sent_at=? WHERE notification_id=?",
        (now, notification_id),
    )
    db.audit(
        actor, "notification-sent", "notification", notification_id,
        {"case_id": row["case_id"]},
    )
    db.commit()


def create_work_order(
    db: Database,
    case_id: str,
    actor: str = "dispatcher",
    notification_id: str | None = None,
) -> str:
    """依据工程师最新决策生成工作单；无决策时拒绝创建。"""
    case = _require_active_case(db, case_id)
    decision = latest_decision(db, case_id)
    if decision is None:
        raise PermissionError(
            "安全工程师尚未作出处置决策，网页命中不能直接生成维修指令"
        )
    if decision["action"] == "no-action":
        raise ValueError("该事项决策为不动作，无需生成工作单")
    if notification_id is not None:
        notification = db.query_one(
            "SELECT * FROM notifications WHERE notification_id=?",
            (notification_id,),
        )
        if notification is None or notification["case_id"] != case_id:
            raise ValueError("通知与风险事项不匹配")
        if notification["status"] == "cancelled":
            raise ValueError("依据的通知已被取消，请重新评估后再派单")

    work_order_id = db.next_id("work_order", "WO-")
    now = utcnow()
    db.execute(
        "INSERT INTO work_orders(work_order_id, case_id, notification_id, asset_id, "
        "action, revision_id, status, created_at) VALUES(?,?,?,?,?,?, 'open', ?)",
        (
            work_order_id,
            case_id,
            notification_id,
            case["asset_id"],
            decision["action"],
            case["revision_id"],
            now,
        ),
    )
    db.audit(
        actor, "work-order-created", "work_order", work_order_id,
        {"case_id": case_id, "action": decision["action"],
         "decision_id": decision["decision_id"],
         "notification_id": notification_id,
         "revision_id": case["revision_id"]},
    )
    db.commit()
    return work_order_id


def start_work_order(
    db: Database, work_order_id: str, actor: str = "technician"
) -> None:
    _update_work_order_status(db, work_order_id, "in-progress", actor)


def _update_work_order_status(
    db: Database, work_order_id: str, new_status: str, actor: str
) -> None:
    row = db.query_one(
        "SELECT * FROM work_orders WHERE work_order_id=?", (work_order_id,)
    )
    if row is None:
        raise ValueError(f"工作单 {work_order_id} 不存在")
    if row["status"] in ("done", "cancelled"):
        raise ValueError(f"工作单 {work_order_id} 已 {row['status']}，不可变更")
    db.execute(
        "UPDATE work_orders SET status=? WHERE work_order_id=?",
        (new_status, work_order_id),
    )
    db.audit(
        actor, f"work-order-{new_status}", "work_order", work_order_id,
        {"case_id": row["case_id"]},
    )
    db.commit()


def submit_inspection(
    db: Database,
    work_order_id: str,
    technician: str,
    findings: str,
    observed_firmware: str | None = None,
    evidence_sha: str | None = None,
    actor: str | None = None,
) -> int:
    """维修人员提交现场回执；工作单标记完成，风险事项等待安全工程师解除。"""
    work_order = db.query_one(
        "SELECT * FROM work_orders WHERE work_order_id=?", (work_order_id,)
    )
    if work_order is None:
        raise ValueError(f"工作单 {work_order_id} 不存在")
    if work_order["status"] in ("cancelled",):
        raise ValueError("工作单已取消，不能提交回执")
    now = utcnow()
    cursor = db.execute(
        "INSERT INTO inspections(work_order_id, case_id, observed_firmware, "
        "findings, technician, submitted_at, evidence_sha) VALUES(?,?,?,?,?,?,?)",
        (
            work_order_id,
            work_order["case_id"],
            observed_firmware,
            findings,
            technician,
            now,
            evidence_sha,
        ),
    )
    inspection_id = int(cursor.lastrowid)
    if work_order["status"] != "done":
        db.execute(
            "UPDATE work_orders SET status='done' WHERE work_order_id=?",
            (work_order_id,),
        )
    db.audit(
        actor or technician, "inspection-submitted", "inspection",
        str(inspection_id),
        {"work_order_id": work_order_id, "case_id": work_order["case_id"],
         "observed_firmware": observed_firmware, "evidence_sha": evidence_sha},
    )
    db.commit()
    return inspection_id


def review_release(
    db: Database,
    case_id: str,
    reviewer: str,
    approved: bool,
    rationale: str,
    actor: str | None = None,
) -> int:
    """安全工程师依据最新检修回执判断是否满足解除条件。"""
    case = db.query_one("SELECT * FROM risk_cases WHERE case_id=?", (case_id,))
    if case is None:
        raise ValueError(f"风险事项 {case_id} 不存在")
    inspection = db.query_one(
        "SELECT i.* FROM inspections i JOIN work_orders w "
        "ON w.work_order_id=i.work_order_id WHERE i.case_id=? "
        "ORDER BY i.inspection_id DESC LIMIT 1",
        (case_id,),
    )
    if approved and inspection is None:
        raise ValueError("没有检修回执，不能批准解除")
    now = utcnow()
    cursor = db.execute(
        "INSERT INTO release_reviews(case_id, inspection_id, approved, rationale, "
        "reviewer, reviewed_at) VALUES(?,?,?,?,?,?)",
        (case_id, inspection["inspection_id"] if inspection else None,
         1 if approved else 0, rationale, reviewer, now),
    )
    review_id = int(cursor.lastrowid)
    if approved:
        db.execute(
            "UPDATE risk_cases SET status='closed', close_reason=?, closed_at=?, "
            "updated_at=? WHERE case_id=?",
            (f"解除条件满足：{rationale}", now, now, case_id),
        )
    db.audit(
        actor or reviewer,
        "release-approved" if approved else "release-rejected",
        "risk_case", case_id,
        {"review_id": review_id,
         "inspection_id": inspection["inspection_id"] if inspection else None,
         "rationale": rationale},
    )
    db.commit()
    return review_id


def case_action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action)
