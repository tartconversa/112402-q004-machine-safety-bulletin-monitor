"""适用范围匹配与风险事项同步测试（以仓库合成样例为基准）。"""
from _helpers import NOTICE_CSV_HEADER, DatabaseFixture, SAMPLES
from robotnotice.assets import import_ledger
from robotnotice.collector import collect_source, register_source
from robotnotice.console import applicability_history
from robotnotice.matching import evaluate_notice
from robotnotice.workflow import sync_cases


class MatchingFixture(DatabaseFixture):
    def setUp(self) -> None:
        super().setUp()
        register_source(
            self.db, "src-1", "样例", "file",
            str(SAMPLES / "notices.csv"), "csv_feed",
        )
        collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        import_ledger(
            self.db, (SAMPLES / "assets.csv").read_bytes(), "assets.csv"
        )


class SampleSemanticsTest(MatchingFixture):
    def test_当前版本只命中缩窄后的A2(self):
        summary = evaluate_notice(self.db, "N-7")
        self.assertEqual(summary.applicable, ["A2"])
        self.assertIn("A1", summary.not_applicable)
        self.assertIn("A3", summary.not_applicable)

    def test_旧版本只按型号匹配会误伤_A1的历史被保留为不适用(self):
        # 当前 rev2：A1(115,2.0) 序列号在 120-160 之外
        history = applicability_history(self.db, "A1", "N-7")
        # 评估前无历史
        self.assertEqual(history, [])
        evaluate_notice(self.db, "N-7")
        history = applicability_history(self.db, "A1", "N-7")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["revision_no"], "2")
        self.assertEqual(history[0]["result"], "not-applicable")
        self.assertEqual(history[0]["reason_code"], "serial-out-of-range")

    def test_撤回通告不打开新事项(self):
        report = sync_cases(self.db, "N-8")
        self.assertEqual(report.opened, [])
        case = self.db.query_one(
            "SELECT * FROM risk_cases WHERE notice_id='N-8'"
        )
        self.assertIsNone(case)

    def test_同步只打开适用设备的事项(self):
        report = sync_cases(self.db, "N-7")
        self.assertEqual(report.opened, ["RC-A2-N-7"])
        case = self.db.query_one("SELECT * FROM risk_cases")
        self.assertEqual(case["status"], "open")


class ScopeNarrowingTest(MatchingFixture):
    def _rewrite_feed(self, rows: str) -> None:
        path = SAMPLES / "notices.csv"
        # 复制到临时目录改写，避免触碰仓库样例
        target = self.base / "notices_copy.csv"
        target.write_text(NOTICE_CSV_HEADER + rows, encoding="utf-8")
        self.db.execute(
            "UPDATE sources SET url=? WHERE source_id='src-1'", (str(target),)
        )
        self.db.commit()

    def test_范围扩大后已关闭事项重开并指向新证据(self):
        # 初始只使用 rev1 宽范围，让 A1 先进入事项并被解除关闭
        self._rewrite_feed(
            "N-7,Maker-A,RB-2,100,180,2.x,1,active\n"
        )
        collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        sync_cases(self.db, "N-7")
        self.assertEqual(
            self.db.query_one(
                "SELECT status FROM risk_cases WHERE case_id='RC-A1-N-7'"
            )["status"],
            "open",
        )
        # 模拟工程师解除关闭
        from robotnotice.workflow import decide
        from robotnotice.dispatch import (
            create_work_order, submit_inspection, review_release,
        )

        decide(self.db, "RC-A1-N-7", "inspect", "现场核验", "eng")
        wo = create_work_order(self.db, "RC-A1-N-7")
        submit_inspection(self.db, wo, "tech", "无异常", "2.0")
        review_release(self.db, "RC-A1-N-7", "eng", True, "通过")
        self.assertEqual(
            self.db.query_one(
                "SELECT status FROM risk_cases WHERE case_id='RC-A1-N-7'"
            )["status"],
            "closed",
        )
        # 发布修订版本，范围再次覆盖 A1
        self._rewrite_feed(
            "N-7,Maker-A,RB-2,100,180,2.x,1,active\n"
            "N-7,Maker-A,RB-2,100,200,2.x,2,active\n"
        )
        collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        report = sync_cases(self.db, "N-7")
        self.assertEqual(report.reopened, ["RC-A1-N-7"])
        case = self.db.query_one(
            "SELECT * FROM risk_cases WHERE case_id='RC-A1-N-7'"
        )
        self.assertEqual(case["status"], "open")
        self.assertEqual(case["reopened_count"], 1)
        # 事项锚定到新修订版本证据
        revision_no = self.db.query_one(
            "SELECT revision_no FROM notice_revisions WHERE revision_id=?",
            (case["revision_id"],),
        )["revision_no"]
        self.assertEqual(revision_no, "2")

    def test_范围缩窄关闭事项并取消待执行通知与工单(self):
        self._rewrite_feed(
            "N-7,Maker-A,RB-2,100,180,2.x,1,active\n"
        )
        collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        sync_cases(self.db, "N-7")
        from robotnotice.workflow import decide
        from robotnotice.dispatch import (
            create_work_order, notify_factory,
        )

        case_id = "RC-A2-N-7"
        decide(self.db, case_id, "upgrade", "固件升级", "eng")
        notification = notify_factory(self.db, case_id, "portal", "请升级")
        work_order = create_work_order(
            self.db, case_id, notification_id=notification
        )
        # 缩窄到 A2 不再命中的区间
        self._rewrite_feed(
            "N-7,Maker-A,RB-2,200,300,2.x,2,active\n"
        )
        collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        report = sync_cases(self.db, "N-7")
        self.assertIn(case_id, report.closed)
        self.assertIn(notification, report.cancelled_notifications)
        self.assertIn(work_order, report.cancelled_work_orders)
        self.assertEqual(
            self.db.query_one(
                "SELECT status FROM notifications WHERE notification_id=?",
                (notification,),
            )["status"],
            "cancelled",
        )
        self.assertEqual(
            self.db.query_one(
                "SELECT status FROM work_orders WHERE work_order_id=?",
                (work_order,),
            )["status"],
            "cancelled",
        )

    def test_缺序列号设备产生待核且不自动开事项(self):
        # 临时插入一台缺序列号设备
        self.db.execute(
            "INSERT INTO assets(asset_id, customer, canonical_model, raw_model, "
            "serial, firmware, site, import_id, updated_at) "
            "VALUES('A9','F-12','RB-2','RB-2',NULL,'2.1','North',1,?)",
            ("2026-11-25T00:00:00+00:00",),
        )
        self.db.commit()
        report = sync_cases(self.db, "N-7")
        self.assertIn("A9", report.pending_verification)
        self.assertIsNone(
            self.db.query_one(
                "SELECT * FROM risk_cases WHERE asset_id='A9'"
            )
        )
        item = self.db.query_one(
            "SELECT * FROM verification_items WHERE asset_id='A9' "
            "AND kind='missing-serial'"
        )
        self.assertIsNotNone(item)


if __name__ == "__main__":
    import unittest

    unittest.main()
