"""Large-batch collection: paged persistence, interleaved pages and budgets.

The crawl no longer rewrites a single snapshot blob nor loads the whole corpus
at once.  These tests pin the behaviour that makes that safe:

* pages of different boreholes may arrive interleaved, but each borehole still
  merges exactly as it would for a continuous feed (values, conflicts,
  provenance);
* replay reads the frozen per-page snapshot, not the live site;
* growing the corpus four-fold (more boreholes, same per-borehole size) keeps
  Python peak memory within 2x of the small batch for both collection and
  replay, and keeps snapshot + checkpoint writes within 3x the raw HTML;
* resuming only fetches pages that never reached disk.
"""
import gc
import json
import os
import random
import tempfile
import tracemalloc
import unittest

from tests._helpers import read_jsonl, rule_text, scaled_dataset, write_scaled_site
from corebench.merge import merge_extractions, write_snapshot_jsonl
from corebench.rules import Rule
from corebench.runner import RunConfig, RunEngine
from corebench.sources import SnapshotStore, fetch_page
from corebench.storage import WorkbenchStore


def _peak_memory(action) -> int:
    gc.collect()
    tracemalloc.start()
    try:
        action()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _crawl(tmp: str, manifest, run_id: str = "crawl"):
    store = WorkbenchStore(tmp)
    snapshots = SnapshotStore(os.path.join(tmp, "snapshots"))
    engine = RunEngine(store, snapshots)
    rule = store.save_rule_version("drill-archive", rule_text(1), "v1")
    engine.start(RunConfig(run_id, rule["rule_id"], rule["text"], manifest))
    return store, snapshots, engine


def _collect_peak(manifest) -> int:
    tmp = tempfile.mkdtemp()

    # Warm up module-level caches (regex, rule parsing) outside the measured
    # region so the small batch is not charged with one-off startup costs.
    _store, _snapshots, warm_engine = _crawl(tempfile.mkdtemp(), manifest)
    warm_engine.run("crawl")

    def action():
        _store, _snapshots, engine = _crawl(tmp, manifest)
        engine.run("crawl")

    return _peak_memory(action)


def _replay_peak(manifest) -> int:
    tmp = tempfile.mkdtemp()
    store, _snapshots, engine = _crawl(tmp, manifest)
    engine.run("crawl")

    # Warm up caches with a throwaway replay before measuring.
    engine.replay("crawl", new_run_id="crawl-warmup")

    def action():
        engine.replay("crawl", new_run_id="crawl-replay")

    return _peak_memory(action)


