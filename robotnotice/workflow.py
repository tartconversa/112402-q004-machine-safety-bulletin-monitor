"""风险事项生命周期与安全工程师决策。

关键规则：
- 采集与匹配只产生带证据的适用结论，风险事项由本模块同步，
  工程师必须显式决策（停用/检查/升级/不动作），网页命中不直接下工单；
- 通告撤回或范围缩窄时，自动关闭不再适用的事项，尚未执行的通知与
  工作单同步取消；已完成的检修记录按原通告版本永久保留；
- 修订后通告重新覆盖已关闭设备时，自动重开事项并指向新版本证据；
- 所有状态变化写审计事件。
"""
from __future__ import annotations

from dataclasses import dataclass

from .evidence import utcnow
from .matching import evaluate_notice
from .storage import Database

ACTION_VALUES = ("stop", "inspect", "upgrade", "no-action")
ACTION_LABELS = {
    "stop": "停用",
    "inspect": "检查",
    "upgrade": "升级",
    "no-action": "不动作",
}


@dataclass
class SyncReport:
    opened: list[str]
    reopened: list[str]
    closed: list[str]
    cancelled_notifications: list[str]
    cancelled_work_orders: list[str]
    pending_verification: list[str]


def sync_cases(
    db: Database, notice_id: str, actor: str = "workflow"
) -> SyncReport:
    """重算通告适用性并把风险事项同步到最新版本结论。"""
    evaluate_notice(db, notice_id, actor)
    notice = db.query_one(
        "SELECT * FROM notices WHERE notice_id=?", (notice_id,)
    )
    revision_id = notice["current_revision_id"]
    now = utcnow()
    report = SyncReport([], [], [], [], [], [])

    rows = db.query(
        "SELECT a.asset_id, e.result, e.reason_text, e.eval_id "
        "FROM assets a JOIN applicability_evals e ON e.asset_id=a.asset_id "
        "WHERE e.notice_id=? AND e.eval_id=(SELECT MAX(eval_id) "
        "FROM applicability_evals WHERE asset_id=a.asset_id AND notice_id=?) "
        "ORDER BY a.asset_id",
        (notice_id, notice_id),
    )
    for row in rows:
        case = db.query_one(
            "SELECT * FROM risk_cases WHERE asset_id=? AND notice_id=?",
            (row["asset_id"], notice_id),
        )
        if notice["current_state"] == "withdrawn":
            # 撤回通告：任何仍开启的事项一律关闭，不新打开事项。
            if case is not None and case["status"] != "closed":
                close_reason = "通告已撤回"
                _close_case(db, case["case_id"], close_reason, now, actor)
                report.closed.append(case["case_id"])
                report.cancelled_notifications.extend(
                    _cancel_pending_notifications(db, case["case_id"], close_reason, now, actor)
                )
                report.cancelled_work_orders.extend(
                    _cancel_open_work_orders(db, case["case_id"], close_reason, now, actor)
                )
            continue
        if row["result"] == "applicable":
            if case is None:
                case_id = f"RC-{row['asset_id']}-{notice_id}"
                db.execute(
                    "INSERT INTO risk_cases(case_id, asset_id, notice_id, status, "
                    "open_reason, revision_id, created_at, updated_at) "
                    "VALUES(?,?,?, 'open', ?, ?, ?, ?)",
                    (
                        case_id,
                        row["asset_id"],
                        notice_id,
                        row["reason_text"],
                        revision_id,
                        now,
                        now,
                    ),
                )
                report.opened.append(case_id)
                db.audit(
                    actor, "case-opened", "risk_case", case_id,
                    {"asset_id": row["asset_id"], "notice_id": notice_id,
                     "revision_id": revision_id, "reason": row["reason_text"]},
                )
            elif case["status"] == "closed":
                db.execute(
                    "UPDATE risk_cases SET status='open', close_reason=NULL, "
                    "closed_at=NULL, revision_id=?, updated_at=?, "
                    "reopened_count=reopened_count+1 WHERE case_id=?",
                    (revision_id, now, case["case_id"]),
                )
                report.reopened.append(case["case_id"])
                db.audit(
                    actor, "case-reopened", "risk_case", case["case_id"],
                    {"new_revision_id": revision_id,
                     "previous_close_reason": case["close_reason"],
                     "reason": row["reason_text"]},
                )
            else:
                # 事项保持开启，但锚定到最新适用版本证据。
                db.execute(
                    "UPDATE risk_cases SET revision_id=?, updated_at=? WHERE case_id=?",
                    (revision_id, now, case["case_id"]),
                )
        elif row["result"] == "unverifiable":
            report.pending_verification.append(row["asset_id"])
            if case is not None and case["status"] != "closed":
                db.audit(
                    actor, "case-pending-verification", "risk_case",
                    case["case_id"], {"reason": row["reason_text"]},
                )
        else:  # not-applicable：撤回或范围缩窄
            if case is not None and case["status"] != "closed":
                close_reason = (
                    "通告已撤回"
                    if notice["current_state"] == "withdrawn"
                    else f"范围缩窄，最新版本不再适用：{row['reason_text']}"
                )
                _close_case(db, case["case_id"], close_reason, now, actor)
                report.closed.append(case["case_id"])
                report.cancelled_notifications.extend(
                    _cancel_pending_notifications(db, case["case_id"], close_reason, now, actor)
                )
                report.cancelled_work_orders.extend(
                    _cancel_open_work_orders(db, case["case_id"], close_reason, now, actor)
                )

    db.commit()
    return report


