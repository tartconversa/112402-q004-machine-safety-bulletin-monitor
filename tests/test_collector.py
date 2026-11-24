"""采集器：差异识别、撤回、静默更正、附件替换与条件请求。"""
import http.server
import json
import threading
from pathlib import Path

from _helpers import NOTICE_CSV_HEADER, DatabaseFixture, SAMPLES
from robotnotice.collector import collect_source
from robotnotice.sources import CsvFeedParser, JsonFeedParser, PageParser


class CsvFeedParseTest(DatabaseFixture):
    def test_样例通告解析为两个通告三个版本(self):
        records = CsvFeedParser().parse(
            (SAMPLES / "notices.csv").read_bytes()
        )
        keys = {(r.notice_id, r.revision_no, r.state) for r in records}
        self.assertIn(("N-7", "1", "active"), keys)
        self.assertIn(("N-7", "2", "active"), keys)
        self.assertIn(("N-8", "1", "withdrawn"), keys)
        n7_rev2 = next(r for r in records if r.notice_id == "N-7" and r.revision_no == "2")
        self.assertEqual(n7_rev2.scope[0].serial_from, "120")


class CollectDiffTest(DatabaseFixture):
    def _feed(self, rows: str) -> Path:
        return self.write_feed("feed.csv", NOTICE_CSV_HEADER + rows)

    def test_新增_新版本_撤回_再生效的差异序列(self):
        feed = self._feed("N-1,Maker,RB-2,100,180,2.x,1,active\n")
        self.register_file_source(feed)
        first = collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        self.assertEqual(first.change_kinds, ["new"])

        feed.write_text(
            NOTICE_CSV_HEADER
            + "N-1,Maker,RB-2,100,160,2.x,2,active\n",
            encoding="utf-8",
        )
        second = collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        self.assertEqual(second.change_kinds, ["new-revision"])

        feed.write_text(
            NOTICE_CSV_HEADER
            + "N-1,Maker,RB-2,100,160,2.x,2,withdrawn\n",
            encoding="utf-8",
        )
        third = collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        self.assertEqual(third.change_kinds, ["withdrawn"])
        self.assertEqual(
            self.db.query_one(
                "SELECT current_state FROM notices"
            )["current_state"],
            "withdrawn",
        )

        feed.write_text(
            NOTICE_CSV_HEADER
            + "N-1,Maker,RB-2,100,160,2.x,2,active\n",
            encoding="utf-8",
        )
        fourth = collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        self.assertEqual(fourth.change_kinds, ["reactivated"])

    def test_同版本号静默更正产生revision_corrected(self):
        feed = self._feed("N-1,Maker,RB-2,100,180,2.x,1,active\n")
        self.register_file_source(feed)
        collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        feed.write_text(
            NOTICE_CSV_HEADER
            + "N-1,Maker,RB-2,100,190,2.x,1,active\n",
            encoding="utf-8",
        )
        report = collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        self.assertEqual(report.change_kinds, ["revision-corrected"])
        # 两个版本行都保留，各自指向不同原始证据哈希
        rows = self.db.query(
            "SELECT DISTINCT content_sha FROM notice_revisions WHERE notice_id='N-1'"
        )
        self.assertEqual(len(rows), 2)

    def test_重复抓取内容不变且证据复用(self):
        feed = self._feed("N-1,Maker,RB-2,100,180,2.x,1,active\n")
        self.register_file_source(feed)
        collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        again = collect_source(self.db, self.store, "src-1", fetch_attachments=False)
        self.assertEqual(again.change_kinds, ["unchanged"])
        self.assertEqual(len(self.store.all_hashes()), 1)

    def test_抓取失败被记录而不是抛出(self):
        missing = self.base / "nope.csv"
        self.register_file_source(missing)
        report = collect_source(self.db, self.store, "src-1")
        self.assertFalse(report.ok)
        self.assertIn("文件不存在", report.error)
        run = self.db.query_one("SELECT * FROM fetch_runs")
        self.assertEqual(run["ok"], 0)


