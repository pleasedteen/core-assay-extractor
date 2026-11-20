"""Page sources: HTTP fetch, local files, and frozen snapshots.

A *snapshot* freezes the raw HTML of every page of a run under a content hash,
so a rule replay always sees exactly the same pages.  Fetching supports both
``http(s)://`` URLs and ``file://`` / plain local paths -- the automated
checks never need a real browser or an external site.

HTTP 429 ("too many requests") answers carrying a ``Retry-After`` hint are a
first-class signal: the per-site hold is kept in a shared, file-backed
:class:`RateLimitStore`, so every run -- and every process started later on
the same data directory -- waits until the promised moment before touching
that site again.  Redirects into a held site are held too.
"""
from __future__ import annotations

import datetime
import email.utils
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

FETCH_TIMEOUT = 10.0
# Fallback hold when a 429 arrives without a usable Retry-After hint.
DEFAULT_RETRY_AFTER = 60.0


class SourceError(RuntimeError):
    pass


class RateLimitedError(SourceError):
    """The site asked to be left alone until ``retry_at`` (epoch seconds)."""

    def __init__(self, url: str, retry_at: float, retry_after: float):
        self.url = url
        self.retry_at = retry_at
        self.retry_after = retry_after
        moment = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(retry_at))
        super().__init__(
            f"rate limited for {url}: retry after {retry_after:.1f}s (at {moment})")


def parse_retry_after(value: Optional[str], now: Optional[float] = None) -> float:
    """Absolute epoch deadline from a Retry-After header (delta or HTTP-date)."""
    now = time.time() if now is None else now
    text = (value or "").strip()
    if text:
        try:
            return now + max(0.0, float(text))
        except ValueError:
            pass
        try:
            moment = email.utils.parsedate_to_datetime(text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
            return max(now, moment.timestamp())
        except (TypeError, ValueError, OverflowError):
            pass
    return now + DEFAULT_RETRY_AFTER


class RateLimitStore:
    """Shared per-site retry-after holds, persisted as one small JSON file.

    Every run engine pointed at the same data directory sees the same holds,
    and a hold recorded before a service restart still fires at its original
    absolute moment afterwards (deadlines are stored as epoch seconds).
    """

    def __init__(self, path: str, clock=time.time):
        self.path = os.path.abspath(path)
        self.clock = clock
        self._lock = threading.Lock()
        self._limits: Dict[str, float] = {}
        self._limits = self._prune(self._read_disk())

    @staticmethod
    def site_key(url: str) -> Optional[str]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return parsed.netloc  # host[:port] identifies "the site"
        return None

    def wait_until(self, url: str) -> Optional[float]:
        """Current hold deadline for the URL's site, or None if it may be fetched."""
        key = self.site_key(url)
        if key is None:
            return None
        with self._lock:
            self._merge_from_disk()
            deadline = self._limits.get(key)
            if deadline is not None and deadline > self.clock():
                return deadline
            return None

    def record(self, url: str, retry_at: float) -> None:
        """Remember (and persist) a hold for the URL's site; the later deadline wins."""
        key = self.site_key(url)
        if key is None:
            return
        with self._lock:
            self._merge_from_disk()
            self._limits[key] = max(self._limits.get(key, 0.0), float(retry_at))
            self._limits = self._prune(self._limits)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._limits, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)

    def _read_disk(self) -> Dict[str, float]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return {}
        return {str(key): float(value) for key, value in payload.items()}

    def _merge_from_disk(self) -> None:
        merged = self._read_disk()
        for key, value in self._limits.items():
            merged[key] = max(merged.get(key, 0.0), value)
        self._limits = self._prune(merged)

    def _prune(self, limits: Dict[str, float]) -> Dict[str, float]:
        now = self.clock()
        return {key: value for key, value in limits.items() if value > now}


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


class Fetcher:
    """Default page fetcher; honours the shared per-site rate-limit holds.

    Before any request goes out, the URL's site is checked against the
    registry: while a hold is active a :class:`RateLimitedError` is raised
    without touching the network.  A 429 response records the hold for the
    site that actually answered (after any redirects), and a redirect into a
    held site is stopped before the target is requested.
    """

    def __init__(self, rate_limits: Optional[RateLimitStore] = None,
                 timeout: float = FETCH_TIMEOUT, clock=time.time):
        self.rate_limits = rate_limits
        self.timeout = timeout
        self.clock = clock

    def __call__(self, page_id: str, url: str) -> "PageContent":
        if not url.startswith(("http://", "https://")):
            return _read_local(page_id, url)
        self._raise_if_held(url)
        opener = self._opener()
        request = urllib.request.Request(url, headers={"User-Agent": "corebench/1.0"})
        try:
            with opener.open(request, timeout=self.timeout) as response:  # noqa: S310 (test server)
                charset = response.headers.get_content_charset() or "utf-8"
                html = response.read().decode(charset, errors="replace")
                status = response.status
        except RateLimitedError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise self._rate_limited(exc, url) from exc
            raise SourceError(f"fetch failed for {url}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - surfaced as SourceError
            raise SourceError(f"fetch failed for {url}: {exc}") from exc
        if status >= 400:
            raise SourceError(f"fetch failed for {url}: HTTP {status}")
        return PageContent.make(page_id, url, html)

    def _raise_if_held(self, url: str) -> None:
        if self.rate_limits is None:
            return
        deadline = self.rate_limits.wait_until(url)
        if deadline is not None:
            raise RateLimitedError(url, deadline, deadline - self.clock())

    def _rate_limited(self, exc: urllib.error.HTTPError, url: str) -> RateLimitedError:
        now = self.clock()
        retry_at = parse_retry_after(exc.headers.get("Retry-After"), now)
        effective_url = getattr(exc, "url", None) or url
        if self.rate_limits is not None:
            self.rate_limits.record(effective_url, retry_at)
        return RateLimitedError(effective_url, retry_at, retry_at - now)

    def _opener(self) -> urllib.request.OpenerDirector:
        store = self.rate_limits
        clock = self.clock

        class _HoldGuardRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                # A redirect into a held site is held exactly like a direct hit:
                # the target is never requested while the hold is active.
                if store is not None:
                    deadline = store.wait_until(newurl)
                    if deadline is not None:
                        raise RateLimitedError(newurl, deadline, deadline - clock())
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        return urllib.request.build_opener(_HoldGuardRedirectHandler())


def _read_local(page_id: str, url: str) -> "PageContent":
    path = url[7:] if url.startswith("file://") else url
    try:
        with open(path, "r", encoding="utf-8") as handle:
            html = handle.read()
    except OSError as exc:
        raise SourceError(f"read failed for {path}: {exc}") from exc
    return PageContent.make(page_id, url, html)


def fetch_page(page_id: str, url: str, timeout: float = FETCH_TIMEOUT) -> "PageContent":
    """One-off fetch without a shared hold registry (a 429 still raises)."""
    return Fetcher(timeout=timeout)(page_id, url)


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
