"""设备台账导入、型号别名管理与待核项。

台账来自客户门户或维保导出，命名不统一：导入时保留原始型号文本，
同时写入归一化型号。同一资产出现互相矛盾的记录、缺序列号或
型号无法归一到已知型号时，生成待核项，匹配阶段不得把网页命中
直接当成维修指令。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .evidence import utcnow
from .storage import Database
from .versions import canonical_model, normalize_model

ASSET_HEADERS = ["asset", "customer", "model", "serial", "firmware", "site"]


@dataclass
class ImportReport:
    import_id: int
    upserted: int
    verification_items: int
    details: list[str]


def add_alias(
    db: Database, alias: str, canonical: str, note: str = "", actor: str = "admin"
) -> None:
    alias_key = normalize_model(alias)
    canonical_value = normalize_model(canonical)
    if not alias_key or not canonical_value:
        raise ValueError("别名与规范型号均不能为空")
    db.execute(
        "INSERT INTO model_aliases(alias, canonical, note, updated_at) "
        "VALUES(?,?,?,?) ON CONFLICT(alias) DO UPDATE SET canonical=excluded.canonical, "
        "note=excluded.note, updated_at=excluded.updated_at",
        (alias_key, canonical_value, note, utcnow()),
    )
    db.audit(
        actor, "alias-added", "model_alias", alias_key,
        {"canonical": canonical_value, "note": note},
    )
    db.commit()


def load_aliases(db: Database) -> dict[str, str]:
    return {
        row["alias"]: row["canonical"]
        for row in db.query("SELECT alias, canonical FROM model_aliases")
    }


def _known_models(db: Database) -> set[str]:
    models = {
        row["model"] for row in db.query("SELECT DISTINCT model FROM notice_scope")
    }
    models |= {row["canonical"] for row in db.query("SELECT canonical FROM model_aliases")}
    return models


def import_ledger(
    db: Database,
    data: bytes,
    source_label: str,
    content_sha: str | None = None,
    actor: str = "asset-admin",
) -> ImportReport:
    """导入一份台账 CSV，按资产 ID 合并并生成待核项。"""
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), strict=True)
    missing = set(ASSET_HEADERS) - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"台账 CSV 缺少列: {sorted(missing)}")

    now = utcnow()
    cursor = db.execute(
        "INSERT INTO asset_imports(source_label, content_sha, imported_at) "
        "VALUES(?,?,?)",
        (source_label, content_sha, now),
    )
    import_id = int(cursor.lastrowid)
    aliases = load_aliases(db)
    known = _known_models(db)
    upserted = 0
    details: list[str] = []

    for row in reader:
        asset_id = (row.get("asset") or "").strip()
        if not asset_id:
            continue
        raw_model = (row.get("model") or "").strip()
        canonical = canonical_model(raw_model, aliases)
        serial = (row.get("serial") or "").strip() or None
        firmware = (row.get("firmware") or "").strip() or None
        customer = (row.get("customer") or "").strip()
        site = (row.get("site") or "").strip() or None

        existing = db.query_one(
            "SELECT * FROM assets WHERE asset_id=?", (asset_id,)
        )
        if existing is not None:
            conflicts = _detect_conflicts(
                existing, customer, canonical, serial, firmware, site
            )
            for conflict in conflicts:
                _open_item(db, asset_id, "conflicting-record", conflict, now)
                details.append(f"{asset_id}: {conflict}")
        db.execute(
            "INSERT INTO assets(asset_id, customer, canonical_model, raw_model, "
            "serial, firmware, site, import_id, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(asset_id) DO UPDATE SET customer=excluded.customer, "
            "canonical_model=excluded.canonical_model, raw_model=excluded.raw_model, "
            "serial=excluded.serial, firmware=excluded.firmware, site=excluded.site, "
            "import_id=excluded.import_id, updated_at=excluded.updated_at",
            (
                asset_id,
                customer,
                canonical,
                raw_model,
                serial,
                firmware,
                site,
                import_id,
                now,
            ),
        )
        upserted += 1

        if not serial:
            message = "台账缺少序列号，序列号区间类通告无法自动判定"
            _open_item(db, asset_id, "missing-serial", message, now)
            details.append(f"{asset_id}: {message}")
        if known and canonical not in known:
            message = f"型号 {canonical}（原始 {raw_model}）未出现在任何通告或别名表中"
            _open_item(db, asset_id, "unknown-model", message, now)
            details.append(f"{asset_id}: {message}")

    item_count = db.query_one(
        "SELECT COUNT(*) AS c FROM verification_items WHERE status='open'"
    )["c"]
    db.audit(
        actor, "ledger-imported", "asset_import", str(import_id),
        {"source_label": source_label, "upserted": upserted, "sha256": content_sha},
    )
    db.commit()
    return ImportReport(import_id, upserted, item_count, details)


def _detect_conflicts(existing, customer, canonical, serial, firmware, site) -> list[str]:
    conflicts: list[str] = []
    checks = (
        ("customer", customer, "客户"),
        ("canonical_model", canonical, "型号"),
        ("serial", serial, "序列号"),
        ("firmware", firmware, "固件"),
        ("site", site, "现场"),
    )
    for field, new_value, label in checks:
        old_value = existing[field]
        if _present(old_value) and _present(new_value) and old_value != new_value:
            conflicts.append(f"{label}冲突：台账已有 {old_value}，新导入 {new_value}")
    return conflicts


def _present(value: str | None) -> bool:
    return value is not None and value != ""


def _open_item(db: Database, asset_id: str, kind: str, detail: str, now: str) -> None:
    duplicate = db.query_one(
        "SELECT 1 FROM verification_items WHERE asset_id=? AND kind=? AND status='open' "
        "AND detail=? LIMIT 1",
        (asset_id, kind, detail),
    )
    if duplicate is None:
        db.execute(
            "INSERT INTO verification_items(asset_id, kind, detail, status, created_at) "
            "VALUES(?,?,?, 'open', ?)",
            (asset_id, kind, detail, now),
        )


def resolve_item(
    db: Database, item_id: int, resolution: str, actor: str = "asset-admin"
) -> None:
    item = db.query_one(
        "SELECT * FROM verification_items WHERE item_id=?", (item_id,)
    )
    if item is None:
        raise ValueError(f"待核项 {item_id} 不存在")
    if item["status"] != "open":
        raise ValueError(f"待核项 {item_id} 已是 {item['status']} 状态")
    db.execute(
        "UPDATE verification_items SET status='resolved', resolved_at=?, resolution=? "
        "WHERE item_id=?",
        (utcnow(), resolution, item_id),
    )
    db.audit(
        actor, "verification-resolved", "verification_item", str(item_id),
        {"resolution": resolution},
    )
    db.commit()
