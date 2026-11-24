"""适用范围匹配引擎。

对每台设备与每个通告的**当前版本**评估一次，并在 ``applicability_evals``
留下不可覆盖的历史评估；型号命中但缺序列号无法核验区间时，结论为
``unverifiable`` 并确保存在待核项。网页命中只产生评估结论，不产生维修指令。
"""
from __future__ import annotations

from dataclasses import dataclass

from .assets import _open_item
from .evidence import utcnow
from .storage import Database
from .versions import ScopeEntry, match_asset


@dataclass
class EvaluationSummary:
    notice_id: str
    revision_id: int
    applicable: list[str]
    not_applicable: list[str]
    unverifiable: list[str]


def _scope_for_revision(db: Database, revision_id: int) -> list[ScopeEntry]:
    return [
        ScopeEntry(
            model=row["model"],
            serial_from=row["serial_from"],
            serial_to=row["serial_to"],
            firmware=row["firmware"],
        )
        for row in db.query(
            "SELECT * FROM notice_scope WHERE revision_id=?", (revision_id,)
        )
    ]


def current_notices(db: Database):
    return db.query(
        "SELECT n.notice_id, n.current_state, n.current_revision_id, r.revision_no "
        "FROM notices n JOIN notice_revisions r ON r.revision_id=n.current_revision_id "
        "ORDER BY n.notice_id"
    )


def evaluate_notice(db: Database, notice_id: str, actor: str = "matcher") -> EvaluationSummary:
    """针对单个通告的当前版本重算全部设备的适用性。"""
    notice = db.query_one(
        "SELECT * FROM notices WHERE notice_id=?", (notice_id,)
    )
    if notice is None:
        raise ValueError(f"通告 {notice_id} 不存在")
    revision_id = notice["current_revision_id"]
    scope = _scope_for_revision(db, revision_id)
    now = utcnow()
    summary = EvaluationSummary(notice_id, revision_id, [], [], [])
    open_items = {
        (row["asset_id"], row["kind"])
        for row in db.query(
            "SELECT asset_id, kind FROM verification_items WHERE status='open'"
        )
    }

    for asset in db.query("SELECT * FROM assets ORDER BY asset_id"):
        result = match_asset(
            asset["canonical_model"],
            asset["serial"],
            asset["firmware"],
            scope,
        )
        db.execute(
            "INSERT INTO applicability_evals(asset_id, notice_id, revision_id, "
            "result, reason_code, reason_text, evaluated_at) VALUES(?,?,?,?,?,?,?)",
            (
                asset["asset_id"],
                notice_id,
                revision_id,
                result.result,
                result.reason_code,
                result.reason_text,
                now,
            ),
        )
        getattr(summary, {
            "applicable": "applicable",
            "not-applicable": "not_applicable",
            "unverifiable": "unverifiable",
        }[result.result]).append(asset["asset_id"])

        if result.result == "unverifiable":
            pair = (asset["asset_id"], "missing-serial")
            if pair not in open_items:
                _open_item(
                    db,
                    asset["asset_id"],
                    "missing-serial",
                    result.reason_text,
                    now,
                )
                open_items.add(pair)

    db.audit(
        actor, "applicability-evaluated", "notice", notice_id,
        {
            "revision_id": revision_id,
            "applicable": summary.applicable,
            "not_applicable": summary.not_applicable,
            "unverifiable": summary.unverifiable,
        },
    )
    db.commit()
    return summary


def evaluate_all(db: Database, actor: str = "matcher") -> list[EvaluationSummary]:
    return [
        evaluate_notice(db, row["notice_id"], actor)
        for row in current_notices(db)
    ]


def latest_eval(db: Database, asset_id: str, notice_id: str):
    return db.query_one(
        "SELECT e.*, r.revision_no FROM applicability_evals e "
        "JOIN notice_revisions r ON r.revision_id=e.revision_id "
        "WHERE e.asset_id=? AND e.notice_id=? ORDER BY e.eval_id DESC LIMIT 1",
        (asset_id, notice_id),
    )
