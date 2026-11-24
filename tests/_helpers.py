"""测试公共夹具：临时数据库、证据库与样例文件。"""
import tempfile
import unittest
from pathlib import Path

from robotnotice.evidence import EvidenceStore
from robotnotice.storage import Database

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"

NOTICE_CSV_HEADER = (
    "notice,issuer,model,serial_from,serial_to,firmware,revision,state\n"
)


class DatabaseFixture(unittest.TestCase):
    """每个用例独立的数据库与证据目录。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = Database(self.base / "robotnotice.db")
        self.db.initialize()
        self.store = EvidenceStore(self.base / "evidence")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    # -- 便捷构造 -------------------------------------------------------
    def write_feed(self, name: str, content: str) -> Path:
        path = self.base / name
        path.write_text(content, encoding="utf-8")
        return path

    def write_bytes(self, name: str, data: bytes) -> Path:
        path = self.base / name
        path.write_bytes(data)
        return path

    def register_file_source(
        self, path: Path, source_id: str = "src-1",
        parser: str = "csv_feed",
    ) -> None:
        from robotnotice.collector import register_source

        register_source(
            self.db, source_id, "测试源", "file", str(path), parser
        )
