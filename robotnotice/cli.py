"""命令行入口：采集、台账、匹配、处置、调度与查询全流程。

用法示例见仓库 README。所有需要写库的子命令都记录审计事件，
``--json`` 可输出机器可读结果。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .assets import add_alias, import_ledger, resolve_item
from .collector import collect_all, collect_source, register_source
from .console import (
    applicability_history,
    case_status,
    case_timeline,
    evidence_trail,
    notice_overview,
    pending_work,
    verification_queue,
)
from .dispatch import (
    create_work_order,
    mark_notification_sent,
    notify_factory,
    review_release,
    start_work_order,
    submit_inspection,
)
from .evidence import EvidenceStore
from .matching import evaluate_all, evaluate_notice
from .storage import Database
from .workflow import decide, set_site_policy, sync_all, sync_cases

DEFAULT_DB = "data/robotnotice.db"
DEFAULT_EVIDENCE = "data/evidence"


def _open(args: argparse.Namespace) -> tuple[Database, EvidenceStore]:
    db = Database(args.db)
    db.initialize()
    store = EvidenceStore(args.evidence)
    return db, store


def _emit(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    elif isinstance(payload, list):
        if not payload:
            print("（无记录）")
        for row in payload:
            print(_format_row(row))
    else:
        print(_format_row(payload))


def _format_row(row: dict) -> str:
    return " | ".join(f"{key}={value}" for key, value in row.items())


# -- 采集 ----------------------------------------------------------------
def cmd_source_add(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    register_source(db, args.source_id, args.name, args.kind, args.url,
                    args.parser, args.actor)
    _emit({"registered": args.source_id}, args.json)


def cmd_collect(args: argparse.Namespace) -> None:
    db, store = _open(args)
    if args.source_id:
        reports = [collect_source(db, store, args.source_id, args.actor,
                                  not args.no_attachments)]
    else:
        reports = collect_all(db, args.actor)
    payload = [
        {
            "source_id": report.source_id,
            "ok": report.ok,
            "fetched": report.fetched,
            "changes": report.change_kinds,
            "attachment_events": report.attachment_events,
            "error": report.error,
        }
        for report in reports
    ]
    _emit(payload, args.json)


# -- 台账 ----------------------------------------------------------------
def cmd_alias(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    add_alias(db, args.alias, args.canonical, args.note or "", args.actor)
    _emit({"alias": args.alias, "canonical": args.canonical}, args.json)


def cmd_ledger_import(args: argparse.Namespace) -> None:
    db, store = _open(args)
    path = Path(args.file)
    data = path.read_bytes()
    record = store.put(data, "text/csv")
    report = import_ledger(db, data, path.name, record.sha256, args.actor)
    _emit(
        {
            "import_id": report.import_id,
            "upserted": report.upserted,
            "open_verification_items": report.verification_items,
            "details": report.details,
            "evidence_sha256": record.sha256,
        },
        args.json,
    )


def cmd_verification(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    _emit(verification_queue(db, args.status), args.json)


def cmd_verification_resolve(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    resolve_item(db, args.item_id, args.resolution, args.actor)
    _emit({"resolved": args.item_id}, args.json)


# -- 匹配与工作流 ---------------------------------------------------------
def cmd_evaluate(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    if args.notice:
        summaries = [evaluate_notice(db, args.notice, args.actor)]
    else:
        summaries = evaluate_all(db, args.actor)
    _emit(
        [
            {
                "notice_id": summary.notice_id,
                "revision_id": summary.revision_id,
                "applicable": summary.applicable,
                "not_applicable": summary.not_applicable,
                "unverifiable": summary.unverifiable,
            }
            for summary in summaries
        ],
        args.json,
    )


def cmd_sync(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    reports = (
        [sync_cases(db, args.notice, args.actor)]
        if args.notice
        else sync_all(db, args.actor)
    )
    _emit(
        [
            {
                "opened": report.opened,
                "reopened": report.reopened,
                "closed": report.closed,
                "cancelled_notifications": report.cancelled_notifications,
                "cancelled_work_orders": report.cancelled_work_orders,
                "pending_verification": report.pending_verification,
            }
            for report in reports
        ],
        args.json,
    )


def cmd_policy(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    set_site_policy(db, args.customer, args.site, args.allow.split(","),
                    args.note or "", args.actor)
    _emit({"policy": f"{args.customer}/{args.site or '*'}"}, args.json)


def cmd_decide(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    payload = decide(db, args.case_id, args.action, args.rationale,
                     args.actor, override_policy=args.override)
    _emit(payload, args.json)


# -- 调度与检修 -----------------------------------------------------------
def cmd_notify(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    notification_id = notify_factory(db, args.case_id, args.channel,
                                     args.message, args.actor)
    _emit({"notification_id": notification_id}, args.json)


def cmd_notify_send(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    mark_notification_sent(db, args.notification_id, args.actor)
    _emit({"sent": args.notification_id}, args.json)


def cmd_work_order(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    work_order_id = create_work_order(db, args.case_id, args.actor,
                                      args.notification_id)
    _emit({"work_order_id": work_order_id}, args.json)


def cmd_work_start(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    start_work_order(db, args.work_order_id, args.actor)
    _emit({"in_progress": args.work_order_id}, args.json)


def cmd_inspect_submit(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    inspection_id = submit_inspection(
        db,
        args.work_order_id,
        args.actor,
        args.findings,
        args.observed_firmware,
        args.evidence_sha,
    )
    _emit({"inspection_id": inspection_id}, args.json)


def cmd_release(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    review_id = review_release(db, args.case_id, args.actor,
                               not args.reject, args.rationale)
    _emit({"review_id": review_id, "approved": not args.reject}, args.json)


# -- 查询 ----------------------------------------------------------------
def cmd_status(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    _emit(case_status(db, args.customer, args.asset, args.notice), args.json)


def cmd_pending(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    _emit(pending_work(db, args.customer), args.json)


def cmd_history(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    _emit(applicability_history(db, args.asset, args.notice), args.json)


def cmd_timeline(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    _emit(case_timeline(db, args.case_id), args.json)


def cmd_notices(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    _emit(notice_overview(db), args.json)


def cmd_evidence(args: argparse.Namespace) -> None:
    db, _ = _open(args)
    _emit(evidence_trail(db, args.notice), args.json)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="robotnotice",
        description="设备安全通告采集与适用批次处置系统",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite 数据库路径")
    parser.add_argument("--evidence", default=DEFAULT_EVIDENCE,
                        help="原始证据目录")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--actor", default="cli", help="审计事件中的操作人")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("source-add", help="注册公告页面/接口源")
    p.add_argument("source_id")
    p.add_argument("name")
    p.add_argument("--kind", choices=["http", "file"], required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--parser", choices=["csv_feed", "json_feed", "html_page"],
                   required=True)
    p.set_defaults(func=cmd_source_add)

    p = sub.add_parser("collect", help="轮询源并识别新增/更正/撤回/附件替换")
    p.add_argument("source_id", nargs="?")
    p.add_argument("--no-attachments", action="store_true")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("alias", help="维护型号别名")
    p.add_argument("alias")
    p.add_argument("canonical")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_alias)

    p = sub.add_parser("ledger-import", help="导入设备台账 CSV")
    p.add_argument("file")
    p.set_defaults(func=cmd_ledger_import)

    p = sub.add_parser("verification", help="查看待核项队列")
    p.add_argument("--status", default="open",
                   choices=["open", "resolved", "ignored"])
    p.set_defaults(func=cmd_verification)

    p = sub.add_parser("verification-resolve", help="处理待核项")
    p.add_argument("item_id", type=int)
    p.add_argument("resolution")
    p.set_defaults(func=cmd_verification_resolve)

    p = sub.add_parser("evaluate", help="评估通告适用范围")
    p.add_argument("--notice")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("sync", help="同步风险事项（打开/重开/关闭/取消）")
    p.add_argument("--notice")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("policy", help="设置客户现场动作限制")
    p.add_argument("customer")
    p.add_argument("--site")
    p.add_argument("--allow", required=True,
                   help="逗号分隔，如 stop,inspect,upgrade")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_policy)

    p = sub.add_parser("decide", help="安全工程师处置决策")
    p.add_argument("case_id")
    p.add_argument("--action", required=True,
                   choices=["stop", "inspect", "upgrade", "no-action"])
    p.add_argument("--rationale", required=True)
    p.add_argument("--override", dest="override", action="store_true",
                   help="显式覆盖客户现场限制")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("notify", help="创建工厂通知（待发送）")
    p.add_argument("case_id")
    p.add_argument("--channel", default="portal")
    p.add_argument("--message", required=True)
    p.set_defaults(func=cmd_notify)

    p = sub.add_parser("notify-send", help="标记通知已发送")
    p.add_argument("notification_id")
    p.set_defaults(func=cmd_notify_send)

    p = sub.add_parser("work-order", help="依据工程师决策生成工作单")
    p.add_argument("case_id")
    p.add_argument("--notification-id", dest="notification_id")
    p.set_defaults(func=cmd_work_order)

    p = sub.add_parser("work-start", help="开始执行工作单")
    p.add_argument("work_order_id")
    p.set_defaults(func=cmd_work_start)

    p = sub.add_parser("inspect-submit", help="提交检修回执")
    p.add_argument("work_order_id")
    p.add_argument("--observed-firmware", dest="observed_firmware")
    p.add_argument("--findings", required=True)
    p.add_argument("--evidence-sha", dest="evidence_sha")
    p.set_defaults(func=cmd_inspect_submit)

    p = sub.add_parser("release", help="安全工程师解除评审")
    p.add_argument("case_id")
    p.add_argument("--rationale", required=True)
    p.add_argument("--reject", action="store_true")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("status", help="按客户/设备/通告查当前状态")
    p.add_argument("--customer")
    p.add_argument("--asset")
    p.add_argument("--notice")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("pending", help="查待执行工作与待发送通知")
    p.add_argument("--customer")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("history", help="查设备对通告的历次适用结论")
    p.add_argument("--asset", required=True)
    p.add_argument("--notice", required=True)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("timeline", help="查风险事项全量历史")
    p.add_argument("case_id")
    p.set_defaults(func=cmd_timeline)

    p = sub.add_parser("notices", help="通告总览")
    p.set_defaults(func=cmd_notices)

    p = sub.add_parser("evidence", help="通告版本证据链与附件替换记录")
    p.add_argument("--notice", required=True)
    p.set_defaults(func=cmd_evidence)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (ValueError, PermissionError, FileNotFoundError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
