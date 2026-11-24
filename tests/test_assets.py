"""台账导入、型号别名与待核项测试。"""
from _helpers import DatabaseFixture
from robotnotice.assets import add_alias, import_ledger, load_aliases, resolve_item

LEDGER_HEADER = "asset,customer,model,serial,firmware,site\n"


class LedgerImportTest(DatabaseFixture):
    def test_导入保留原始型号并写入归一号(self):
        data = (LEDGER_HEADER + "A1,F-11,rb_2,115,2.0,East\n").encode()
        report = import_ledger(self.db, data, "portal.csv")
        self.assertEqual(report.upserted, 1)
        asset = self.db.query_one("SELECT * FROM assets WHERE asset_id='A1'")
        self.assertEqual(asset["raw_model"], "rb_2")
        self.assertEqual(asset["canonical_model"], "RB-2")

    def test_别名把杂乱型号映射到通告型号(self):
        add_alias(self.db, "机械臂二号", "RB-2", "门户旧叫法")
        self.assertEqual(load_aliases(self.db), {"机械臂二号": "RB-2"})
        data = (LEDGER_HEADER + "A9,F-11,机械臂二号,115,2.0,East\n").encode()
        import_ledger(self.db, data, "portal.csv")
        asset = self.db.query_one("SELECT * FROM assets WHERE asset_id='A9'")
        self.assertEqual(asset["canonical_model"], "RB-2")

    def test_缺序列号生成待核项(self):
        data = (LEDGER_HEADER + "A2,F-11,RB-2,,2.1,West\n").encode()
        report = import_ledger(self.db, data, "portal.csv")
        self.assertGreaterEqual(report.verification_items, 1)
        item = self.db.query_one(
            "SELECT * FROM verification_items WHERE asset_id='A2' AND kind='missing-serial'"
        )
        self.assertIsNotNone(item)
        resolve_item(self.db, item["item_id"], "现场补录序列号 145")
        self.assertEqual(
            self.db.query_one(
                "SELECT status FROM verification_items WHERE item_id=?",
                (item["item_id"],),
            )["status"],
            "resolved",
        )

    def test_互相矛盾的台账记录生成冲突待核项(self):
        first = (LEDGER_HEADER + "A3,F-12,RC-4,901,1.8,North\n").encode()
        second = (LEDGER_HEADER + "A3,F-12,RC-4,902,1.8,North\n").encode()
        import_ledger(self.db, first, "portal.csv")
        report = import_ledger(self.db, second, "maintenance.csv")
        self.assertTrue(
            any("序列号冲突" in detail for detail in report.details)
        )
        item = self.db.query_one(
            "SELECT * FROM verification_items WHERE kind='conflicting-record'"
        )
        self.assertIsNotNone(item)
        # 后导入的值生效，但冲突证据保留
        self.assertEqual(
            self.db.query_one("SELECT serial FROM assets WHERE asset_id='A3'")["serial"],
            "902",
        )

    def test_重复导入相同矛盾不重复建待核项(self):
        first = (LEDGER_HEADER + "A3,F-12,RC-4,901,1.8,North\n").encode()
        second = (LEDGER_HEADER + "A3,F-12,RC-4,902,1.8,North\n").encode()
        import_ledger(self.db, first, "a.csv")
        import_ledger(self.db, second, "b.csv")
        # 再次导入与当前库值相同的记录，不产生新的冲突条目
        report = import_ledger(self.db, second, "c.csv")
        self.assertFalse(
            any("序列号冲突" in detail for detail in report.details)
        )
        count = self.db.query_one(
            "SELECT COUNT(*) c FROM verification_items "
            "WHERE asset_id='A3' AND kind='conflicting-record' AND status='open'"
        )["c"]
        self.assertEqual(count, 1)

    def test_未知型号生成待核项(self):
        # 先建一条通告范围，形成已知型号集合
        feed = self.write_feed(
            "n.csv",
            "notice,issuer,model,serial_from,serial_to,firmware,revision,state\n"
            "N-1,M,RB-2,1,9,2.x,1,active\n",
        )
        self.register_file_source(feed)
        from robotnotice.collector import collect_source

        collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        data = (LEDGER_HEADER + "A4,F-12,XX-9,1,1.0,South\n").encode()
        report = import_ledger(self.db, data, "portal.csv")
        self.assertTrue(any("未出现在任何通告" in d for d in report.details))


if __name__ == "__main__":
    import unittest

    unittest.main()