class AttachmentReplacementTest(DatabaseFixture):
    HTML = (
        "<html><body>"
        '<div data-notice="N-9" data-issuer="Maker-A" data-revision="1" '
        'data-state="active" data-model="RB-2" data-serial-from="100" '
        'data-serial-to="180" data-firmware="2.x">'
        '<a href="bulletin.pdf">公告附件</a></div></body></html>'
    )

    def test_页面解析与附件替换检测(self):
        page = self.write_feed("page.html", self.HTML)
        pdf_v1 = self.write_bytes("bulletin.pdf", b"PDF-VERSION-1")
        self.register_file_source(page, parser="html_page")
        report = collect_source(self.db, self.store, "src-1")
        self.assertTrue(report.ok)
        self.assertEqual(report.attachment_events, ["added:N-9:bulletin.pdf"])

        # 附件内容替换，页面其余不变
        self.write_bytes("bulletin.pdf", b"PDF-VERSION-2-CHANGED")
        report = collect_source(self.db, self.store, "src-1")
        self.assertEqual(
            report.attachment_events, ["replaced:N-9:bulletin.pdf"]
        )
        attachments = self.db.query(
            "SELECT content_sha, replaced_attachment_id FROM attachments "
            "ORDER BY attachment_id"
        )
        self.assertEqual(len(attachments), 2)
        self.assertIsNotNone(attachments[1]["replaced_attachment_id"])
        # 旧附件证据仍然保留
        self.assertTrue(self.store.exists(attachments[0]["content_sha"]))

    def test_页面解析器提取范围条目(self):
        records = PageParser().parse(self.HTML.encode("utf-8"))
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.notice_id, "N-9")
        self.assertEqual(record.scope[0].model, "RB-2")
        self.assertEqual(record.attachments[0].rel_url, "bulletin.pdf")

    def test_附件抓取失败不影响通告正文落库(self):
        html = (
            '<html><div data-notice="N-1" data-revision="1" data-state="active" '
            'data-model="RB-2"><a href="missing.pdf">x</a></div></html>'
        )
        page = self.write_feed("broken.html", html)
        self.register_file_source(page, parser="html_page")
        report = collect_source(self.db, self.store, "src-1")
        self.assertTrue(report.ok)
        self.assertEqual(
            report.attachment_events, ["failed:N-1:missing.pdf"]
        )
        self.assertEqual(
            self.db.query_one("SELECT notice_id FROM notices")["notice_id"],
            "N-1",
        )
        event = self.db.query_one(
            "SELECT action FROM audit_events WHERE action='attachment-fetch-failed'"
        )
        self.assertIsNotNone(event)

    def test_页面可承载多个通告条目(self):
        html = (
            "<html><body>"
            '<div data-notice="N-1" data-revision="1" data-state="active" '
            'data-model="RB-2" data-serial-from="1" data-serial-to="9">'
            '<a href="n1.pdf">a</a></div>'
            '<div data-notice="N-2" data-revision="1" data-state="withdrawn" '
            'data-model="RC-4" data-firmware="1.8">'
            '<a href="n2.pdf">b</a></div>'
            "</body></html>"
        )
        records = PageParser().parse(html.encode("utf-8"))
        self.assertEqual({r.notice_id for r in records}, {"N-1", "N-2"})
        n2 = next(r for r in records if r.notice_id == "N-2")
        self.assertEqual(n2.state, "withdrawn")
        self.assertEqual(n2.scope[0].firmware, "1.8")


class JsonFeedTest(DatabaseFixture):
    def test_json接口多范围多附件解析(self):
        payload = {
            "notices": [
                {
                    "notice_id": "N-10",
                    "issuer": "Maker-B",
                    "title": "控制柜电池通告",
                    "revisions": [
                        {
                            "revision": 3,
                            "state": "active",
                            "scope": [
                                {"model": "rc4", "firmware": "1.8"},
                                {"model": "RC-4", "serial_from": "1",
                                 "serial_to": "9", "firmware": "*"},
                            ],
                            "attachments": [{"rel_url": "a.pdf"}],
                        }
                    ],
                }
            ]
        }
        records = JsonFeedParser().parse(
            json.dumps(payload).encode("utf-8")
        )
        self.assertEqual(records[0].notice_id, "N-10")
        self.assertEqual(records[0].revision_no, "3")
        self.assertEqual({entry.model for entry in records[0].scope}, {"RC-4"})
        self.assertEqual(records[0].attachments[0].rel_url, "a.pdf")


class HttpConditionalFetchTest(DatabaseFixture):
    """用本地 HTTP 服务验证 ETag 条件请求返回 304 的路径。"""

    def setUp(self) -> None:
        super().setUp()
        self.payload = (
            NOTICE_CSV_HEADER
            + "N-1,Maker,RB-2,100,180,2.x,1,active\n"
        ).encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            etag = '"v1"'

            def do_GET(this):
                if this.headers.get("If-None-Match") == this.etag:
                    this.send_response(304)
                    this.end_headers()
                    return
                this.send_response(200)
                this.send_header("ETag", this.etag)
                this.send_header("Content-Type", "text/csv")
                this.end_headers()
                this.wfile.write(self.payload)

            def log_message(this, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def test_etag命中时跳过解析(self):
        from robotnotice.collector import register_source

        url = f"http://127.0.0.1:{self.port}/feed.csv"
        register_source(self.db, "http-1", "HTTP源", "http", url, "csv_feed")
        first = collect_source(self.db, self.store, "http-1",
                               fetch_attachments=False)
        self.assertTrue(first.fetched)
        second = collect_source(self.db, self.store, "http-1",
                                fetch_attachments=False)
        self.assertFalse(second.fetched)
        runs = self.db.query("SELECT is_conditional_hit FROM fetch_runs")
        self.assertEqual([row["is_conditional_hit"] for row in runs], [0, 1])


if __name__ == "__main__":
    unittest.main()
