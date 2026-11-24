"""型号别名、序列号字符串闭区间与固件版本匹配。

样例约定：序列号范围按**字符串闭区间**理解（见仓库 README），
因此这里不做数值转换，直接使用字符串比较；固件模式支持段级通配
（``2.x`` / ``2.*`` 可匹配 ``2.0``、``2.1.3``）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_BOUNDARY = re.compile(r"(?<=[A-Za-z])(?=\d)")
_UNDERSCORE = re.compile(r"[_\s]+")
_DASHES = re.compile(r"-+")


def normalize_model(name: str | None) -> str:
    """把台账里的杂乱型号归一为规范形式。

    规则：去首尾空白、大写、去除内部空白、下划线转连字符、
    字母与数字相邻处补连字符。例如 ``rb2``、``RB_2``、`` rb-2 ``
    都归一为 ``RB-2``。
    """
    if not name:
        return ""
    text = name.strip().upper()
    text = _UNDERSCORE.sub("-", text)
    text = _BOUNDARY.sub("-", text)
    text = _DASHES.sub("-", text)
    return text.strip("-")


def canonical_model(raw: str | None, aliases: dict[str, str] | None = None) -> str:
    """先查别名表，未命中再按通用规则归一。"""
    normalized = normalize_model(raw)
    if aliases and normalized in aliases:
        return aliases[normalized]
    return normalized


def serial_in_range(serial: str | None, low: str | None, high: str | None) -> bool:
    """字符串闭区间判断；空边界表示该侧不限。序列号缺失返回 False。"""
    if serial is None or serial == "":
        return False
    if low and serial < low:
        return False
    if high and serial > high:
        return False
    return True


def firmware_matches(version: str | None, pattern: str | None) -> bool:
    """固件版本匹配。空模式视为不限；``x``/``*`` 为段级通配。"""
    if pattern is None or pattern.strip() == "":
        return True
    pat = pattern.strip().lower()
    if pat in ("*", "x"):
        return True
    ver = (version or "").strip().lower()
    pat_segments = pat.split(".")
    ver_segments = ver.split(".")
    for index, segment in enumerate(pat_segments):
        if segment in ("x", "*"):
            continue
        if index >= len(ver_segments) or ver_segments[index] != segment:
            return False
    return True


@dataclass(frozen=True)
class ScopeEntry:
    """通告某一版本中的单条适用范围。"""

    model: str
    serial_from: str | None = None
    serial_to: str | None = None
    firmware: str | None = None

    @property
    def serial_bounded(self) -> bool:
        return bool(self.serial_from or self.serial_to)

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "serial_from": self.serial_from,
            "serial_to": self.serial_to,
            "firmware": self.firmware,
        }


@dataclass(frozen=True)
class MatchResult:
    """单台设备对单个通告版本的匹配结论。"""

    result: str  # applicable | not-applicable | unverifiable
    reason_code: str
    reason_text: str
    scope_model: str | None = None


def match_asset(
    canonical_model_name: str,
    serial: str | None,
    firmware: str | None,
    scope: list[ScopeEntry],
) -> MatchResult:
    """按型号 → 序列号区间 → 固件的顺序评估适用范围。

    结论取值：
    - applicable：命中至少一条范围；
    - unverifiable：型号命中但设备缺序列号，而范围限定了序列号区间；
    - not-applicable：型号不符、序列号落在区间外或固件不匹配。
    """
    candidates = [entry for entry in scope if entry.model == canonical_model_name]
    if not candidates:
        return MatchResult(
            "not-applicable",
            "model-mismatch",
            f"型号 {canonical_model_name or '<空>'} 不在通告适用型号内",
        )

    missing_serial = serial is None or serial == ""
    unverifiable: MatchResult | None = None
    for entry in candidates:
        if missing_serial:
            if entry.serial_bounded:
                unverifiable = MatchResult(
                    "unverifiable",
                    "missing-serial",
                    f"设备缺少序列号，无法核验区间 {entry.serial_from or ''}–"
                    f"{entry.serial_to or ''}，转人工待核",
                    entry.model,
                )
                continue
            # 范围不限序列号：跳过序列号检查，继续核验固件
        else:
            if not serial_in_range(serial, entry.serial_from, entry.serial_to):
                return MatchResult(
                    "not-applicable",
                    "serial-out-of-range",
                    f"序列号 {serial or '<空>'} 不在字符串闭区间 "
                    f"[{entry.serial_from or ''}, {entry.serial_to or ''}] 内",
                    entry.model,
                )
        if not firmware_matches(firmware, entry.firmware):
            return MatchResult(
                "not-applicable",
                "firmware-mismatch",
                f"固件 {firmware or '<空>'} 不满足模式 {entry.firmware}",
                entry.model,
            )
        return MatchResult(
            "applicable",
            "in-scope",
            f"型号 {entry.model} 命中，序列号 "
            f"{serial or '<不限>'} 位于区间 [{entry.serial_from or ''}, "
            f"{entry.serial_to or ''}]，固件 {firmware or '<空>'} 满足 "
            f"{entry.firmware or '<不限>'}",
            entry.model,
        )
    if unverifiable is not None:
        return unverifiable
    entry = candidates[0]
    return MatchResult(
        "not-applicable",
        "no-scope-match",
        f"型号 {entry.model} 命中但无满足全部条件的范围条目",
        entry.model,
    )
