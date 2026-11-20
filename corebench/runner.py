"""Collection runner with checkpoint/resume and pinned rule versions.

A run walks a manifest of (page_id, url) entries and stores each fetched raw
page into the run's snapshot.  Progress is checkpointed after every page, so an
interrupted run can resume without re-fetching completed pages.  The rule text
is copied into the run at start time: saving a new rule version while the run
is in progress has no effect on it.

HTTP 429 answers are handled separately from ordinary fetch errors: the page
is deferred, the shared per-site hold (persisted in the data directory) is
honoured without sending further requests to that site, and other sites of
the same run keep crawling.  Once the retry-after moment passes, only the
still-missing pages are fetched.  Ordinary failures still fail the run and
wait for a manual resume.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .extractor import PageExtraction, extract_page
from .merge import BoreholeDocument, has_blocking_conflicts, merge_extractions, write_jsonl
from .rules import Rule
from .sources import (
    Fetcher,
    PageContent,
    RateLimitedError,
    RateLimitStore,
    Snapshot,
    SnapshotStore,
    SourceError,
)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_WAITING = "waiting"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


@dataclass
class RunConfig:
    run_id: str
    rule_id: str
    rule_text: str
    pages: List[Dict[str, str]]            # [{"page_id": ..., "url": ...}]
    note: str = ""


class RunEngine:
    def __init__(self, store, snapshot_store: SnapshotStore,
                 fetcher: Optional[Callable[[str, str], PageContent]] = None,
                 rate_limits: Optional[RateLimitStore] = None,
                 clock: Callable[[], float] = time.time,
                 sleeper: Callable[[float], None] = time.sleep,
                 max_wait_seconds: float = 3600.0):
        self.store = store
        self.snapshot_store = snapshot_store
        self.clock = clock
        self.sleeper = sleeper
        # Upper bound on the total time one run() call spends sleeping on 429
        # holds; beyond it the run stays resumable in "waiting" state.
        self.max_wait_seconds = max_wait_seconds
        if rate_limits is None:
            rate_limits = RateLimitStore(os.path.join(store.root, "rate_limits.json"),
                                         clock=clock)
        self.rate_limits = rate_limits
        self.fetcher = fetcher if fetcher is not None else Fetcher(rate_limits, clock=clock)

    # ----------------------------------------------------------- lifecycle --
    def start(self, config: RunConfig) -> Dict[str, Any]:
        run = {
            "run_id": config.run_id,
            "rule_id": config.rule_id,
            "rule_text": config.rule_text,
            "note": config.note,
            "status": STATUS_PENDING,
            "pages": [
                {"page_id": page["page_id"], "url": page["url"], "state": STATUS_PENDING,
                 "error": None, "wait_until": None}
                for page in config.pages
            ],
            "snapshot_id": f"snap-{config.run_id}",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "finished_at": None,
            "waiting_until": None,
            "has_conflicts": False,
            "has_warnings": False,
            "output_path": None,
            "error": None,
        }
        self.store.save_run(run)
        return run

    def run(self, run_id: str) -> Dict[str, Any]:
        """Execute (or resume) the run.

        Pages of a site that answered HTTP 429 are deferred without any new
        request until the shared retry-after moment passes; other sites of
        the same run keep crawling meanwhile.  Ordinary fetch errors still
        fail the run for a manual resume.
        """
        run = self.store.load_run(run_id)
        run["status"] = STATUS_RUNNING
        run["waiting_until"] = None
        self.store.save_run(run)

        snapshot = Snapshot(snapshot_id=run["snapshot_id"])
        if self.snapshot_store.exists(snapshot.snapshot_id):
            snapshot = self.snapshot_store.load(snapshot.snapshot_id)
        done_ids = {page.page_id for page in snapshot.pages}

        rule = Rule.from_text(run["rule_text"])
        try:
            waited = 0.0
            while True:
                waiting = self._fetch_pass(run, snapshot, done_ids)
                if waiting is None:  # ordinary fetch failure: manual resume
                    return run
                if not waiting:
                    break
                deadline = min(self._entry_deadline(entry) or self.clock()
                               for entry in waiting)
                wait_for = max(0.0, deadline - self.clock())
                if waited + wait_for > self.max_wait_seconds:
                    # Stay resumable instead of sleeping beyond the budget.
                    run["status"] = STATUS_WAITING
                    run["waiting_until"] = _iso(deadline)
                    self.store.save_run(run)
                    return run
                run["status"] = STATUS_WAITING
                run["waiting_until"] = _iso(deadline)
                self.store.save_run(run)
                self.sleeper(wait_for)
                waited += wait_for
                run["status"] = STATUS_RUNNING
                run["waiting_until"] = None
                self.store.save_run(run)

            extractions = [extract_page(page, rule) for page in
                           sorted(snapshot.pages, key=lambda p: p.page_id)]
            documents = merge_extractions(extractions)
            output_path = self.store.run_output_path(run_id)
            write_jsonl(documents, output_path)

            run["status"] = STATUS_COMPLETED
            run["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            run["has_conflicts"] = has_blocking_conflicts(documents)
            run["has_warnings"] = any(doc.warnings for doc in documents)
            run["output_path"] = output_path
            self.store.save_run(run)
            self.snapshot_store.save(snapshot)
            return run
        except Exception as exc:  # noqa: BLE001 - mark run failed, keep checkpoint
            run["status"] = STATUS_FAILED
            run["error"] = f"{type(exc).__name__}: {exc}"
            self.store.save_run(run)
            raise

    def _fetch_pass(self, run: Dict[str, Any], snapshot: Snapshot,
                    done_ids: set) -> Optional[List[Dict[str, Any]]]:
        """One crawl pass over unfinished pages.

        Returns the entries held by a 429 retry-after, or None when an
        ordinary fetch error failed the run (the manual-resume path).
        """
        waiting: List[Dict[str, Any]] = []
        for entry in run["pages"]:
            if entry["page_id"] in done_ids or entry["state"] == STATUS_COMPLETED:
                continue
            deadline = self._entry_deadline(entry)
            if deadline is not None and deadline > self.clock():
                # The site is on a shared retry-after hold: zero requests go out.
                entry["state"] = STATUS_WAITING
                waiting.append(entry)
                continue
            entry["wait_until"] = None
            try:
                page = self.fetcher(entry["page_id"], entry["url"])
            except RateLimitedError as exc:
                # Shared, persisted hold: later runs and restarts honour the
                # same absolute moment, including redirect targets.
                self.rate_limits.record(exc.url or entry["url"], exc.retry_at)
                entry["state"] = STATUS_WAITING
                entry["wait_until"] = exc.retry_at
                entry["error"] = None
                waiting.append(entry)
                continue
            except SourceError as exc:
                entry["state"] = STATUS_FAILED
                entry["error"] = str(exc)
                snapshot.failures[entry["page_id"]] = str(exc)
                self.snapshot_store.save(snapshot)
                run["status"] = STATUS_FAILED
                run["error"] = str(exc)
                self.store.save_run(run)
                return None
            # A retried page that now succeeds clears its failure marker.
            snapshot.failures.pop(entry["page_id"], None)
            snapshot.pages.append(page)
            entry["state"] = STATUS_COMPLETED
            entry["error"] = None
            done_ids.add(page.page_id)
            # Checkpoint after every page: resumption needs no re-fetch.
            self.snapshot_store.save(snapshot)
            self.store.save_run(run)
        if waiting:
            self.store.save_run(run)
        return waiting

    def _entry_deadline(self, entry: Dict[str, Any]) -> Optional[float]:
        """Active hold for this entry: shared site hold or its own 429 moment."""
        deadlines = []
        held = self.rate_limits.wait_until(entry["url"])
        if held is not None:
            deadlines.append(held)
        own = entry.get("wait_until")
        if own:
            deadlines.append(float(own))
        return max(deadlines) if deadlines else None

    # ------------------------------------------------------------- replay ---
    def replay(self, run_id: str, rule_text: Optional[str] = None,
               new_run_id: Optional[str] = None) -> Dict[str, Any]:
        """Re-run extraction on the frozen snapshot, optionally with new rules."""
        original = self.store.load_run(run_id)
        snapshot = self.snapshot_store.load(original["snapshot_id"])
        text = rule_text if rule_text is not None else original["rule_text"]
        rule = Rule.from_text(text)
        extractions = [extract_page(page, rule) for page in
                       sorted(snapshot.pages, key=lambda p: p.page_id)]
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
        }
        self.store.save_run(replay_run)
        return replay_run