class InterleavedMergeEquivalenceTest(unittest.TestCase):
    """Interleaved pages must merge exactly like a continuous feed."""

    def setUp(self):
        self.manifest = write_scaled_site(tempfile.mkdtemp(), factor=2)
        self.rule = Rule.from_text(rule_text(1))

    def _merged_rows(self, snapshot) -> list:
        path = os.path.join(tempfile.mkdtemp(), "out.jsonl")
        write_snapshot_jsonl(snapshot, self.rule, path)
        return read_jsonl(path)

    def test_interleaved_feed_matches_continuous_per_borehole(self):
        # Fetch in a deliberately mixed order: boreholes and copies alternate.
        shuffled = list(self.manifest)
        random.Random(11).shuffle(shuffled)
        snapshot = SnapshotStore(tempfile.mkdtemp()).load_or_create("snap")
        for entry in shuffled:
            snapshot.add_page(fetch_page(entry["page_id"], entry["url"]))

        streamed = self._merged_rows(snapshot)
        # Reference: the original in-memory batch merge over the same pages.
        from corebench.extractor import extract_page
        reference = merge_extractions(
            [extract_page(page, self.rule) for page in snapshot.iter_pages()])
        reference_rows = [doc.to_jsonl_obj() for doc in reference]

        self.assertEqual([row["borehole_id"] for row in streamed],
                         [row["borehole_id"] for row in reference_rows])
        for streamed_row, reference_row in zip(streamed, reference_rows):
            self.assertEqual(json.dumps(streamed_row, sort_keys=True, ensure_ascii=False),
                             json.dumps(reference_row, sort_keys=True, ensure_ascii=False))

    def test_repeated_page_dropped_and_snapshot_order_independent(self):
        snapshot = SnapshotStore(tempfile.mkdtemp()).load_or_create("snap")
        first = fetch_page(self.manifest[0]["page_id"], self.manifest[0]["url"])
        snapshot.add_page(first)
        snapshot.add_page(fetch_page(self.manifest[0]["page_id"], self.manifest[0]["url"]))
        self.assertEqual(snapshot.page_ids(), [first.page_id])
        for entry in self.manifest[1:]:
            snapshot.add_page(fetch_page(entry["page_id"], entry["url"]))
        forward = self._merged_rows(snapshot)

        # Same pages added in reverse order produce byte-identical output.
        reverse_snapshot = SnapshotStore(tempfile.mkdtemp()).load_or_create("snap")
        for entry in reversed(self.manifest):
            reverse_snapshot.add_page(fetch_page(entry["page_id"], entry["url"]))
        self.assertEqual(self._merged_rows(reverse_snapshot), forward)

    def test_conflict_values_and_provenance_unchanged_by_interleaving(self):
        snapshot = SnapshotStore(tempfile.mkdtemp()).load_or_create(
            "snap")
        shuffled = list(self.manifest)
        random.Random(3).shuffle(shuffled)
        for entry in shuffled:
            snapshot.add_page(fetch_page(entry["page_id"], entry["url"]))
        rows = {row["borehole_id"]: row for row in self._merged_rows(snapshot)}

        zk01 = rows["ZK01"]
        self.assertEqual(len(zk01["conflicts"]), 1)
        conflict = zk01["conflicts"][0]
        self.assertEqual(conflict["identity"], "sample=S102|analyte=Au|method=FA")
        # Value pair keeps p1-then-p2 order even though p2 may be stored first.
        self.assertEqual([item["value"] for item in conflict["values"]], [0.008, 0.012])
        self.assertEqual({source["page_id"] for source in conflict["sources"]},
                         {"zk01-p1", "zk01-p2"})
        sample = next(s for s in zk01["samples"] if s["identity"] == "sample_id=S101")
        self.assertEqual(sample["provenance"]["page_ids"], ["zk01-p1", "zk01-p2"])
        self.assertEqual(sample["provenance"]["also_seen_in"], "zk01-p2")
        # Each copied borehole reproduces the same conflict structure.
        for copy_index in (1,):
            copied = rows[f"ZK01-c{copy_index}"]
            self.assertEqual(len(copied["conflicts"]), 1)
            self.assertEqual(
                {(source["page_id"]) for source in copied["conflicts"][0]["sources"]},
                {f"zk01-p1-c{copy_index}", f"zk01-p2-c{copy_index}"})

    def test_replay_uses_frozen_pages_not_live_source(self):
        tmp = tempfile.mkdtemp()
        store, snapshots, engine = _crawl(tmp, self.manifest)
        run = engine.run("crawl")
        original = read_jsonl(run["output_path"])
        # Delete every source file: replay must still see the frozen HTML.
        for entry in self.manifest:
            os.remove(entry["url"])
        replay = engine.replay("crawl", new_run_id="after-delete")
        self.assertEqual(read_jsonl(replay["output_path"]), original)
        page = snapshots.load(run["snapshot_id"]).get(self.manifest[0]["page_id"])
        self.assertIn("<table", page.html)


