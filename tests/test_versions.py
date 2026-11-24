"""型号别名、序列号字符串闭区间与固件通配匹配测试。"""
import unittest

from robotnotice.versions import (
    ScopeEntry,
    firmware_matches,
    match_asset,
    normalize_model,
    canonical_model,
    serial_in_range,
)


class NormalizeModelTest(unittest.TestCase):
    def test_型号归一规则(self):
        for raw, expected in [
            ("rb2", "RB-2"),
            ("RB_2", "RB-2"),
            (" rb-2 ", "RB-2"),
            ("RC 4", "RC-4"),
            ("rc--4", "RC-4"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_model(raw), expected)

    def test_别名表优先于通用归一(self):
        self.assertEqual(
            canonical_model("arm-x", {"ARM-X": "RB-2"}), "RB-2"
        )
        self.assertEqual(canonical_model("rb2"), "RB-2")


class SerialRangeTest(unittest.TestCase):
    def test_字符串闭区间(self):
        # 样例约定：按字符串比较，"90" > "180"（'9' > '1'）
        self.assertTrue(serial_in_range("120", "100", "180"))
        self.assertTrue(serial_in_range("100", "100", "180"))
        self.assertTrue(serial_in_range("180", "100", "180"))
        self.assertFalse(serial_in_range("190", "100", "180"))
        # 上界为空只检查下界；字符串比较下 "90" > "100"（'9' > '1'），故满足下界
        self.assertTrue(serial_in_range("90", "100", None))
        self.assertFalse(serial_in_range("09", "100", None))
        # 下界为空只检查上界：字符串比较下 "999" > "180"
        self.assertFalse(serial_in_range("999", None, "180"))
        self.assertTrue(serial_in_range("100", None, "180"))

    def test_缺序列号无法判定(self):
        self.assertFalse(serial_in_range(None, "1", "9"))
        self.assertFalse(serial_in_range("", "1", "9"))

    def test_空边界表示不限(self):
        self.assertTrue(serial_in_range("任意", None, None))


class FirmwareTest(unittest.TestCase):
    def test_段级通配(self):
        self.assertTrue(firmware_matches("2.0", "2.x"))
        self.assertTrue(firmware_matches("2.1.3", "2.*"))
        self.assertFalse(firmware_matches("3.0", "2.x"))
        self.assertTrue(firmware_matches("1.8", "1.8"))
        self.assertFalse(firmware_matches("1.9", "1.8"))

    def test_空模式不限固件(self):
        self.assertTrue(firmware_matches("9.9", ""))
        self.assertTrue(firmware_matches(None, None))


class MatchAssetTest(unittest.TestCase):
    SCOPE = [
        ScopeEntry("RB-2", "100", "180", "2.x"),
        ScopeEntry("RB-2", "120", "160", "2.1"),
    ]

    def test_型号不符(self):
        result = match_asset("RC-4", "150", "2.1", self.SCOPE)
        self.assertEqual(result.result, "not-applicable")
        self.assertEqual(result.reason_code, "model-mismatch")

    def test_序列号落在区间外(self):
        result = match_asset("RB-2", "190", "2.1", self.SCOPE)
        self.assertEqual(result.result, "not-applicable")
        self.assertEqual(result.reason_code, "serial-out-of-range")

    def test_固件不匹配(self):
        result = match_asset("RB-2", "150", "3.0", self.SCOPE)
        self.assertEqual(result.result, "not-applicable")
        self.assertEqual(result.reason_code, "firmware-mismatch")

    def test_命中适用范围(self):
        result = match_asset("RB-2", "145", "2.1", self.SCOPE)
        self.assertEqual(result.result, "applicable")
        self.assertEqual(result.reason_code, "in-scope")

    def test_缺序列号且范围限序列号则待核(self):
        result = match_asset("RB-2", None, "2.1", self.SCOPE)
        self.assertEqual(result.result, "unverifiable")
        self.assertEqual(result.reason_code, "missing-serial")

    def test_缺序列号但范围不限序列号仍可判定(self):
        scope = [ScopeEntry("RC-4", None, None, "1.8")]
        result = match_asset("RC-4", None, "1.8", scope)
        self.assertEqual(result.result, "applicable")

    def test_样例语义_缩窄后旧设备被排除(self):
        # N-7 rev2 只保留 120-160/2.1：A1(115,2.0) 不再适用，A2 适用
        rev2 = [ScopeEntry("RB-2", "120", "160", "2.1")]
        self.assertEqual(
            match_asset("RB-2", "115", "2.0", rev2).result, "not-applicable"
        )
        self.assertEqual(
            match_asset("RB-2", "145", "2.1", rev2).result, "applicable"
        )


if __name__ == "__main__":
    unittest.main()
