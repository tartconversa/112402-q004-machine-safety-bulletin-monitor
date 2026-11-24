"""工程师决策、现场限制、调度、检修解除与历史保留测试。"""
from _helpers import NOTICE_CSV_HEADER, DatabaseFixture
from robotnotice.assets import import_ledger
from robotnotice.collector import collect_source
from robotnotice.console import case_timeline, pending_work
from robotnotice.dispatch import (
    create_work_order,
    mark_notification_sent,
    notify_factory,
    review_release,
    start_work_order,
    submit_inspection,
)
from robotnotice.workflow import decide, set_site_policy, sync_cases


class WorkflowFixture(DatabaseFixture):
    def setUp(self) -> None:
        super().setUp()
        feed = self.write_feed(
            "feed.csv",
            NOTICE_CSV_HEADER
            + "N-7,Maker-A,RB-2,120,160,2.1,2,active\n",
        )
        self.register_file_source(feed)
        collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        ledger = (
            "asset,customer,model,serial,firmware,site\n"
            "A2,F-11,RB-2,145,2.1,West\n"
        ).encode()
        import_ledger(self.db, ledger, "assets.csv")
        sync_cases(self.db, "N-7")
        self.case_id = "RC-A2-N-7"


class DecisionGateTest(WorkflowFixture):
    def test_无工程师决策不能生成工作单(self):
        with self.assertRaises(PermissionError):
            create_work_order(self.db, self.case_id)

    def test_现场限制拦截越权动作_允许覆盖并留痕(self):
        set_site_policy(self.db, "F-11", "West", ["inspect"], "产线不可停")
        with self.assertRaises(PermissionError):
            decide(self.db, self.case_id, "stop", "要求停用", "eng")
        result = decide(
            self.db, self.case_id, "stop", "紧急风险", "eng",
            override_policy=True,
        )
        self.assertIn("超出客户现场限制", result["policy_warning"])
        row = self.db.query_one(
            "SELECT policy_warning FROM engineer_decisions ORDER BY decision_id DESC"
        )
        self.assertIsNotNone(row["policy_warning"])

    def test_决策为不动作时不下工单(self):
        decide(self.db, self.case_id, "no-action", "评估后风险可接受", "eng")
        with self.assertRaises(ValueError):
            create_work_order(self.db, self.case_id)
        # 不动作不改变事项开启状态
        self.assertEqual(
            self.db.query_one(
                "SELECT status FROM risk_cases WHERE case_id=?",
                (self.case_id,),
            )["status"],
            "open",
        )


