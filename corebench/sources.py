"""Page sources: HTTP fetch, local files, and frozen snapshots.

A *snapshot* freezes every page of a run so a rule replay always sees exactly
the same pages, even after the source site has gone away.  Fetching supports
both ``http(s)://`` URLs and ``file://`` / plain local paths -- the automated
checks never need a real browser or an external site.

Encoding is handled page by page (archive headers are unreliable):

* the raw bytes of every page are kept, so a page that cannot be decoded is
  **not** lost -- it stays in the snapshot with only its bytes and a recorded
  failure;
* the bytes are decoded with an explicit encoding, chosen by the rule when it
  says so and only then falling back to the header / a BOM / UTF-8;
* how the text was obtained (encoding, whether it came from the source or a
  rule override) is recorded with the page so a later rule can re-decode the
  frozen bytes without refetching.

Snapshots written before raw bytes were stored only contain decoded text; they
load in a ``legacy`` mode and always replay with that original text.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .charset import DEFAULT_ENCODING, decode_bytes, normalize_encoding, resolve_encoding, sniff_bom

FETCH_TIMEOUT = 10.0

STATUS_DECODED = "decoded"
STATUS_UNDECODABLE = "undecodable"


class SourceError(RuntimeError):
    pass


@dataclass
class PageContent:
    page_id: str
    url: str
    html: str
    content_sha1: str
    raw: Optional[bytes] = None
    declared_encoding: Optional[str] = None       # charset the source claimed
    encoding: Optional[str] = None                # encoding actually used
    encoding_source: str = "builtin"              # rule-page | rule-default | recorded | bom | header | default
    status: str = STATUS_DECODED
    decode_error: Optional[str] = None

    @staticmethod
    def make(page_id: str, url: str, html: str) -> "PageContent":
        """Build a page from already-decoded text (text-only, no raw bytes)."""
        digest = hashlib.sha1(html.encode("utf-8")).hexdigest()
        return PageContent(page_id=page_id, url=url, html=html, content_sha1=digest,
                           encoding=DEFAULT_ENCODING, encoding_source="provided",
                           status=STATUS_DECODED)

    @staticmethod
    def from_bytes(page_id: str, url: str, raw: bytes,
                   declared_encoding: Optional[str] = None) -> "PageContent":
        """Build a page from raw bytes, using a provisional explicit decode.

        A rule-specific encoding is applied later via :meth:`with_encoding`;
        this provisional choice only exists so that a page fetched by a plain
        :func:`fetch_page` is already usable.
        """
        digest = hashlib.sha1(raw).hexdigest()
        encoding = resolve_encoding(page_id, declared_encoding, None, None, raw)
        text, error = decode_bytes(raw, encoding)
        source = "bom" if sniff_bom(raw) else ("header" if declared_encoding else "default")
        return PageContent(
            page_id=page_id, url=url, html=text, content_sha1=digest,
            raw=raw, declared_encoding=declared_encoding,
            encoding=encoding, encoding_source=source,
            status=STATUS_UNDECODABLE if error else STATUS_DECODED,
            decode_error=error,
        )

    @property
    def is_legacy(self) -> bool:
        return self.raw is None

    @property
    def is_undecodable(self) -> bool:
        return self.status == STATUS_UNDECODABLE

    @property
    def encoding_overridden(self) -> bool:
        """True when a rule (not the source) chose the encoding."""
        return self.encoding_source in ("rule-page", "rule-default")

    def with_encoding(self, spec) -> "PageContent":
        """Re-decode the frozen bytes according to ``spec``.

        Legacy text-only pages and pages without bytes are returned unchanged:
        an old snapshot that only holds decoded text must keep replaying with
        its original text.
        """
        if self.raw is None:
            return self
        encoding, source = _choose_encoding(self, spec)
        if (encoding == self.encoding and source == self.encoding_source
                and not self.is_undecodable):
            return self
        text, error = decode_bytes(self.raw, encoding)
        return PageContent(
            page_id=self.page_id, url=self.url, html=text,
            content_sha1=self.content_sha1, raw=self.raw,
            declared_encoding=self.declared_encoding,
            encoding=encoding, encoding_source=source,
            status=STATUS_UNDECODABLE if error else STATUS_DECODED,
            decode_error=error,
        )

    def decoded_elsewhere(self, encoding: Optional[str]) -> "PageContent":
        """Re-decode the frozen bytes with an explicit encoding.

        Used for on-demand inspection: viewing a page with a correction that is
        different from what the run actually used.  ``encoding`` of ``None``
        reproduces the run's own text without any change.
        """
        if self.raw is None or encoding is None:
            return self
        normalized = normalize_encoding(encoding)
        if normalized == self.encoding and not self.is_undecodable:
            return self
        text, error = decode_bytes(self.raw, normalized)
        return PageContent(
            page_id=self.page_id, url=self.url, html=text,
            content_sha1=self.content_sha1, raw=self.raw,
            declared_encoding=self.declared_encoding,
            encoding=normalized, encoding_source="viewer",
            status=STATUS_UNDECODABLE if error else STATUS_DECODED,
            decode_error=error,
        )

    def to_record(self) -> Dict[str, object]:
        data: Dict[str, object] = {
            "page_id": self.page_id,
            "url": self.url,
            "content_sha1": self.content_sha1,
            "encoding": self.encoding,
            "encoding_source": self.encoding_source,
            "declared_encoding": self.declared_encoding,
            "status": self.status,
        }
        if self.decode_error:
            data["decode_error"] = self.decode_error
        if self.raw is not None:
            data["raw_b64"] = base64.b64encode(self.raw).decode("ascii")
        else:
            # Legacy text-only page: keep the decoded text so it still replays.
            data["html"] = self.html
        return data


def _choose_encoding(page: PageContent, spec) -> tuple[str, str]:
    if spec is not None:
        override = spec.for_page(page.page_id)
        if override:
            return normalize_encoding(override), "rule-page"
        if spec.default:
            return normalize_encoding(spec.default), "rule-default"
    if page.encoding:
        return normalize_encoding(page.encoding), (
            page.encoding_source if page.encoding_source != "builtin" else "recorded")
    bom = sniff_bom(page.raw or b"")
    if bom:
        return bom, "bom"
    if page.declared_encoding:
        try:
            return normalize_encoding(page.declared_encoding), "header"
        except ValueError:
            pass
    return DEFAULT_ENCODING, "default"


def fetch_page(page_id: str, url: str, timeout: float = FETCH_TIMEOUT) -> PageContent:
    declared: Optional[str] = None
    if url.startswith(("http://", "https://")):
        request = urllib.request.Request(url, headers={"User-Agent": "corebench/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (test server)
                declared = response.headers.get_content_charset()
                raw = response.read()
                status = response.status
        except Exception as exc:  # noqa: BLE001 - surfaced as SourceError
            raise SourceError(f"fetch failed for {url}: {exc}") from exc
        if status >= 400:
            raise SourceError(f"fetch failed for {url}: HTTP {status}")
    else:
        path = url[7:] if url.startswith("file://") else url
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            raise SourceError(f"read failed for {path}: {exc}") from exc
    return PageContent.from_bytes(page_id, url, raw, declared)


@dataclass
class Snapshot:
    snapshot_id: str
    pages: List[PageContent] = field(default_factory=list)
    failures: Dict[str, str] = field(default_factory=dict)

    def page_ids(self) -> List[str]:
        return [page.page_id for page in self.pages]

    def get(self, page_id: str) -> Optional[PageContent]:
        for page in self.pages:
            if page.page_id == page_id:
                return page
        return None

    @property
    def is_complete(self) -> bool:
        return not self.failures

    @property
    def has_undecodable_pages(self) -> bool:
        return any(page.is_undecodable for page in self.pages)


def _snapshot_page(item: Dict[str, object]) -> PageContent:
    if isinstance(item.get("raw_b64"), str):
        raw = base64.b64decode(item["raw_b64"])
        page = PageContent.from_bytes(
            str(item["page_id"]), str(item["url"]), raw,
            item.get("declared_encoding") if isinstance(item.get("declared_encoding"), str) else None)
        if isinstance(item.get("encoding"), str):
            page = page.decoded_elsewhere(str(item["encoding"]))
            page.encoding_source = str(item.get("encoding_source") or page.encoding_source)
        page.content_sha1 = str(item["content_sha1"])
        actual = hashlib.sha1(raw).hexdigest()
        if actual != page.content_sha1:
            raise SourceError(
                f"snapshot page {page.page_id}: raw content hash mismatch")
        if isinstance(item.get("status"), str):
            # Decoding is deterministic, so the recomputed status should match
            # the recorded one.  Only carry the stored status across when it
            # agrees; a mismatch (e.g. a different codec build) must not make
            # the page claim it has no text while carrying decoded text.
            if str(item["status"]) == page.status and item.get("decode_error"):
                page.decode_error = str(item["decode_error"])
        return page
    # Legacy snapshot: only decoded text was stored.  It replays as-is.
    html = str(item.get("html", ""))
    digest = hashlib.sha1(html.encode("utf-8")).hexdigest()
    if digest != item["content_sha1"]:
        raise SourceError(f"snapshot page {item['page_id']}: content hash mismatch")
    return PageContent(
        page_id=str(item["page_id"]), url=str(item["url"]), html=html,
        content_sha1=str(item["content_sha1"]), raw=None,
        declared_encoding=None, encoding=None, encoding_source="legacy",
        status=STATUS_DECODED,
    )


class SnapshotStore:
    """Filesystem backed store; plain files, no database service."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def _path(self, snapshot_id: str) -> str:
        return os.path.join(self.root, f"{snapshot_id}.json")

    def save(self, snapshot: Snapshot) -> str:
        payload = {
            "snapshot_id": snapshot.snapshot_id,
            "pages": [p.to_record() for p in snapshot.pages],
            "failures": snapshot.failures,
        }
        path = self._path(snapshot.snapshot_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
        return path

    def load(self, snapshot_id: str) -> Snapshot:
        with open(self._path(snapshot_id), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        snapshot = Snapshot(snapshot_id=payload["snapshot_id"], failures=dict(payload.get("failures", {})))
        for item in payload["pages"]:
            snapshot.pages.append(_snapshot_page(item))
        return snapshot

    def exists(self, snapshot_id: str) -> bool:
        return os.path.exists(self._path(snapshot_id))

    def list_ids(self) -> List[str]:
        return sorted(name[:-5] for name in os.listdir(self.root) if name.endswith(".json"))