class ExecutionBudgetTest(unittest.TestCase):
    """Four-fold corpus, unchanged per-borehole size: memory and writes stay bounded."""

    FACTOR_SMALL = 1
    FACTOR_LARGE = 4

    @classmethod
    def setUpClass(cls):
        cls.small = write_scaled_site(tempfile.mkdtemp(), cls.FACTOR_SMALL)
        cls.large = write_scaled_site(tempfile.mkdtemp(), cls.FACTOR_LARGE)
        cls.html_small = sum(os.path.getsize(item["url"]) for item in cls.small)
        cls.html_large = sum(os.path.getsize(item["url"]) for item in cls.large)

    def test_corpus_is_four_times_larger_with_same_borehole_size(self):
        self.assertEqual(len(self.large), 4 * len(self.small))
        self.assertAlmostEqual(self.html_large / self.html_small, 4.0, delta=0.2)
        small_sizes = {borehole: len(pages) for borehole, pages in
                       scaled_dataset(self.FACTOR_SMALL).items()}
        large_sizes = {borehole: len(pages) for borehole, pages in
                       scaled_dataset(self.FACTOR_LARGE).items()}
        self.assertEqual(len(large_sizes), 4 * len(small_sizes))
        self.assertEqual(set(large_sizes.values()), {3, 1})  # same shape per borehole

    def test_collection_peak_memory_within_twice_small_batch(self):
        small = _collect_peak(self.small)
        large = _collect_peak(self.large)
        self.assertLessEqual(large, 2 * small,
                             f"collection peak {large} > 2x small-batch {small}")

    def test_replay_peak_memory_within_twice_small_batch(self):
        small = _replay_peak(self.small)
        large = _replay_peak(self.large)
        self.assertLessEqual(large, 2 * small,
                             f"replay peak {large} > 2x small-batch {small}")

    def test_snapshot_and_checkpoint_writes_within_three_times_html(self):
        for manifest, html_bytes in ((self.small, self.html_small),
                                     (self.large, self.html_large)):
            tmp = tempfile.mkdtemp()
            store, snapshots, engine = _crawl(tmp, manifest)
            store.bytes_written = 0
            snapshots.bytes_written = 0
            engine.run("crawl")
            written = store.bytes_written + snapshots.bytes_written
            self.assertLessEqual(written, 3 * html_bytes,
                                 f"{written} bytes written for {html_bytes} bytes of HTML")
            # Writes scale sub-linearly-per-page: no O(n^2) snapshot rewrites.
            self.assertLessEqual(snapshots.bytes_written, 2 * html_bytes)

    def test_writes_grow_less_than_corpus(self):
        def writes(manifest):
            tmp = tempfile.mkdtemp()
            store, snapshots, engine = _crawl(tmp, manifest)
            store.bytes_written = 0
            snapshots.bytes_written = 0
            engine.run("crawl")
            return store.bytes_written + snapshots.bytes_written
        small = writes(self.small)
        large = writes(self.large)
        # A whole-corpus rewrite per page would be ~16x for a 4x corpus.
        self.assertLess(large, 4 * small)


class ResumeOnlyMissingPagesTest(unittest.TestCase):
    def test_resume_fetches_only_pages_not_on_disk(self):
        manifest = write_scaled_site(tempfile.mkdtemp(), factor=2)
        tmp = tempfile.mkdtemp()
        store, snapshots, engine = _crawl(tmp, manifest)

        # Interrupt: drop every page of one borehole copy, keep the rest.
        missing = {entry["page_id"] for entry in manifest
                   if entry["page_id"].startswith(("zk01-p", "zk02-p"))
                   and entry["page_id"].endswith("-c1")}
        state = {"fail": True}

        def flaky(page_id, url):
            if state["fail"] and page_id in missing:
                from corebench.sources import SourceError
                raise SourceError(f"simulated failure for {page_id}")
            return fetch_page(page_id, url)

        engine.fetcher = flaky
        failed = engine.run("crawl")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(len(missing), 4)
        on_disk = set(snapshots.load(failed["snapshot_id"]).page_ids())
        self.assertEqual(on_disk, {entry["page_id"] for entry in manifest} - missing)

        state["fail"] = False
        fetched = []

        def recording(page_id, url):
            fetched.append(page_id)
            return fetch_page(page_id, url)

        engine.fetcher = recording
        run = engine.run("crawl")
        self.assertEqual(run["status"], "completed")
        # Only the four pages that never reached disk are re-fetched.
        self.assertEqual(sorted(fetched), sorted(missing))
        snapshot = snapshots.load(run["snapshot_id"])
        self.assertTrue(snapshot.is_complete)
        self.assertEqual(set(snapshot.page_ids()), {entry["page_id"] for entry in manifest})

    def test_second_resume_after_completion_fetches_nothing(self):
        manifest = write_scaled_site(tempfile.mkdtemp(), factor=1)
        tmp = tempfile.mkdtemp()
        store, snapshots, engine = _crawl(tmp, manifest)
        engine.run("crawl")
        fetched = []
        engine.fetcher = lambda page_id, url: fetched.append(page_id) or fetch_page(page_id, url)
        run = engine.run("crawl")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(fetched, [])

    def test_torn_index_line_does_not_lose_frozen_pages(self):
        manifest = write_scaled_site(tempfile.mkdtemp(), factor=1)
        tmp = tempfile.mkdtemp()
        store, snapshots, engine = _crawl(tmp, manifest)
        engine.run("crawl")
        snapshot_id = "snap-crawl"
        index_path = os.path.join(snapshots.root, snapshot_id, "index.jsonl")
        # Simulate a crash mid-append: a torn trailing line.
        with open(index_path, "a", encoding="utf-8") as handle:
            handle.write('{"page_id": "zk01-p4", "url": "x", "content_sha1": "y", "file')
        loaded = snapshots.load(snapshot_id)
        self.assertEqual(len(loaded.page_ids()), len(manifest))
        self.assertTrue(loaded.is_complete)


if __name__ == "__main__":
    unittest.main()