class DispatchLifecycleTest(WorkflowFixture):
    def _decide_and_notify(self, action: str = "upgrade"):
        decide(self.db, self.case_id, action, f"需要{action}", "eng")
        notification_id = notify_factory(
            self.db, self.case_id, "portal", "请安排处置"
        )
        return notification_id

    def test_通知发送与工单全流程(self):
        notification_id = self._decide_and_notify()
        mark_notification_sent(self.db, notification_id)
        work_order_id = create_work_order(
            self.db, self.case_id, notification_id=notification_id
        )
        start_work_order(self.db, work_order_id)
        inspection_id = submit_inspection(
            self.db, work_order_id, "tech-li",
            "已升级到 2.2 并复测通过", observed_firmware="2.2",
            evidence_sha="abc123",
        )
        # 无评审前事项仍在进行中
        self.assertEqual(
            self.db.query_one(
                "SELECT status FROM risk_cases WHERE case_id=?",
                (self.case_id,),
            )["status"],
            "in-progress",
        )
        review_id = review_release(
            self.db, self.case_id, "eng", True, "升级证据满足解除条件"
        )
        self.assertTrue(review_id)
        self.assertEqual(
            self.db.query_one(
                "SELECT status, close_reason FROM risk_cases WHERE case_id=?",
                (self.case_id,),
            )["status"],
            "closed",
        )
        # 待办视图不再包含该工单
        pending = pending_work(self.db)
        self.assertNotIn(
            work_order_id,
            [row["work_order_id"] for row in pending["open_work_orders"]],
        )
        # 时间线包含全部流
        streams = {item["stream"] for item in case_timeline(self.db, self.case_id)}
        self.assertEqual(
            streams,
            {
                "audit", "decision", "notification",
                "work_order", "inspection", "release_review",
            },
        )

    def test_解除被拒绝时事项保持开启(self):
        notification_id = self._decide_and_notify("inspect")
        work_order_id = create_work_order(
            self.db, self.case_id, notification_id=notification_id
        )
        submit_inspection(self.db, work_order_id, "tech", "未见异常但固件未升级")
        review_release(self.db, self.case_id, "eng", False, "固件仍为受影响版本")
        self.assertEqual(
            self.db.query_one(
                "SELECT status FROM risk_cases WHERE case_id=?",
                (self.case_id,),
            )["status"],
            "in-progress",
        )

    def test_取消的通知不能作为派单依据(self):
        notification_id = self._decide_and_notify()
        self.db.execute(
            "UPDATE notifications SET status='cancelled', cancelled_at=?, "
            "cancel_reason='范围缩窄' WHERE notification_id=?",
            ("2026-11-25T00:00:00+00:00", notification_id),
        )
        self.db.commit()
        with self.assertRaises(ValueError):
            create_work_order(
                self.db, self.case_id, notification_id=notification_id
            )

    def test_撤回后已完成检修按原版本保留(self):
        notification_id = self._decide_and_notify("inspect")
        work_order_id = create_work_order(
            self.db, self.case_id, notification_id=notification_id
        )
        submit_inspection(
            self.db, work_order_id, "tech", "检查完成", observed_firmware="2.1"
        )
        anchored_revision = self.db.query_one(
            "SELECT revision_id FROM work_orders WHERE work_order_id=?",
            (work_order_id,),
        )["revision_id"]

        feed = self.base / "feed.csv"
        feed.write_text(
            NOTICE_CSV_HEADER
            + "N-7,Maker-A,RB-2,120,160,2.1,2,withdrawn\n",
            encoding="utf-8",
        )
        collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        sync_cases(self.db, "N-7")

        inspection = self.db.query_one(
            "SELECT * FROM inspections WHERE work_order_id=?", (work_order_id,)
        )
        self.assertIsNotNone(inspection)
        self.assertEqual(inspection["findings"], "检查完成")
        # 工作单仍锚定撤回前的通告版本，不被回写
        self.assertEqual(
            self.db.query_one(
                "SELECT revision_id FROM work_orders WHERE work_order_id=?",
                (work_order_id,),
            )["revision_id"],
            anchored_revision,
        )
        case = self.db.query_one(
            "SELECT status, close_reason FROM risk_cases WHERE case_id=?",
            (self.case_id,),
        )
        self.assertEqual(case["status"], "closed")
        self.assertIn("撤回", case["close_reason"])

    def test_撤回只取消未执行通知_已发送通知保留(self):
        decide(self.db, self.case_id, "inspect", "需要检查", "eng")
        pending = notify_factory(self.db, self.case_id, "portal", "待发送")
        sent = notify_factory(self.db, self.case_id, "email", "已发送")
        mark_notification_sent(self.db, sent)

        feed = self.base / "feed.csv"
        feed.write_text(
            NOTICE_CSV_HEADER
            + "N-7,Maker-A,RB-2,120,160,2.1,2,withdrawn\n",
            encoding="utf-8",
        )
        collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        report = sync_cases(self.db, "N-7")
        self.assertEqual(report.cancelled_notifications, [pending])
        self.assertEqual(
            self.db.query_one(
                "SELECT status FROM notifications WHERE notification_id=?",
                (pending,),
            )["status"],
            "cancelled",
        )
        self.assertEqual(
            self.db.query_one(
                "SELECT status FROM notifications WHERE notification_id=?",
                (sent,),
            )["status"],
            "sent",
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
