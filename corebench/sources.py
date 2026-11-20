"""Page sources: HTTP fetch, local files, and frozen snapshots.

A *snapshot* freezes the raw HTML of every page of a run under a content hash,
so a rule replay always sees exactly the same pages.  Fetching supports both
``http(s)://`` URLs and ``file://`` / plain local paths -- the automated
checks never need a real browser or an external site.

Large crawls are stored page by page instead of as one JSON blob: every page
gets its own file under ``<snapshot>/pages/`` and an append-only ``index.jsonl``
records its metadata.  Writing a page therefore costs O(page size) rather than
re-serialising the whole snapshot, and readers can stream pages one at a time
instead of holding every original HTML page in memory.  The index is ordered by
nothing in particular (pages may arrive interleaved across boreholes); merge
code sorts by ``page_id`` where deterministic order matters.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

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


def _safe_name(value: str) -> str:
    """Filesystem-safe, reversible encoding of an arbitrary id."""
    return urllib.parse.quote(value, safe="")


@dataclass
class PageEntry:
    """Metadata of one frozen page; the HTML itself lives in ``pages/<file>``."""

    page_id: str
    url: str
    content_sha1: str
    filename: str


class Snapshot:
    """Lazily loaded view of a frozen snapshot.

    ``iter_pages`` yields one page at a time (sorted by ``page_id``) so replay
    never needs the whole corpus resident.  ``pages`` is kept for callers that
    deliberately want the eager list (tests, small snapshots).
    """

    def __init__(self, snapshot_id: str, entries: Optional[List[PageEntry]] = None,
                 failures: Optional[Dict[str, str]] = None,
                 backend: Optional["SnapshotStore"] = None) -> None:
        self.snapshot_id = snapshot_id
        self.failures: Dict[str, str] = dict(failures or {})
        self._entries: Dict[str, PageEntry] = {entry.page_id: entry for entry in (entries or [])}
        self._backend = backend

    # ------------------------------------------------------------ reading --
    def _load(self, entry: PageEntry) -> PageContent:
        if self._backend is None:
            raise SourceError(f"snapshot {self.snapshot_id} has no storage backend")
        return self._backend.load_page(self.snapshot_id, entry)

    @property
    def pages(self) -> List[PageContent]:
        return [self._load(self._entries[page_id]) for page_id in sorted(self._entries)]

    def iter_pages(self) -> Iterator[PageContent]:
        for page_id in sorted(self._entries):
            yield self._load(self._entries[page_id])

    def page_ids(self) -> List[str]:
        return list(self._entries)

    def has_page(self, page_id: str) -> bool:
        return page_id in self._entries

    def get(self, page_id: str) -> Optional[PageContent]:
        entry = self._entries.get(page_id)
        return self._load(entry) if entry else None

    @property
    def is_complete(self) -> bool:
        return not self.failures

    # ------------------------------------------------------------ writing --
    def add_page(self, page: PageContent) -> None:
        if self._backend is None:
            raise SourceError(f"snapshot {self.snapshot_id} has no storage backend")
        self._entries[page.page_id] = self._backend.add_page(self.snapshot_id, page)

    def save(self) -> str:
        if self._backend is None:
            raise SourceError(f"snapshot {self.snapshot_id} has no storage backend")
        return self._backend.save(self)


class SnapshotStore:
    """Filesystem backed store; plain files, no database service.

    Layout::

        <root>/<snapshot_id>/meta.json       failures / snapshot metadata
        <root>/<snapshot_id>/index.jsonl     one append-only line per page
        <root>/<snapshot_id>/pages/<id>.json frozen HTML per page

    ``bytes_written`` counts the bytes actually handed to the disk so callers
    (and tests) can verify a crawl does not rewrite the corpus repeatedly.
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.bytes_written = 0

    # --------------------------------------------------------------- paths --
    def _dir(self, snapshot_id: str) -> str:
        return os.path.join(self.root, _safe_name(snapshot_id))

    def _meta_path(self, snapshot_id: str) -> str:
        return os.path.join(self._dir(snapshot_id), "meta.json")

    def _index_path(self, snapshot_id: str) -> str:
        return os.path.join(self._dir(snapshot_id), "index.jsonl")

    def _pages_dir(self, snapshot_id: str) -> str:
        return os.path.join(self._dir(snapshot_id), "pages")

    # ------------------------------------------------------------- writing --
    def _write_text(self, path: str, text: str) -> None:
        self._write_bytes(path, text.encode("utf-8"))

    def _write_bytes(self, path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
        self.bytes_written += len(data)

    def _append(self, path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab") as handle:
            handle.write(data)
        self.bytes_written += len(data)

    def load_or_create(self, snapshot_id: str) -> Snapshot:
        if self.exists(snapshot_id):
            return self.load(snapshot_id)
        os.makedirs(self._pages_dir(snapshot_id), exist_ok=True)
        return Snapshot(snapshot_id=snapshot_id, backend=self)

    def add_page(self, snapshot_id: str, page: PageContent) -> PageEntry:
        """Freeze one page: write its file, then append one index record.

        Both writes are O(page size).  The page file is renamed into place
        before its index line exists, so a crash between the two leaves an
        orphan file that a later resume simply overwrites -- never an index
        entry pointing at missing content.
        """
        filename = _safe_name(page.page_id) + ".json"
        payload = {"page_id": page.page_id, "url": page.url, "html": page.html,
                   "content_sha1": page.content_sha1}
        self._write_bytes(os.path.join(self._pages_dir(snapshot_id), filename),
                          json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        entry = PageEntry(page_id=page.page_id, url=page.url,
                          content_sha1=page.content_sha1, filename=filename)
        index_line = {"page_id": page.page_id, "url": page.url,
                      "content_sha1": page.content_sha1, "filename": filename}
        self._append(self._index_path(snapshot_id),
                     (json.dumps(index_line, ensure_ascii=False) + "\n").encode("utf-8"))
        return entry

    def clear_failures(self, snapshot: Snapshot) -> None:
        """Remove failure markers for pages that are now frozen.

        Called right after a page is written, so a resume that later crashes
        elsewhere cannot resurrect a stale marker for a page that is on disk.
        Writes only when something actually changed.
        """
        stale = [page_id for page_id in snapshot.failures if snapshot.has_page(page_id)]
        if not stale:
            return
        for page_id in stale:
            del snapshot.failures[page_id]
        self.save(snapshot)

    def save(self, snapshot: "Snapshot") -> str:
        payload = {"snapshot_id": snapshot.snapshot_id, "failures": snapshot.failures}
        path = self._meta_path(snapshot.snapshot_id)
        self._write_text(path, json.dumps(payload, ensure_ascii=False))
        return path

    # ------------------------------------------------------------- reading --
    def load_failures(self, snapshot_id: str) -> Dict[str, str]:
        meta_path = self._meta_path(snapshot_id)
        if not os.path.exists(meta_path):
            return {}
        with open(meta_path, "r", encoding="utf-8") as handle:
            return dict(json.load(handle).get("failures", {}))

    def load(self, snapshot_id: str) -> Snapshot:
        failures = self.load_failures(snapshot_id)
        entries: Dict[str, PageEntry] = {}
        index_path = self._index_path(snapshot_id)
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        # A torn trailing line from an interrupted append is ignored;
                        # the page simply looks unfetched and will be re-fetched.
                        continue
                    entries[item["page_id"]] = PageEntry(
                        page_id=item["page_id"], url=item["url"],
                        content_sha1=item["content_sha1"], filename=item["filename"])
        return Snapshot(snapshot_id=snapshot_id, entries=list(entries.values()),
                        failures=failures, backend=self)

    def load_page(self, snapshot_id: str, entry: PageEntry) -> PageContent:
        path = os.path.join(self._pages_dir(snapshot_id), entry.filename)
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        page = PageContent(payload["page_id"], payload["url"], payload["html"],
                           payload["content_sha1"])
        actual = hashlib.sha1(page.html.encode("utf-8")).hexdigest()
        if actual != page.content_sha1:
            raise SourceError(f"snapshot {snapshot_id} page {page.page_id}: content hash mismatch")
        return page

    def load_page_by_id(self, snapshot_id: str, page_id: str) -> Optional[PageContent]:
        """Read a single page without building the whole snapshot index."""
        filename = _safe_name(page_id) + ".json"
        if not os.path.exists(os.path.join(self._pages_dir(snapshot_id), filename)):
            return None
        entry = PageEntry(page_id=page_id, url="", content_sha1="", filename=filename)
        return self.load_page(snapshot_id, entry)

    def exists(self, snapshot_id: str) -> bool:
        return os.path.isdir(self._dir(snapshot_id))

    def list_ids(self) -> List[str]:
        return sorted(name for name in os.listdir(self.root)
                      if os.path.isdir(os.path.join(self.root, name)) and name)
