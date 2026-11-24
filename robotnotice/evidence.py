"""只追加的内容寻址原始证据库。

每次抓取到的字节都按 SHA-256 存入 ``<root>/ab/ab<hash>`` 形式的路径，
同名内容只存一份，但任何历史抓取记录都通过哈希引用到不可变文件；
证据一旦写入不再修改或删除。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class EvidenceRecord:
    sha256: str
    path: Path
    media_type: str | None
    size_bytes: int
    reused: bool


class EvidenceStore:
    """内容寻址证据存储，所有写入均为不可变追加。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    def put(self, data: bytes, media_type: str | None = None) -> EvidenceRecord:
        digest = sha256_hex(data)
        target = self.path_for(digest)
        reused = target.exists()
        if not reused:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
        return EvidenceRecord(digest, target, media_type, len(data), reused)

    def get(self, digest: str) -> bytes:
        return self.path_for(digest).read_bytes()

    def exists(self, digest: str) -> bool:
        return self.path_for(digest).exists()

    def all_hashes(self) -> set[str]:
        return {path.name for path in self.root.glob("*/*") if len(path.name) == 64}
