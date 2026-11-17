"""Runner, checkpoint/resume, baseline and regression-diff tests.

Uses the in-process mock HTTP server on an ephemeral port: no external host,
no browser, no database service.
"""
import json
import os
import tempfile
import time
import unittest
import urllib.request

from tests._helpers import rule_text
from corebench import mock_site
from corebench.diff import compare_runs, confirm_baseline, regression_report
from corebench.merge import read_jsonl
from corebench.runner import RunConfig, RunEngine
from corebench.rules import Rule
from corebench.sources import SnapshotStore, SourceError, fetch_page
from corebench.storage import WorkbenchStore


def _post(base_url: str, path: str, payload: dict):
    req = urllib.request.Request(
        base_url + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


class RunnerLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base_url = mock_site.start_mock_server()
        cls.manifest = [
            {"page_id": page["page_id"],
             "url": f"{cls.base_url}/site/{borehole}/{page['page_id']}"}
            for borehole, pages in mock_site.DATASET.items() for page in pages
        ]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = WorkbenchStore(self.tmp)
        self.snapshots = SnapshotStore(os.path.join(self.tmp, "snapshots"))
        self.store.snapshots = self.snapshots
        self.engine = RunEngine(self.store, self.snapshots)
        self.rule = self.store.save_rule_version("drill-archive", rule_text(1), "v1")

    def test_full_run_over_http(self):
        run = self.engine.start(RunConfig("r1", self.rule["rule_id"], self.rule["text"], self.manifest))
        run = self.engine.run("r1")
        self.assertEqual(run["status"], "completed")
        self.assertTrue(run["has_conflicts"])  # ZK01 carries the deliberate conflict
        self.assertTrue(os.path.exists(run["output_path"]))
        rows = read_jsonl(run["output_path"])
        self.assertEqual({row["borehole_id"] for row in rows}, {"ZK01", "ZK02"})

    def test_interrupted_run_resumes_without_refetch(self):
        mock_site.STATE.fail(["zk01-p2"])
        try:
            run = self.engine.start(RunConfig("r2", self.rule["rule_id"], self.rule["text"], self.manifest))
            run = self.engine.run("r2")
            self.assertEqual(run["status"], "failed")
            failed_pages = [p for p in run["pages"] if p["state"] == "failed"]
            self.assertEqual([p["page_id"] for p in failed_pages], ["zk01-p2"])
            snapshot = self.snapshots.load(run["snapshot_id"])
            self.assertFalse(snapshot.is_complete)
            fetched_before = len(snapshot.pages)
        finally:
            mock_site.STATE.reset()
        # Resume: already fetched pages must come from the checkpoint.
        seen_urls = []

        def counting_fetcher(page_id, url):
            seen_urls.append(url)
            return fetch_page(page_id, url)

        self.engine.fetcher = counting_fetcher
        run = self.engine.run("r2")
        self.assertEqual(run["status"], "completed")
        # Only the previously failing page (and later ones) are fetched.
        refetched = {url.rsplit("/", 1)[-1] for url in seen_urls}
        self.assertNotIn("zk01-p1.html", refetched)
        self.assertIn("zk01-p2", refetched)
        snapshot = self.snapshots.load(run["snapshot_id"])
        self.assertTrue(snapshot.is_complete)
        self.assertGreaterEqual(fetched_before, 1)

    def test_saving_new_rule_does_not_change_running_run(self):
        run = self.engine.start(RunConfig("r3", self.rule["rule_id"], self.rule["text"], self.manifest))
        self.store.save_rule_version("drill-archive", rule_text(2), "v2 saved mid-run")
        run = self.engine.run("r3")
        # The run keeps the exact rule text pinned at start time.
        self.assertEqual(run["rule_text"], self.rule["text"])
        self.assertIn('rule_version: "1"', run["rule_text"])

    def test_failed_run_cannot_become_baseline(self):
        mock_site.STATE.fail(["zk02-p1"])
        try:
            self.engine.start(RunConfig("r4", self.rule["rule_id"], self.rule["text"], self.manifest))
            run = self.engine.run("r4")
        finally:
            mock_site.STATE.reset()
        snapshot = self.snapshots.load(run["snapshot_id"])
        with self.assertRaises(ValueError):
            confirm_baseline(self.store, run, snapshot)

    def test_conflicting_run_cannot_become_baseline(self):
        self.engine.start(RunConfig("r5", self.rule["rule_id"], self.rule["text"], self.manifest))
        run = self.engine.run("r5")
        snapshot = self.snapshots.load(run["snapshot_id"])
        with self.assertRaises(ValueError):
            confirm_baseline(self.store, run, snapshot)

    def test_clean_borehole_run_confirms_baseline(self):
        zk02_manifest = [page for page in self.manifest if "zk02" in page["page_id"]]
        self.engine.start(RunConfig("r6", self.rule["rule_id"], self.rule["text"], zk02_manifest))
        run = self.engine.run("r6")
        snapshot = self.snapshots.load(run["snapshot_id"])
        baseline = confirm_baseline(self.store, run, snapshot, "clean ZK02")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "baselines",
                                                     f"{baseline['baseline_id']}.json")))

    def test_replay_uses_frozen_snapshot(self):
        self.engine.start(RunConfig("r7", self.rule["rule_id"], self.rule["text"], self.manifest))
        run = self.engine.run("r7")
        replay = self.engine.replay("r7", new_run_id="r7-replay")
        self.assertEqual(replay["snapshot_id"], run["snapshot_id"])
        self.assertEqual(read_jsonl(replay["output_path"]), read_jsonl(run["output_path"]))

    def test_incomplete_snapshot_cannot_pass_regression(self):
        zk02_manifest = [page for page in self.manifest if "zk02" in page["page_id"]]
        # Simulate a partial crawl: one fetch failure leaves the snapshot incomplete.
        mock_site.STATE.fail(["zk02-p1"])
        try:
            self.engine.start(RunConfig("partial", self.rule["rule_id"],
                                        self.rule["text"], zk02_manifest))
            partial_run = self.engine.run("partial")
        finally:
            mock_site.STATE.reset()
        snapshot = self.snapshots.load(partial_run["snapshot_id"])
        self.assertFalse(snapshot.is_complete)
        # Build a baseline from a separate clean run on the same snapshot id is
        # impossible, so verify the guard directly: a completed run paired with
        # an incomplete snapshot must still fail regression.
        clean_engine = RunEngine(self.store, self.snapshots)
        clean_engine.start(RunConfig("partial-clean", self.rule["rule_id"],
                                     self.rule["text"], zk02_manifest))
        clean_run = clean_engine.run("partial-clean")
        clean_snapshot = self.snapshots.load(clean_run["snapshot_id"])
        baseline = confirm_baseline(self.store, clean_run, clean_snapshot)
        # Tamper the snapshot used by the report to be incomplete (same id family).
        bad_snapshot = self.snapshots.load(clean_run["snapshot_id"])
        bad_snapshot.failures["zk02-p1"] = "simulated gap"
        self.snapshots.save(bad_snapshot)
        report = regression_report(self.store, baseline, clean_run)
        self.assertFalse(report["regression_passed"])
        self.assertFalse(report["snapshot_complete"])


class DiffSemanticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base_url = mock_site.start_mock_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _run(self, store, snapshots, rule_text_value, run_id, layout_b=False, zk02_only=False):
        prefix = "/site-b" if layout_b else "/site"
        manifest = [
            {"page_id": page["page_id"],
             "url": f"{self.base_url}{prefix}/{borehole}/{page['page_id']}"}
            for borehole, pages in mock_site.DATASET.items() for page in pages
            if not zk02_only or borehole == "ZK02"
        ]
        engine = RunEngine(store, snapshots)
        rule = store.save_rule_version("drill-archive", rule_text_value, run_id)
        engine.start(RunConfig(run_id, rule["rule_id"], rule["text"], manifest))
        return engine.run(run_id), engine

    def test_rename_reorder_equivalent_unit_are_not_changes(self):
        tmp = tempfile.mkdtemp()
        store = WorkbenchStore(tmp)
        snapshots = SnapshotStore(os.path.join(tmp, "snapshots"))
        # Baseline run with rule v1 on a clean single-borehole snapshot.
        run_a, engine_a = self._run(store, snapshots, rule_text(1), "a",
                                    layout_b=False, zk02_only=True)
        # Equivalent candidate rule: analyte list reordered, harmless aliases
        # added, comment changed. Output field names and semantics are identical.
        tweaked = (rule_text(1)
                   .replace("  - code: Au\n    labels: [Au]",
                            "  - code: Cu\n    labels: [Cu, 铜]\n  - code: Au\n    labels: [Au, 金]")
                   .replace("  - code: Cu\n    labels: [Cu]\n", "")
                   .replace('rule_version: "1"', 'rule_version: "1-equivalent"'))
        # Sanity check: the tweaked rule still parses.
        Rule.from_text(tweaked)
        replay_b = engine_a.replay("a", rule_text=tweaked, new_run_id="a-v2")
        rows_old = read_jsonl(run_a["output_path"])
        rows_new = read_jsonl(replay_b["output_path"])
        report = compare_runs(rows_old, rows_new)
        self.assertTrue(report["regression_passed"], report["changes"])
        self.assertEqual(report["summary"]["total"], 0)

    def test_value_and_meaning_changes_are_classified(self):
        old_rows = [{
            "borehole_id": "Z", "intervals": [], "samples": [],
            "measurements": [{
                "identity": "sample=S|analyte=Au|method=FA", "sample_id": "S",
                "analyte": "Au", "method": "FA",
                "value": {"kind": "number", "value": 0.01, "unit": "mg/kg", "raw_text": "0.01"},
                "provenance": {"page_id": "p1"},
            }],
            "warnings": [], "conflicts": [], "source_pages": ["p1"],
        }]
        new_value = json.loads(json.dumps(old_rows))
        new_value[0]["measurements"][0]["value"]["value"] = 0.02
        report = compare_runs(old_rows, new_value)
        self.assertFalse(report["regression_passed"])
        self.assertEqual(report["changes"][0]["field_changes"][0]["category"], "value_changed")

        new_meaning = json.loads(json.dumps(old_rows))
        new_meaning[0]["measurements"][0]["value"] = {
            "kind": "censored", "value": 0.01, "unit": "mg/kg", "raw_text": "<0.01"}
        report = compare_runs(old_rows, new_meaning)
        self.assertEqual(report["changes"][0]["field_changes"][0]["category"],
                         "type_or_meaning_changed")

    def test_added_and_removed_records(self):
        old_rows = [{"borehole_id": "Z", "intervals": [], "samples": [],
                     "measurements": [], "warnings": [], "conflicts": [], "source_pages": []}]
        new_rows = [{
            "borehole_id": "Z", "intervals": [], "samples": [],
            "measurements": [{
                "identity": "sample=S|analyte=Au|method=FA", "sample_id": "S",
                "analyte": "Au", "method": "FA",
                "value": {"kind": "number", "value": 1.0, "unit": "mg/kg", "raw_text": "1"},
                "provenance": {"page_id": "p1"}}],
            "warnings": [], "conflicts": [], "source_pages": ["p1"]}]
        report = compare_runs(old_rows, new_rows)
        self.assertEqual(report["changes"][0]["category"], "added")
        report_reverse = compare_runs(new_rows, old_rows)
        self.assertEqual(report_reverse["changes"][0]["category"], "removed")

    def test_extraction_failure_blocks_regression(self):
        good = [{"borehole_id": "Z",
                 "intervals": [{"identity": "interval_id=I1", "fields": {}, "provenance": {"table": "interval_table"}}],
                 "samples": [], "measurements": [], "warnings": [], "conflicts": [], "source_pages": ["p1"]}]
        broken = [{"borehole_id": "Z", "intervals": [], "samples": [], "measurements": [],
                   "warnings": [{"code": "EXTRACTION_FAILED", "table": "interval_table",
                                 "page_id": "p1", "message": "boom", "raw": "<table>"}],
                   "conflicts": [], "source_pages": ["p1"]}]
        report = compare_runs(good, broken)
        self.assertFalse(report["regression_passed"])
        categories = {change.category for change in
                      [type("C", (), change)() for change in report["changes"]]}
        self.assertIn("extraction_failed", json.dumps(report))

    def test_unexplained_column_is_not_normal_deletion(self):
        old_rows = [{"borehole_id": "Z", "intervals": [], "samples": [], "measurements": [],
                     "warnings": [], "conflicts": [], "source_pages": ["p1"]}]
        new_rows = [{"borehole_id": "Z", "intervals": [], "samples": [], "measurements": [],
                     "warnings": [{"code": "UNEXPLAINED_COLUMN", "table": "assay_table",
                                   "page_id": "p1", "message": "x", "raw": "新元素"}],
                     "conflicts": [], "source_pages": ["p1"]}]
        report = compare_runs(old_rows, new_rows)
        self.assertFalse(report["regression_passed"])
        self.assertIn("unexplained_field", json.dumps(report))

    def test_regression_requires_same_snapshot(self):
        tmp = tempfile.mkdtemp()
        store = WorkbenchStore(tmp)
        snapshots = SnapshotStore(os.path.join(tmp, "snapshots"))
        store.snapshots = snapshots
        # Two distinct runs on distinct (per-run) snapshots using clean ZK02.
        run_a, _ = self._run(store, snapshots, rule_text(1), "snap-a", zk02_only=True)
        run_b, _ = self._run(store, snapshots, rule_text(2), "snap-b",
                             layout_b=True, zk02_only=True)
        snapshot_b = snapshots.load(run_b["snapshot_id"])
        baseline = confirm_baseline(store, run_b, snapshot_b)
        if baseline["snapshot_id"] != run_a["snapshot_id"]:
            with self.assertRaises(ValueError):
                regression_report(store, baseline, run_a)


if __name__ == "__main__":
    unittest.main()
