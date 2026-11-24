"""公告源抓取与解析。

抓取器负责拿到原始字节（文件源或 HTTP 源，HTTP 支持 ETag/Last-Modified
条件请求），解析器把字节解析为统一的 :class:`ParsedRecord`。
解析器只做结构化提取，不做任何匹配或处置决策。
"""
from __future__ import annotations

import csv
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from .versions import ScopeEntry, normalize_model

MAX_BYTES = 20 * 1024 * 1024
USER_AGENT = "robotnotice-collector/1.0"


@dataclass
class FetchResult:
    ok: bool
    data: bytes | None = None
    media_type: str | None = None
    http_status: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    conditional_hit: bool = False
    error: str | None = None


@dataclass
class ParsedAttachment:
    rel_url: str


@dataclass
class ParsedRecord:
    notice_id: str
    issuer: str
    revision_no: str
    state: str  # active | withdrawn
    scope: list[ScopeEntry] = field(default_factory=list)
    attachments: list[ParsedAttachment] = field(default_factory=list)
    title: str | None = None


class FileFetcher:
    """本地文件源，便于离线测试与门户导出文件接入。"""

    def fetch(
        self,
        url: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        path = Path(url)
        if not path.is_file():
            return FetchResult(ok=False, error=f"文件不存在: {path}")
        data = path.read_bytes()
        if len(data) > MAX_BYTES:
            return FetchResult(ok=False, error=f"内容超过 {MAX_BYTES} 字节上限")
        return FetchResult(ok=True, data=data, media_type=self._guess_media(path))

    def resolve_attachment(self, base_url: str, rel_url: str) -> str:
        return str(Path(base_url).parent / rel_url)

    @staticmethod
    def _guess_media(path: Path) -> str:
        suffix = path.suffix.lower()
        return {
            ".csv": "text/csv",
            ".json": "application/json",
            ".html": "text/html",
            ".htm": "text/html",
            ".pdf": "application/pdf",
        }.get(suffix, "application/octet-stream")


class HttpFetcher:
    """基于标准库的 HTTP 抓取器，支持条件请求与下载体积上限。"""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def fetch(
        self,
        url: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = self._read_capped(response)
                return FetchResult(
                    ok=True,
                    data=data,
                    media_type=response.headers.get("Content-Type"),
                    http_status=response.status,
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return FetchResult(
                    ok=True,
                    http_status=304,
                    conditional_hit=True,
                    etag=etag,
                    last_modified=last_modified,
                )
            return FetchResult(ok=False, http_status=exc.code, error=str(exc))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return FetchResult(ok=False, error=str(exc))

    @staticmethod
    def _read_capped(response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise OSError(f"内容超过 {MAX_BYTES} 字节上限")
            chunks.append(chunk)
        return b"".join(chunks)

    def resolve_attachment(self, base_url: str, rel_url: str) -> str:
        return urllib.parse.urljoin(base_url, rel_url)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


class CsvFeedParser:
    """解析与 ``samples/notices.csv`` 同构的通告 CSV。"""

    def parse(self, data: bytes) -> list[ParsedRecord]:
        text = data.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text), strict=True)
        required = {
            "notice",
            "issuer",
            "model",
            "serial_from",
            "serial_to",
            "firmware",
            "revision",
            "state",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV 缺少列: {sorted(missing)}")
        grouped: dict[tuple[str, str], ParsedRecord] = {}
        for row in reader:
            key = (row["notice"].strip(), row["revision"].strip())
            record = grouped.get(key)
            if record is None:
                record = ParsedRecord(
                    notice_id=row["notice"].strip(),
                    issuer=row["issuer"].strip(),
                    revision_no=row["revision"].strip(),
                    state=row["state"].strip(),
                )
                grouped[key] = record
            record.scope.append(
                ScopeEntry(
                    model=normalize_model(row["model"]),
                    serial_from=_clean(row.get("serial_from")),
                    serial_to=_clean(row.get("serial_to")),
                    firmware=_clean(row.get("firmware")),
                )
            )
        return list(grouped.values())


class JsonFeedParser:
    """解析 JSON 通告接口，支持每个版本携带多条范围与附件。"""

    def parse(self, data: bytes) -> list[ParsedRecord]:
        payload = json.loads(data.decode("utf-8"))
        notices = payload.get("notices") if isinstance(payload, dict) else payload
        if not isinstance(notices, list):
            raise ValueError("JSON 通告接口需包含 notices 数组")
        records: list[ParsedRecord] = []
        for notice in notices:
            for revision in notice.get("revisions", []):
                scope = [
                    ScopeEntry(
                        model=normalize_model(item.get("model")),
                        serial_from=_clean(item.get("serial_from")),
                        serial_to=_clean(item.get("serial_to")),
                        firmware=_clean(item.get("firmware")),
                    )
                    for item in revision.get("scope", [])
                ]
                attachments = [
                    ParsedAttachment(rel_url=item["rel_url"])
                    for item in revision.get("attachments", [])
                    if item.get("rel_url")
                ]
                records.append(
                    ParsedRecord(
                        notice_id=str(notice["notice_id"]).strip(),
                        issuer=str(notice.get("issuer", "")).strip(),
                        title=_clean(notice.get("title")),
                        revision_no=str(revision["revision"]).strip(),
                        state=str(revision.get("state", "active")).strip(),
                        scope=scope,
                        attachments=attachments,
                    )
                )
        return records


class _PageExtractor(HTMLParser):
    """从 HTML 公告页提取 data-* 通告条目与附件链接。

    通告条目由带 ``data-notice`` 的元素声明，其内部（或自身）的
    ``<a href>`` 视为该通告的附件链接。
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: dict[tuple[str, str], ParsedRecord] = {}
        self._current_key: tuple[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value for key, value in attrs}
        notice_id = attributes.get("data-notice")
        if notice_id:
            notice_id = notice_id.strip()
            revision_no = (attributes.get("data-revision") or "1").strip()
            state = (attributes.get("data-state") or "active").strip()
            key = (notice_id, revision_no)
            self._current_key = key
            record = self.records.get(key)
            if record is None:
                record = ParsedRecord(
                    notice_id=notice_id,
                    issuer=(attributes.get("data-issuer") or "").strip(),
                    title=attributes.get("data-title"),
                    revision_no=revision_no,
                    state=state,
                )
                self.records[key] = record
            if attributes.get("data-model"):
                record.scope.append(
                    ScopeEntry(
                        model=normalize_model(attributes.get("data-model")),
                        serial_from=_clean(attributes.get("data-serial-from")),
                        serial_to=_clean(attributes.get("data-serial-to")),
                        firmware=_clean(attributes.get("data-firmware")),
                    )
                )
        href = attributes.get("href")
        if tag == "a" and href and self._current_key is not None:
            self.records[self._current_key].attachments.append(
                ParsedAttachment(rel_url=href.strip())
            )

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._current_key is not None:
            self._current_key = None


class PageParser:
    """解析内嵌 data-* 标注的公告 HTML 页面。"""

    def parse(self, data: bytes) -> list[ParsedRecord]:
        extractor = _PageExtractor()
        extractor.feed(data.decode("utf-8"))
        return list(extractor.records.values())


PARSERS = {
    "csv_feed": CsvFeedParser,
    "json_feed": JsonFeedParser,
    "html_page": PageParser,
}


def get_parser(name: str):
    try:
        return PARSERS[name]()
    except KeyError as exc:
        raise ValueError(f"未知解析器 {name}，可选: {sorted(PARSERS)}") from exc


def get_fetcher(kind: str):
    if kind == "http":
        return HttpFetcher()
    if kind == "file":
        return FileFetcher()
    raise ValueError(f"未知源类型 {kind}")
