"""Collection runner with checkpoint/resume and pinned rule versions.

A run walks a manifest of (page_id, url) entries and stores each fetched raw
page into the run's snapshot.  Progress is checkpointed after every page (a
single page file plus one appended index line), so an interrupted run can
resume without re-fetching completed pages.  Pages from different boreholes may
arrive interleaved; each page is merged into its borehole in ``page_id`` order
at the end, which keeps complementary measurements, conflict values and
provenance identical to a continuous single-borehole feed.  The rule text is
copied into the run at start time: saving a new rule version while the run is
in progress has no effect on it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .merge import write_snapshot_jsonl
from .rules import Rule
from .sources import PageContent, SnapshotStore, SourceError, fetch_page

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
        # The durable per-page checkpoint is the snapshot itself: one page file
        # plus one appended index line.  run.json is O(manifest) and is written
        # only at terminal transitions, so a crawl never re-serialises either
        # the corpus or the whole manifest once per page.  Resume derives the
        # completed set from the snapshot, not from run["pages"].
        # Status changes are recorded at terminal transitions only (completed /
        # failed).  A crash mid-crawl leaves the last durable state alone; the
        # snapshot index, not run.json, is what resume consults.
        run["status"] = STATUS_RUNNING

        snapshot = self.snapshot_store.load_or_create(run["snapshot_id"])
        # Pages already frozen by an earlier attempt are never re-fetched.
        done_ids = set(snapshot.page_ids())

        rule = Rule.from_text(run["rule_text"])
        try:
            for entry in run["pages"]:
                if entry["page_id"] in done_ids:
                    continue
                if entry["state"] == STATUS_COMPLETED:
                    # Completed in a run record whose snapshot page is gone:
                    # re-fetch rather than silently claiming a page we lack.
                    entry["state"] = STATUS_PENDING
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
                # Persist just this page: the raw HTML is written once and the
                # index line is appended, never the whole corpus rewritten.
                # The page file plus its index line are the durable checkpoint,
                # so neither run.json nor meta.json is rewritten per page.
                snapshot.add_page(page)
                del page
                entry["state"] = STATUS_COMPLETED
                entry["error"] = None
                done_ids.add(entry["page_id"])
                # A retried page that now succeeds clears its failure marker;
                # persist that at once so a later crash cannot resurrect it.
                self.snapshot_store.clear_failures(snapshot)

            output_path = self.store.run_output_path(run_id)
            # Stream: one page's DOM and one borehole document in memory at a time.
            outcome = write_snapshot_jsonl(snapshot, rule, output_path)

            run["status"] = STATUS_COMPLETED
            run["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            run["has_conflicts"] = outcome["has_conflicts"]
            run["has_warnings"] = outcome["has_warnings"]
            run["output_path"] = output_path
            self.store.save_run(run)
            return run
        except Exception as exc:  # noqa: BLE001 - mark run failed, keep checkpoint
            run["status"] = STATUS_FAILED
            run["error"] = f"{type(exc).__name__}: {exc}"
            self.store.save_run(run)
            raise

    # ------------------------------------------------------------- replay ---
    def replay(self, run_id: str, rule_text: Optional[str] = None,
               new_run_id: Optional[str] = None) -> Dict[str, Any]:
        """Re-run extraction on the frozen snapshot, optionally with new rules."""
        original = self.store.load_run(run_id)
        snapshot = self.snapshot_store.load(original["snapshot_id"])
        text = rule_text if rule_text is not None else original["rule_text"]
        rule = Rule.from_text(text)
        replay_id = new_run_id or f"{run_id}-replay-{int(time.time() * 1000)}"
        output_path = self.store.run_output_path(replay_id)
        outcome = write_snapshot_jsonl(snapshot, rule, output_path)
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
            "has_conflicts": outcome["has_conflicts"],
            "has_warnings": outcome["has_warnings"],
            "output_path": output_path,
            "error": None,
            "replayed_from": run_id,
            "snapshot_complete": snapshot.is_complete,
        }
        self.store.save_run(replay_run)
        return replay_run
