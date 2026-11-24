"""CLI 端到端测试：用独立数据目录走完整条处置链路。"""
import json
import tempfile
import unittest
from pathlib import Path

from robotnotice.cli import main

NOTICE_CSV = (
    "notice,issuer,model,serial_from,serial_to,firmware,revision,state\n"
    "N-7,Maker-A,RB-2,120,160,2.1,2,active\n"
)
ASSET_CSV = (
    "asset,customer,model,serial,firmware,site\n"
    "A2,F-11,RB-2,145,2.1,West\n"
)


class CliEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.feed = self.base / "feed.csv"
        self.feed.write_text(NOTICE_CSV, encoding="utf-8")
        self.ledger = self.base / "assets.csv"
        self.ledger.write_text(ASSET_CSV, encoding="utf-8")
        self.db_path = self.base / "data.db"
        self.evidence_path = self.base / "evidence"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, *arguments: str) -> str:
        import contextlib
        import io

        buf = io.StringIO()
        argv = [
            "--db", str(self.db_path),
            "--evidence", str(self.evidence_path),
            "--json",
            *arguments,
        ]
        with contextlib.redirect_stdout(buf):
            code = main(argv)
        self.assertEqual(code, 0, buf.getvalue())
        return buf.getvalue()

    def test_全链路命令(self):
        self.run_cli(
            "source-add", "s1", "厂商页", "--kind", "file",
            "--url", str(self.feed), "--parser", "csv_feed",
        )
        collect = json.loads(self.run_cli("collect", "s1", "--no-attachments"))
        self.assertEqual(collect[0]["changes"], ["new"])
        self.run_cli("ledger-import", str(self.ledger))
        sync = json.loads(self.run_cli("sync", "--notice", "N-7"))
        self.assertEqual(sync[0]["opened"], ["RC-A2-N-7"])

        self.run_cli(
            "policy", "F-11", "--site", "West",
            "--allow", "inspect,upgrade",
        )
        decide = json.loads(self.run_cli(
            "decide", "RC-A2-N-7", "--action", "upgrade",
            "--rationale", "固件需升级",
        ))
        self.assertIsNone(decide["policy_warning"])

        notify = json.loads(self.run_cli(
            "notify", "RC-A2-N-7", "--message", "请安排升级",
        ))
        notification_id = notify["notification_id"]
        self.run_cli("notify-send", notification_id)
        order = json.loads(self.run_cli(
            "work-order", "RC-A2-N-7",
            "--notification-id", notification_id,
        ))
        work_order_id = order["work_order_id"]
        self.run_cli("work-start", work_order_id)
        self.run_cli(
            "inspect-submit", work_order_id,
            "--observed-firmware", "2.2", "--findings", "升级完成",
        )
        self.run_cli(
            "release", "RC-A2-N-7", "--rationale", "满足解除条件",
        )
        status = json.loads(self.run_cli("status", "--notice", "N-7"))
        self.assertEqual(status[0]["status"], "closed")
        pending = json.loads(self.run_cli("pending"))
        self.assertEqual(pending["open_work_orders"], [])
        notices = json.loads(self.run_cli("notices"))
        self.assertEqual(notices[0]["notice_id"], "N-7")
        evidence = json.loads(self.run_cli("evidence", "--notice", "N-7"))
        self.assertEqual(len(evidence), 1)
        self.assertEqual(len(evidence[0]["scope"]), 1)
        self.assertEqual(evidence[0]["scope"][0]["serial_from"], "120")
        self.assertRegex(evidence[0]["content_sha"], "^[0-9a-f]{64}$")
        timeline = json.loads(self.run_cli("timeline", "RC-A2-N-7"))
        self.assertGreaterEqual(len(timeline), 6)

    def test_错误命令返回非零退出码(self):
        import contextlib
        import io

        self.assertEqual(main(["--db", str(self.db_path),
                               "--evidence", str(self.evidence_path),
                               "status"]), 0)
        with contextlib.redirect_stderr(io.StringIO()):
            code = main([
                "--db", str(self.db_path),
                "--evidence", str(self.evidence_path),
                "decide", "RC-NOPE-N-1", "--action", "inspect",
                "--rationale", "x",
            ])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
