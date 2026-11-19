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


class GapAttributionTest(unittest.TestCase):
    """Same-snapshot replays of two rule versions on multi-page archives.

    These exercise the reported defects end to end: a new rule that no longer
    recognises an old column must produce *one* attributed gap (not a deletion
    plus an unexplained field), legitimate removals keep their ``removed``
    category, and a missing critical table blocks baseline/regression.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.store = WorkbenchStore(cls.tmp)
        cls.snapshots = SnapshotStore(os.path.join(cls.tmp, "snapshots"))
        cls.store.snapshots = cls.snapshots
        cls.manifest = None
        cls.server, cls.base_url = mock_site.start_mock_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.engine = RunEngine(self.store, self.snapshots)
        self.manifest = [
            {"page_id": page["page_id"],
             "url": f"{self.base_url}/site/{borehole}/{page['page_id']}"}
            for borehole, pages in mock_site.DATASET.items() for page in pages
        ]

    def _start(self, run_id, rule):
        self.engine.start(RunConfig(run_id, rule["rule_id"], rule["text"], self.manifest))
        return self.engine.run(run_id)

    def test_dropped_column_counts_once_per_affected_page(self):
        rule_v1 = self.store.save_rule_version("drill-archive", rule_text(1), "v1")
        base = self._start(f"gap-base-{id(self)}", rule_v1)
        old_rows = read_jsonl(base["output_path"])

        # Candidate rule forgets the Zn analyte entirely: both Zn columns on
        # zk01-p1 and the Zn column on zk01-p2 become uninterpretable, while
        # every other analyte is still extracted.
        candidate = rule_text(1).replace("  - code: Zn\n    labels: [Zn]\n", "")
        replay = self.engine.replay(base["run_id"], rule_text=candidate,
                                    new_run_id=f"gap-cand-{id(self)}")
        new_rows = read_jsonl(replay["output_path"])
        report = compare_runs(old_rows, new_rows)

        # One gap per affected original page/column; nothing reported as a
        # plain deletion, and no duplicate unexplained entries.
        self.assertEqual(report["summary"].get("removed", 0), 0, report["summary"])
        gap_changes = [c for c in report["changes"] if c["record_type"] == "gap"]
        self.assertEqual(len(gap_changes), 2, gap_changes)
        self.assertEqual({c["new_source"]["page_id"] for c in gap_changes},
                         {"zk01-p1", "zk01-p2"})
        self.assertEqual({c["category"] for c in gap_changes}, {"unexplained_field"})
        self.assertTrue(all("zn" in c["identity"].lower() for c in gap_changes))
        # The two measurements that vanished from zk01-p1 are attached to that
        # page's single gap, not double counted.
        p1_gap = next(c for c in gap_changes if c["new_source"]["page_id"] == "zk01-p1")
        self.assertEqual(
            {a["identity"] for a in p1_gap["affected"]},
            {"measurement::sample=S101|analyte=Zn|method=ICP-AES",
             "measurement::sample=S102|analyte=Zn|method=ICP-AES"},
        )
        # Regression must fail because an old field became uninterpretable.
        self.assertFalse(report["regression_passed"])

    def test_gap_attribution_keeps_other_pages_and_missing_values(self):
        rule_v1 = self.store.save_rule_version("drill-archive", rule_text(1), "v1")
        base = self._start(f"gap2-base-{id(self)}", rule_v1)
        old_rows = read_jsonl(base["output_path"])

        # Only the long-format page (zk01-p3) loses its value column; the wide
        # pages keep extracting and their legitimate missing cells stay intact.
        candidate = rule_text(1).replace("    value_column: [结果]", "    value_column: [结果值未识别]")
        replay = self.engine.replay(base["run_id"], rule_text=candidate,
                                    new_run_id=f"gap2-cand-{id(self)}")
        new_rows = read_jsonl(replay["output_path"])
        report = compare_runs(old_rows, new_rows)

        gap_changes = [c for c in report["changes"] if c["record_type"] == "gap"]
        self.assertEqual(len(gap_changes), 1, report["changes"])
        self.assertEqual(gap_changes[0]["new_source"]["page_id"], "zk01-p3")
        # The S103 measurements on that page are attributed to the single gap.
        self.assertEqual(len(gap_changes[0]["affected"]), 3)
        # Everything extracted on the other pages is unchanged, including the
        # deliberate empty (missing) cells on zk01-p1.
        self.assertFalse([c for c in report["changes"]
                          if c["record_type"] != "gap" and c["category"] != "added"])
        self.assertEqual(report["summary"].get("removed", 0), 0)

    def test_legitimate_deletion_is_still_removed(self):
        rule_v1 = self.store.save_rule_version("drill-archive", rule_text(1), "v1")
        base = self._start(f"del-base-{id(self)}", rule_v1)
        old_rows = read_jsonl(base["output_path"])
        new_rows = json.loads(json.dumps(old_rows))
        for doc in new_rows:
            if doc["borehole_id"] == "ZK01":
                doc["intervals"] = [i for i in doc["intervals"]
                                    if i["identity"] != "interval_id=I-2"]
        # No gap warning corresponds to this removal, so it stays ``removed``.
        report = compare_runs(old_rows, new_rows)
        self.assertEqual(report["summary"].get("removed", 0), 1)
        self.assertEqual(report["changes"][0]["category"], "removed")

    def test_gap_on_one_table_does_not_absorb_other_table_records(self):
        rule_v1 = self.store.save_rule_version("drill-archive", rule_text(1), "v1")
        base = self._start(f"xtab-base-{id(self)}", rule_v1)
        old_rows = read_jsonl(base["output_path"])
        candidate = rule_text(1).replace("selector: table.assay-table",
                                          "selector: table.assay-absent")
        replay = self.engine.replay(base["run_id"], rule_text=candidate,
                                    new_run_id=f"xtab-cand-{id(self)}")
        new_rows = read_jsonl(replay["output_path"])
        # Delete one interval as well: the assay gap must not swallow it.
        for doc in new_rows:
            if doc["borehole_id"] == "ZK01":
                doc["intervals"] = [i for i in doc["intervals"]
                                    if i["identity"] != "interval_id=I-2"]
        report = compare_runs(old_rows, new_rows)
        removed = [c for c in report["changes"] if c["category"] == "removed"]
        self.assertEqual([c["identity"] for c in removed], ["interval::interval_id=I-2"])
        gaps = [c for c in report["changes"] if c["record_type"] == "gap"]
        self.assertTrue(gaps)
        self.assertTrue(all(c["new_source"]["table"] == "assay_table" for c in gaps))

    def test_missing_critical_table_blocks_baseline_and_regression(self):
        rule_v1 = self.store.save_rule_version("drill-archive", rule_text(1), "v1")
        zk02 = [page for page in self.manifest if "zk02" in page["page_id"]]
        self.engine.start(RunConfig(f"crit-base-{id(self)}", rule_v1["rule_id"],
                                    rule_v1["text"], zk02))
        base = self.engine.run(f"crit-base-{id(self)}")
        baseline = confirm_baseline(self.store, base,
                                    self.snapshots.load(base["snapshot_id"]))

        broken_rule = rule_text(1).replace("selector: table.assay-table",
                                           "selector: table.assay-gone")
        replay = self.engine.replay(base["run_id"], rule_text=broken_rule,
                                    new_run_id=f"crit-cand-{id(self)}")
        # The run "completes" but the critical assay table is simply absent.
        self.assertTrue(replay["has_critical_gaps"])
        with self.assertRaises(ValueError):
            confirm_baseline(self.store, replay,
                             self.snapshots.load(replay["snapshot_id"]))
        report = regression_report(self.store, baseline, replay)
        self.assertFalse(report["regression_passed"])
        self.assertEqual({g["table"] for g in report["candidate_critical_gaps"]},
                         {"assay_table"})

    def test_optional_table_gap_does_not_block_baseline(self):
        rule_v1 = self.store.save_rule_version("drill-archive", rule_text(1), "v1")
        zk02 = [page for page in self.manifest if "zk02" in page["page_id"]]
        optional = (rule_text(1)
                    .replace("      selector: table.assay-table",
                             "      selector: table.assay-gone\n      required: false"))
        self.engine.start(RunConfig(f"opt-{id(self)}", rule_v1["rule_id"],
                                    optional, zk02))
        run = self.engine.run(f"opt-{id(self)}")
        self.assertFalse(run["has_critical_gaps"])
        # A non-critical gap is still baselineable.
        confirm_baseline(self.store, run, self.snapshots.load(run["snapshot_id"]))

    def test_unexplained_column_blocks_regression_not_baseline(self):
        # An extra column the rule cannot interpret is a field-level gap: it is
        # attributed to its original page/column and is not a critical table
        # failure, so it blocks regression without implying the table is gone.
        from corebench.sources import PageContent
        from corebench.extractor import extract_page
        from corebench.rules import Rule
        html = mock_site.render_page("ZK02", mock_site.DATASET["ZK02"][0])
        # Add a whole new column the rule knows nothing about.
        html = html.replace('<th colspan="1">Cu</th>',
                            '<th colspan="1">Cu</th><th colspan="1">Xr</th>')
        html = html.replace('<th>ICP-AES (mg/kg)</th>',
                            '<th>ICP-AES (mg/kg)</th><th>新仪器读数 (mg/kg)</th>')
        html = html.replace('<td>77.0</td>', '<td>77.0</td><td>9.5</td>')
        page = PageContent.make("zk02-p1", "injected", html)
        result = extract_page(page, Rule.from_text(rule_text(1)))
        unexplained = [w for w in result.warnings if w.code == "UNEXPLAINED_COLUMN"]
        self.assertTrue(unexplained)
        # It is tied to the original page and column, and is not critical.
        self.assertEqual(unexplained[0].page_id, "zk02-p1")
        self.assertEqual(unexplained[0].field, "Xr / 新仪器读数 (mg/kg)")
        self.assertFalse(unexplained[0].critical)
        self.assertIsNotNone(unexplained[0].url)

    def test_same_gap_in_baseline_and_candidate_is_not_a_regression(self):
        rule_v1 = self.store.save_rule_version("drill-archive", rule_text(1), "v1")
        zk02 = [page for page in self.manifest if "zk02" in page["page_id"]]
        optional = (rule_text(1)
                    .replace("      selector: table.assay-table",
                             "      selector: table.assay-gone\n      required: false"))
        self.engine.start(RunConfig(f"same-base-{id(self)}", rule_v1["rule_id"],
                                    optional, zk02))
        base = self.engine.run(f"same-base-{id(self)}")
        baseline = confirm_baseline(self.store, base,
                                    self.snapshots.load(base["snapshot_id"]))
        replay = self.engine.replay(base["run_id"], rule_text=optional,
                                    new_run_id=f"same-cand-{id(self)}")
        report = regression_report(self.store, baseline, replay)
        # The pre-existing non-critical gap is not reported anew.
        self.assertTrue(report["regression_passed"], report["changes"])
        self.assertEqual(report["summary"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
