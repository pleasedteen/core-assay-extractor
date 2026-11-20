"""Collection runner with checkpoint/resume and pinned rule versions.

A run walks a manifest of (page_id, url) entries and stores each fetched raw
page into the run's snapshot.  Progress is checkpointed after every page, so an
interrupted run can resume without re-fetching completed pages.  The rule text
is copied into the run at start time: saving a new rule version while the run
is in progress has no effect on it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .extractor import PageExtraction, extract_page
from .merge import BoreholeDocument, has_blocking_conflicts, merge_extractions, write_jsonl
from .rules import Rule
from .sources import PageContent, Snapshot, SnapshotStore, SourceError, fetch_page

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


@dataclass
class RunConfig:
    run_id: str
    rule_id: str
    rule_text: str
    pages: List[Dict[str, str]]            # [{"page_id": ..., "url": ...}]
    note: str = ""


class RunEngine:
    def __init__(self, store, snapshot_store: SnapshotStore,
                 fetcher: Callable[[str, str], PageContent] = fetch_page):
        self.store = store
        self.snapshot_store = snapshot_store
        self.fetcher = fetcher

    # ----------------------------------------------------------- decoding ---
    @staticmethod
    def _apply_encoding(page: PageContent, rule: Rule) -> PageContent:
        """Decode a frozen page with the rule's encoding choice.

        The raw bytes are always retained, so a rule can correct the encoding
        of one page of a mixed batch, and an undecodable page is kept as bytes
        plus a recorded failure instead of losing data.
        """
        if page.is_legacy:
            return page
        return page.with_encoding(rule.encoding)

    # ----------------------------------------------------------- lifecycle --
    def start(self, config: RunConfig) -> Dict[str, Any]:
        run = {
            "run_id": config.run_id,
            "rule_id": config.rule_id,
            "rule_text": config.rule_text,
            "note": config.note,
            "status": STATUS_PENDING,
            "pages": [
                {"page_id": page["page_id"], "url": page["url"], "state": STATUS_PENDING, "error": None}
                for page in config.pages
            ],
            "snapshot_id": f"snap-{config.run_id}",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "finished_at": None,
            "has_conflicts": False,
            "has_warnings": False,
            "output_path": None,
            "error": None,
        }
        self.store.save_run(run)
        return run

    def run(self, run_id: str) -> Dict[str, Any]:
        """Execute (or resume) the run until completion or first fetch error."""
        run = self.store.load_run(run_id)
        run["status"] = STATUS_RUNNING
        self.store.save_run(run)

        snapshot = Snapshot(snapshot_id=run["snapshot_id"])
        if self.snapshot_store.exists(snapshot.snapshot_id):
            snapshot = self.snapshot_store.load(snapshot.snapshot_id)
        done_ids = {page.page_id for page in snapshot.pages}

        rule = Rule.from_text(run["rule_text"])
        try:
            for entry in run["pages"]:
                if entry["page_id"] in done_ids or entry["state"] == STATUS_COMPLETED:
                    continue
                try:
                    page = self.fetcher(entry["page_id"], entry["url"])
                except SourceError as exc:
                    entry["state"] = STATUS_FAILED
                    entry["error"] = str(exc)
                    snapshot.failures[entry["page_id"]] = str(exc)
                    self.snapshot_store.save(snapshot)
                    run["status"] = STATUS_FAILED
                    run["error"] = str(exc)
                    self.store.save_run(run)
                    return run
                # A retried page that now succeeds clears its failure marker.
                snapshot.failures.pop(entry["page_id"], None)
                page = self._apply_encoding(page, rule)
                snapshot.pages.append(page)
                entry["state"] = STATUS_COMPLETED
                entry["error"] = None
                done_ids.add(page.page_id)
                # Checkpoint after every page: resumption needs no re-fetch.
                self.snapshot_store.save(snapshot)
                self.store.save_run(run)

            pages = [self._apply_encoding(page, rule) for page in
                     sorted(snapshot.pages, key=lambda p: p.page_id)]
            extractions = [extract_page(page, rule) for page in pages]
            documents = merge_extractions(extractions)
            output_path = self.store.run_output_path(run_id)
            write_jsonl(documents, output_path)

            run["status"] = STATUS_COMPLETED
            run["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            run["has_conflicts"] = has_blocking_conflicts(documents)
            run["has_warnings"] = any(doc.warnings for doc in documents)
            run["output_path"] = output_path
            run["page_encodings"] = [page_decoding_record(page) for page in pages]
            self.store.save_run(run)
            self.snapshot_store.save(snapshot)
            return run
        except Exception as exc:  # noqa: BLE001 - mark run failed, keep checkpoint
            run["status"] = STATUS_FAILED
            run["error"] = f"{type(exc).__name__}: {exc}"
            self.store.save_run(run)
            raise

    # ------------------------------------------------------------- replay ---
    def replay(self, run_id: str, rule_text: Optional[str] = None,
               new_run_id: Optional[str] = None) -> Dict[str, Any]:
        """Re-run extraction on the frozen snapshot, optionally with new rules.

        The snapshot keeps raw bytes, so a candidate rule can also correct a
        page's encoding; this works even when the source site is gone.  Legacy
        snapshots that only hold decoded text replay with that original text.
        """
        original = self.store.load_run(run_id)
        snapshot = self.snapshot_store.load(original["snapshot_id"])
        text = rule_text if rule_text is not None else original["rule_text"]
        rule = Rule.from_text(text)
        pages = [self._apply_encoding(page, rule) for page in
                 sorted(snapshot.pages, key=lambda p: p.page_id)]
        extractions = [extract_page(page, rule) for page in pages]
        documents = merge_extractions(extractions)
        replay_id = new_run_id or f"{run_id}-replay-{int(time.time() * 1000)}"
        output_path = self.store.run_output_path(replay_id)
        write_jsonl(documents, output_path)
        replay_run = {
            "run_id": replay_id,
            "rule_id": original["rule_id"] if rule_text is None else f"{original['rule_id']}:candidate",
            "rule_text": text,
            "note": f"replay of {run_id} on snapshot {snapshot.snapshot_id}",
            "status": STATUS_COMPLETED,
            "pages": list(original["pages"]),
            "snapshot_id": snapshot.snapshot_id,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "has_conflicts": has_blocking_conflicts(documents),
            "has_warnings": any(doc.warnings for doc in documents),
            "output_path": output_path,
            "error": None,
            "replayed_from": run_id,
            "snapshot_complete": snapshot.is_complete,
            "page_encodings": [page_decoding_record(page) for page in pages],
        }
        self.store.save_run(replay_run)
        return replay_run


def page_decoding_record(page: PageContent) -> Dict[str, Any]:
    """A compact, traceable record of how one page's text was obtained."""
    record = {
        "page_id": page.page_id,
        "url": page.url,
        "content_sha1": page.content_sha1,
        "encoding": page.encoding,
        "encoding_source": page.encoding_source,
        "declared_encoding": page.declared_encoding,
        "status": page.status,
        "overridden": page.encoding_overridden,
    }
    if page.is_legacy:
        record["legacy_text_only"] = True
    if page.decode_error:
        record["decode_error"] = page.decode_error
    return record