def _close_case(
    db: Database, case_id: str, reason: str, now: str, actor: str
) -> None:
    db.execute(
        "UPDATE risk_cases SET status='closed', close_reason=?, closed_at=?, "
        "updated_at=? WHERE case_id=?",
        (reason, now, now, case_id),
    )
    db.audit(
        actor, "case-closed", "risk_case", case_id, {"reason": reason}
    )


def _cancel_pending_notifications(
    db: Database, case_id: str, reason: str, now: str, actor: str
) -> list[str]:
    rows = db.query(
        "SELECT notification_id FROM notifications WHERE case_id=? AND status='pending'",
        (case_id,),
    )
    ids = [row["notification_id"] for row in rows]
    for notification_id in ids:
        db.execute(
            "UPDATE notifications SET status='cancelled', cancelled_at=?, "
            "cancel_reason=? WHERE notification_id=?",
            (now, reason, notification_id),
        )
        db.audit(
            actor, "notification-cancelled", "notification", notification_id,
            {"case_id": case_id, "reason": reason},
        )
    return ids


def _cancel_open_work_orders(
    db: Database, case_id: str, reason: str, now: str, actor: str
) -> list[str]:
    rows = db.query(
        "SELECT work_order_id FROM work_orders WHERE case_id=? AND status IN "
        "('open','in-progress')",
        (case_id,),
    )
    ids = [row["work_order_id"] for row in rows]
    for work_order_id in ids:
        db.execute(
            "UPDATE work_orders SET status='cancelled', cancelled_at=?, "
            "cancel_reason=? WHERE work_order_id=?",
            (now, reason, work_order_id),
        )
        db.audit(
            actor, "work-order-cancelled", "work_order", work_order_id,
            {"case_id": case_id, "reason": reason},
        )
    return ids


def sync_all(db: Database, actor: str = "workflow") -> list[SyncReport]:
    return [
        sync_cases(db, row["notice_id"], actor)
        for row in db.query("SELECT notice_id FROM notices ORDER BY notice_id")
    ]


# -- 现场限制与工程师决策 ----------------------------------------------
def set_site_policy(
    db: Database,
    customer: str,
    site: str | None,
    allowed_actions: list[str],
    note: str = "",
    actor: str = "safety-engineer",
) -> None:
    for action in allowed_actions:
        if action not in ACTION_VALUES:
            raise ValueError(f"未知处置动作 {action}")
    now = utcnow()
    db.execute(
        "INSERT INTO site_policies(customer, site, allowed_actions, note, updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(customer, site) DO UPDATE SET "
        "allowed_actions=excluded.allowed_actions, note=excluded.note, "
        "updated_at=excluded.updated_at",
        (customer, site, ",".join(allowed_actions), note, now),
    )
    db.audit(
        actor, "site-policy-set", "site_policy", f"{customer}/{site or '*'}",
        {"allowed_actions": allowed_actions, "note": note},
    )
    db.commit()


def _policy_for(db: Database, customer: str, site: str | None):
    return (
        db.query_one(
            "SELECT * FROM site_policies WHERE customer=? AND site=?",
            (customer, site),
        )
        or db.query_one(
            "SELECT * FROM site_policies WHERE customer=? AND site IS NULL",
            (customer,),
        )
    )


def decide(
    db: Database,
    case_id: str,
    action: str,
    rationale: str,
    decided_by: str,
    actor: str | None = None,
    override_policy: bool = False,
) -> dict:
    """安全工程师对风险事项作出处置决策（网页命中不能绕过此步骤）。"""
    if action not in ACTION_VALUES:
        raise ValueError(f"未知处置动作 {action}")
    case = db.query_one("SELECT * FROM risk_cases WHERE case_id=?", (case_id,))
    if case is None:
        raise ValueError(f"风险事项 {case_id} 不存在")
    if case["status"] == "closed":
        raise ValueError(f"风险事项 {case_id} 已关闭，不能再决策")
    asset = db.query_one(
        "SELECT * FROM assets WHERE asset_id=?", (case["asset_id"],)
    )
    policy = _policy_for(db, asset["customer"], asset["site"])
    warning: str | None = None
    if policy is not None:
        allowed = [item for item in policy["allowed_actions"].split(",") if item]
        if action not in allowed and not override_policy:
            raise PermissionError(
                f"客户 {asset['customer']} 现场 {asset['site'] or '*'} 限制动作 "
                f"{allowed}，{ACTION_LABELS[action]} 不在允许范围；"
                "如确需执行请使用 override_policy"
            )
        if action not in allowed:
            warning = (
                f"动作超出客户现场限制 {allowed}，经 {decided_by} 显式覆盖"
            )
    now = utcnow()
    db.execute(
        "INSERT INTO engineer_decisions(case_id, revision_id, action, rationale, "
        "decided_by, decided_at, policy_warning) VALUES(?,?,?,?,?,?,?)",
        (case_id, case["revision_id"], action, rationale, decided_by, now, warning),
    )
    if action != "no-action":
        db.execute(
            "UPDATE risk_cases SET status='in-progress', updated_at=? WHERE case_id=?",
            (now, case_id),
        )
    db.audit(
        actor or decided_by, "engineer-decision", "risk_case", case_id,
        {"action": action, "rationale": rationale,
         "policy_warning": warning, "revision_id": case["revision_id"]},
    )
    db.commit()
    return {"case_id": case_id, "action": action, "policy_warning": warning}


def latest_decision(db: Database, case_id: str):
    return db.query_one(
        "SELECT * FROM engineer_decisions WHERE case_id=? "
        "ORDER BY decision_id DESC LIMIT 1",
        (case_id,),
    )
