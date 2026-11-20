"""Collection runner with checkpoint/resume and pinned rule versions.

A run walks a manifest of (page_id, url) entries and stores each fetched raw
page into the run's snapshot.  Progress is checkpointed after every page, so an
interrupted run can resume without re-fetching completed pages.  The rule text
is copied into the run at start time: saving a new rule version while the run
is in progress has no effect on it.

Run ids are durable identities, not disposable handles:

* Re-using a run id with the **same request** (same operation kind, rule text
  and page manifest) never overwrites anything -- it re-attaches to the stored
  run, so a timed-out request or a service restart simply resumes the original
  run from its checkpoint.
* Re-using an id with a **different request**, or any id that backs a confirmed
  baseline, raises :class:`~corebench.errors.RunConflictError` instead of
  silently replacing state.
* Each run id has its own lock: a slow page only blocks workers for that run;
  other runs keep progressing, and progress can be queried while a run is
  blocked on a fetch.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .errors import RunConflictError
from .extractor import PageExtraction, extract_page
from .merge import BoreholeDocument, has_blocking_conflicts, merge_extractions, write_jsonl
from .rules import Rule
from .sources import PageContent, Snapshot, SnapshotStore, SourceError, fetch_page

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

KIND_COLLECT = "collect"
KIND_REPLAY = "replay"


@dataclass
class RunConfig:
    run_id: str
    rule_id: str
    rule_text: str
    pages: List[Dict[str, str]]            # [{"page_id": ..., "url": ...}]
    note: str = ""


def _digest(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def collect_signature(rule_text: str, pages: List[Dict[str, str]]) -> str:
    """Stable identity of a collection request (rule text + page manifest)."""
    manifest = sorted((page["page_id"], page["url"]) for page in pages)
    return _digest({"kind": KIND_COLLECT, "rule_text": rule_text, "pages": manifest})


def replay_signature(source_run_id: str, snapshot_id: str, rule_text: str) -> str:
    return _digest({"kind": KIND_REPLAY, "source_run_id": source_run_id,
                    "snapshot_id": snapshot_id, "rule_text": rule_text})


class RunEngine:
    def __init__(self, store, snapshot_store: SnapshotStore,
                 fetcher: Callable[[str, str], PageContent] = fetch_page):
        self.store = store
        self.snapshot_store = snapshot_store
        self.fetcher = fetcher
        # One lock per run id: a slow page blocks only this run, never others.
        self._run_locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # run_id -> page_id currently being fetched (for live progress queries).
        self._active_pages: Dict[str, Optional[str]] = {}

    def _lock_for(self, run_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._run_locks.setdefault(run_id, threading.Lock())

    def _guard_baseline(self, run_id: str) -> None:
        baseline_id = f"baseline-{run_id}"
        if hasattr(self.store, "baseline_exists"):
            protected = self.store.baseline_exists(baseline_id)
        else:  # pragma: no cover - stores always provide the helper now
            import os
            protected = os.path.exists(os.path.join(
                self.store.baselines_dir, f"{baseline_id}.json"))
        if protected:
            raise RunConflictError(
                f"run id {run_id!r} backs confirmed baseline {baseline_id!r}; "
                "the confirmed baseline cannot be overwritten")

    # ----------------------------------------------------------- lifecycle --
    def start(self, config: RunConfig) -> Dict[str, Any]:
        """Create a collection run, or re-attach to an identical retry.

        Same request + same id returns the stored run untouched (a retried
        request after a timeout or a restart resumes that run).  A different
        rule/manifest, a replay id, or a baseline-protected id is a conflict.
        """
        signature = collect_signature(config.rule_text, config.pages)
        if self.store.run_exists(config.run_id):
            self._guard_baseline(config.run_id)
            existing = self.store.load_run(config.run_id)
            if existing.get("request_kind") != KIND_COLLECT:
                raise RunConflictError(
                    f"run id {config.run_id!r} is already bound to a "
                    f"{existing.get('request_kind', 'unknown')} operation; "
                    "choose a different run id")
            if existing.get("request_signature") != signature:
                raise RunConflictError(
                    f"run id {config.run_id!r} already exists with a different "
                    "rule or page manifest; refusing to overwrite it")
            # Identical retry: re-attach to the original run and its checkpoint.
            return existing

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
            "request_kind": KIND_COLLECT,
            "request_signature": signature,
        }
        self.store.save_run(run)
        return run

    def run(self, run_id: str) -> Dict[str, Any]:
        """Execute (or resume) the run until completion or first fetch error.

        Concurrent calls for the same run id share one execution: the second
        caller waits for the in-flight pass and gets back the resulting state,
        so a duplicated "resume" request never double-fetches a page.  Calls for
        different run ids proceed independently.
        """
        run = self.store.load_run(run_id)
        # A completed run is immutable work; re-issuing run() just reports it.
        if run["status"] == STATUS_COMPLETED:
            return run
        with self._lock_for(run_id):
            return self._run_locked(run_id)

    def _run_locked(self, run_id: str) -> Dict[str, Any]:
        run = self.store.load_run(run_id)
        if run["status"] == STATUS_COMPLETED:
            return run
        run["status"] = STATUS_RUNNING
        self.store.save_run(run)

        snapshot = Snapshot(snapshot_id=run["snapshot_id"])
        if self.snapshot_store.exists(snapshot.snapshot_id):
            snapshot = self.snapshot_store.load(snapshot.snapshot_id)
        done_ids = {page.page_id for page in snapshot.pages}

        rule = Rule.from_text(run["rule_text"])
        self._active_pages[run_id] = None
        try:
            for entry in run["pages"]:
                if entry["page_id"] in done_ids or entry["state"] == STATUS_COMPLETED:
                    continue
                self._active_pages[run_id] = entry["page_id"]
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
                snapshot.pages.append(page)
                entry["state"] = STATUS_COMPLETED
                entry["error"] = None
                done_ids.add(page.page_id)
                # Checkpoint after every page: resumption needs no re-fetch.
                self.snapshot_store.save(snapshot)
                self.store.save_run(run)

            extractions = [extract_page(page, rule) for page in
                           sorted(snapshot.pages, key=lambda p: p.page_id)]
            documents = merge_extractions(extractions)
            output_path = self.store.run_output_path(run_id)
            # Atomic publish: if the process dies here, no partial output exists
            # and the run stays non-completed, so nothing unpublished can be diffed.
            write_jsonl(documents, output_path)

            run["status"] = STATUS_COMPLETED
            run["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            run["has_conflicts"] = has_blocking_conflicts(documents)
            run["has_warnings"] = any(doc.warnings for doc in documents)
            run["output_path"] = output_path
            run["error"] = None
            self.store.save_run(run)
            self.snapshot_store.save(snapshot)
            return run
        except Exception as exc:  # noqa: BLE001 - mark run failed, keep checkpoint
            run["status"] = STATUS_FAILED
            run["error"] = f"{type(exc).__name__}: {exc}"
            self.store.save_run(run)
            raise
        finally:
            self._active_pages.pop(run_id, None)

    def progress(self, run_id: str) -> Dict[str, Any]:
        """Checkpoint view of a run, safe to call while a fetch is in flight."""
        run = self.store.load_run(run_id)
        pages = run.get("pages", [])
        completed = sum(1 for page in pages if page["state"] == STATUS_COMPLETED)
        failed = sum(1 for page in pages if page["state"] == STATUS_FAILED)
        total = len(pages)
        return {
            "run_id": run_id,
            "status": run["status"],
            "total_pages": total,
            "completed_pages": completed,
            "failed_pages": failed,
            "pending_pages": total - completed - failed,
            "in_flight_page": self._active_pages.get(run_id),
            "percent_done": round(100.0 * completed / total, 1) if total else 100.0,
            "error": run.get("error"),
        }

    # ------------------------------------------------------------- replay ---
    def replay(self, run_id: str, rule_text: Optional[str] = None,
               new_run_id: Optional[str] = None) -> Dict[str, Any]:
        """Re-run extraction on the frozen snapshot, optionally with new rules."""
        original = self.store.load_run(run_id)
        snapshot = self.snapshot_store.load(original["snapshot_id"])
        text = rule_text if rule_text is not None else original["rule_text"]
        replay_id = new_run_id or f"{run_id}-replay-{int(time.time() * 1000)}"
        signature = replay_signature(run_id, snapshot.snapshot_id, text)

        if self.store.run_exists(replay_id):
            self._guard_baseline(replay_id)
            existing = self.store.load_run(replay_id)
            if existing.get("request_kind") == KIND_REPLAY \
                    and existing.get("request_signature") == signature:
                # Same replay request retried after a timeout: return the stored one.
                return existing
            raise RunConflictError(
                f"run id {replay_id!r} already exists with a different operation, "
                "rule text or source snapshot; choose a different new run id")

        rule = Rule.from_text(text)
        extractions = [extract_page(page, rule) for page in
                       sorted(snapshot.pages, key=lambda p: p.page_id)]
        documents = merge_extractions(extractions)
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
            "request_kind": KIND_REPLAY,
            "request_signature": signature,
        }
        self.store.save_run(replay_run)
        return replay_run
