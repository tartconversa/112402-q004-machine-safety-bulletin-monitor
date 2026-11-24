"""证据库只追加与内容寻址测试。"""
import unittest

from robotnotice.evidence import EvidenceStore, sha256_hex


class EvidenceStoreTest(unittest.TestCase):
    def test_相同内容只存一份且不可变路径(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as tmp:
            store = EvidenceStore(Path(tmp))
            payload = "公告原文".encode("utf-8")
            first = store.put(payload, "text/csv")
            second = store.put(payload, "text/csv")
            self.assertEqual(first.sha256, sha256_hex(payload))
            self.assertEqual(first.sha256, second.sha256)
            self.assertTrue(first.reused is False and second.reused is True)
            self.assertTrue(store.exists(first.sha256))
            self.assertEqual(store.get(first.sha256), payload)
            self.assertEqual(store.all_hashes(), {first.sha256})


if __name__ == "__main__":
    unittest.main()
