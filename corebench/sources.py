"""Page sources: HTTP fetch, local files, and frozen snapshots.

A *snapshot* freezes the raw HTML of every page of a run under a content hash,
so a rule replay always sees exactly the same pages.  Fetching supports both
``http(s)://`` URLs and ``file://`` / plain local paths -- the automated
checks never need a real browser or an external site.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

FETCH_TIMEOUT = 10.0


class SourceError(RuntimeError):
    pass


@dataclass
class PageContent:
    page_id: str
    url: str
    html: str
    content_sha1: str

    @staticmethod
    def make(page_id: str, url: str, html: str) -> "PageContent":
        digest = hashlib.sha1(html.encode("utf-8")).hexdigest()
        return PageContent(page_id=page_id, url=url, html=html, content_sha1=digest)


def fetch_page(page_id: str, url: str, timeout: float = FETCH_TIMEOUT) -> PageContent:
    if url.startswith(("http://", "https://")):
        request = urllib.request.Request(url, headers={"User-Agent": "corebench/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (test server)
                charset = response.headers.get_content_charset() or "utf-8"
                html = response.read().decode(charset, errors="replace")
                status = response.status
        except Exception as exc:  # noqa: BLE001 - surfaced as SourceError
            raise SourceError(f"fetch failed for {url}: {exc}") from exc
        if status >= 400:
            raise SourceError(f"fetch failed for {url}: HTTP {status}")
    else:
        path = url[7:] if url.startswith("file://") else url
        try:
            with open(path, "r", encoding="utf-8") as handle:
                html = handle.read()
        except OSError as exc:
            raise SourceError(f"read failed for {path}: {exc}") from exc
    return PageContent.make(page_id, url, html)


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
            "pages": [
                {"page_id": p.page_id, "url": p.url, "html": p.html, "content_sha1": p.content_sha1}
                for p in snapshot.pages
            ],
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
            page = PageContent(item["page_id"], item["url"], item["html"], item["content_sha1"])
            actual = hashlib.sha1(page.html.encode("utf-8")).hexdigest()
            if actual != page.content_sha1:
                raise SourceError(f"snapshot {snapshot_id} page {page.page_id}: content hash mismatch")
            snapshot.pages.append(page)
        return snapshot

    def exists(self, snapshot_id: str) -> bool:
        return os.path.exists(self._path(snapshot_id))

    def list_ids(self) -> List[str]:
        return sorted(name[:-5] for name in os.listdir(self.root) if name.endswith(".json"))
