"""Manual conflict adjudication (review) and replay-guard tests.

Every test runs against the in-process mock drill-archive site: no external
host, no browser, no database service.  The mock dataset carries exactly one
deliberate conflict -- S102/Au/FA with 0.008 (zk01-p1) vs 0.012 (zk01-p2).
"""
import json
import os
import tempfile
import unittest

from tests._helpers import rule_text
from corebench import mock_site
from corebench.diff import confirm_baseline, regression_report
from corebench.merge import read_jsonl
from corebench.review import (
    ReviewReuseError,
    carry_review,
    create_review,
    evaluate_review_on_rows,
)
from corebench.runner import RunConfig, RunEngine
from corebench.sources import SnapshotStore
from corebench.storage import WorkbenchStore

CONFLICT_ID = "sample=S102|analyte=Au|method=FA"


class ReviewWorkflowTest(unittest.TestCase):
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
        self.engine.start(RunConfig("run-a", self.rule["rule_id"],
                                    self.rule["text"], self.manifest))
        self.run = self.engine.run("run-a")
        self.assertTrue(self.run["has_conflicts"])

    def _decision(self, selected_index=1, reason="zk01-p2 为实验室复测结果"):
        return [{
            "borehole_id": "ZK01", "identity": CONFLICT_ID, "field": "value",
            "conflict_index": 0, "selected_index": selected_index, "reason": reason,
        }]

    # ----------------------------------------------------------- creation --
    def test_01_review_requires_reason_and_valid_index(self):
        with self.assertRaises(ValueError):
            create_review(self.store, "run-a",
                          [{**self._decision()[0], "reason": "  "}])
        with self.assertRaises(ValueError):
            create_review(self.store, "run-a",
                          [{**self._decision()[0], "selected_index": 9}])
        with self.assertRaises(ValueError):
            create_review(self.store, "run-a", [])
        with self.assertRaises(ValueError):
            create_review(self.store, "run-a",
                          [{**self._decision()[0], "identity": "sample=NOPE|x"}])

    def test_02_review_creates_independent_result_with_selected_value_and_source(self):
        review, reviewed = create_review(self.store, "run-a", self._decision(),
                                         reviewer="alice")
        self.assertEqual(review["status"], "resolved")
        self.assertEqual(review["source_run_id"], "run-a")
        self.assertEqual(review["decisions"][0]["candidates"][1]["source"]["page_id"],
                         "zk01-p2")
        # The reviewed run is a separate run record and output file.
        self.assertNotEqual(reviewed["run_id"], "run-a")
        self.assertEqual(reviewed["kind"], "reviewed")
        self.assertFalse(reviewed["has_conflicts"])
        self.assertNotEqual(reviewed["output_path"], self.run["output_path"])

        rows = read_jsonl(reviewed["output_path"])
        zk01 = next(row for row in rows if row["borehole_id"] == "ZK01")
        self.assertEqual(zk01["conflicts"], [])
        measurement = next(m for m in zk01["measurements"] if m["identity"] == CONFLICT_ID)
        self.assertEqual(measurement["value"]["value"], 0.012)  # selected candidate
        self.assertEqual(measurement["provenance"]["page_id"], "zk01-p2")  # its source
        self.assertEqual(measurement["adjudication"]["reason"],
                         "zk01-p2 为实验室复测结果")
        self.assertEqual(zk01["review_id"], review["review_id"])

    def test_03_original_run_is_never_modified(self):
        create_review(self.store, "run-a", self._decision())
        original = self.store.load_run("run-a")
        rows = read_jsonl(original["output_path"])
        self.assertEqual(sum(len(row["conflicts"]) for row in rows), 1)
        measurement = next(m for row in rows if row["borehole_id"] == "ZK01"
                           for m in row["measurements"] if m["identity"] == CONFLICT_ID)
        # Original merge kept the first-seen value (zk01-p1).
        self.assertEqual(measurement["value"]["value"], 0.008)
        self.assertNotIn("adjudication", measurement)

    def test_04_selecting_other_candidate_uses_its_value_and_source(self):
        review, reviewed = create_review(
            self.store, "run-a", self._decision(selected_index=0))
        rows = read_jsonl(reviewed["output_path"])
        measurement = next(m for row in rows if row["borehole_id"] == "ZK01"
                           for m in row["measurements"] if m["identity"] == CONFLICT_ID)
        self.assertEqual(measurement["value"]["value"], 0.008)
        self.assertEqual(measurement["provenance"]["page_id"], "zk01-p1")
        self.assertEqual(review["decisions"][0]["selected_index"], 0)

    def test_05_partial_review_keeps_unresolved_conflict_blocking(self):
        # The dataset has one conflict; emulate a second conflict by adding one
        # to the run output, then adjudicate only the original.
        rows = read_jsonl(self.run["output_path"])
        zk01 = next(row for row in rows if row["borehole_id"] == "ZK01")
        zk01["conflicts"].append({
            "identity": "sample=S101|analyte=Pb|method=ICP-MS", "field": "value",
            "values": [{"kind": "number", "value": 1.0, "unit": "mg/kg",
                        "raw_text": "1.0"}],
            "sources": [{"page_id": "zk01-p2", "table": "assay_table"}],
        })
        with open(self.run["output_path"], "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        review, reviewed = create_review(self.store, "run-a", self._decision())
        self.assertEqual(review["status"], "partially_resolved")
        self.assertEqual(len(review["unresolved"]), 1)
        self.assertTrue(reviewed["has_conflicts"])
        snapshot = self.snapshots.load(self.run["snapshot_id"])
        with self.assertRaises(ValueError):
            confirm_baseline(self.store, reviewed, snapshot)

    def test_06_reviewed_run_confirms_baseline_and_remembers_review(self):
        _review, reviewed = create_review(self.store, "run-a", self._decision())
        snapshot = self.snapshots.load(self.run["snapshot_id"])
        baseline = confirm_baseline(self.store, reviewed, snapshot, "adjudicated ZK01")
        self.assertEqual(baseline["review_id"], reviewed["review_id"])
        self.assertEqual(baseline["run_id"], reviewed["run_id"])
        # The raw conflicting run still cannot be confirmed.
        with self.assertRaises(ValueError):
            confirm_baseline(self.store, self.run, snapshot)

    # ----------------------------------------------------- replay safeguards --
    def _baseline_with_review(self):
        review, reviewed = create_review(self.store, "run-a", self._decision())
        snapshot = self.snapshots.load(self.run["snapshot_id"])
        baseline = confirm_baseline(self.store, reviewed, snapshot)
        return review, baseline

    def _replay(self, new_run_id="run-a-replay"):
        return self.engine.replay("run-a", new_run_id=new_run_id)

    def test_07_identical_replay_reuses_adjudication_and_passes_regression(self):
        review, baseline = self._baseline_with_review()
        replay = self._replay()
        report = regression_report(self.store, baseline, replay)
        self.assertTrue(report["regression_passed"], report["changes"])
        evaluation = report["review_reuse"]
        self.assertEqual(evaluation["needs_recheck_count"], 0)
        self.assertEqual(evaluation["reusable_count"], 1)
        self.assertEqual(evaluation["items"][0]["status"], "reusable")
        # The value change vs the adjudicated baseline is marked carried, not blocking.
        carried = [c for c in report["changes"] if c.get("adjudication_carried")]
        self.assertEqual(len(carried), 1)

    def test_08_conflict_vanishing_forces_recheck_even_if_selected_value_remains(self):
        review, baseline = self._baseline_with_review()
        replay = self._replay("replay-vanish")
        # Rewrite the replay output: conflict removed, only the originally
        # selected value (0.012) survives on the measurement.
        rows = read_jsonl(replay["output_path"])
        for row in rows:
            row["conflicts"] = []
            for measurement in row.get("measurements", []):
                if measurement["identity"] == CONFLICT_ID:
                    measurement["value"] = {
                        "kind": "number", "value": 0.012, "unit": "mg/kg",
                        "raw_text": "0.012"}
        path = self.store.run_output_path("replay-vanish")
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        replay["output_path"] = path
        replay["has_conflicts"] = False
        self.store.save_run(replay)

        report = regression_report(self.store, baseline, replay)
        self.assertFalse(report["regression_passed"])
        item = report["review_reuse"]["items"][0]
        self.assertEqual(item["status"], "needs_recheck")
        self.assertEqual(item["reason"], "conflict_no_longer_present")
        self.assertIn("review_recheck_required", json.dumps(report, ensure_ascii=False))

    def test_09_changed_candidate_value_fails_regression(self):
        _review, baseline = self._baseline_with_review()
        replay = self._replay("replay-value")
        rows = read_jsonl(replay["output_path"])
        for row in rows:
            for conflict in row.get("conflicts", []):
                conflict["values"][1]["value"] = 0.099  # selected candidate moved
        path = self.store.run_output_path("replay-value")
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        replay["output_path"] = path
        self.store.save_run(replay)
        report = regression_report(self.store, baseline, replay)
        self.assertFalse(report["regression_passed"])
        self.assertEqual(report["review_reuse"]["items"][0]["reason"], "value_mismatch")

    def test_10_changed_candidate_source_fails_regression(self):
        _review, baseline = self._baseline_with_review()
        replay = self._replay("replay-source")
        rows = read_jsonl(replay["output_path"])
        for row in rows:
            for conflict in row.get("conflicts", []):
                conflict["sources"][1]["page_id"] = "zk01-p3"  # same value, other page
        path = self.store.run_output_path("replay-source")
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        replay["output_path"] = path
        self.store.save_run(replay)
        report = regression_report(self.store, baseline, replay)
        self.assertFalse(report["regression_passed"])
        self.assertEqual(report["review_reuse"]["items"][0]["reason"], "source_mismatch")

    def test_11_carry_review_succeeds_only_when_candidates_match(self):
        review, _baseline = self._baseline_with_review()
        replay = self._replay("replay-carry")
        carried, carried_run = carry_review(self.store, review, replay)
        self.assertEqual(carried["carried_from_review_id"], review["review_id"])
        self.assertEqual(carried["source_run_id"], replay["run_id"])
        self.assertFalse(carried_run["has_conflicts"])
        rows = read_jsonl(carried_run["output_path"])
        measurement = next(m for row in rows if row["borehole_id"] == "ZK01"
                           for m in row["measurements"] if m["identity"] == CONFLICT_ID)
        self.assertEqual(measurement["value"]["value"], 0.012)
        self.assertEqual(measurement["provenance"]["page_id"], "zk01-p2")

        # A mismatching replay must refuse the carry and return the evaluation.
        broken = self.engine.replay("run-a", new_run_id="replay-broken")
        broken_rows = read_jsonl(broken["output_path"])
        for row in broken_rows:
            for conflict in row.get("conflicts", []):
                conflict["values"][1]["value"] = 0.5
        path = self.store.run_output_path("replay-broken")
        with open(path, "w", encoding="utf-8") as handle:
            for row in broken_rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        broken["output_path"] = path
        self.store.save_run(broken)
        with self.assertRaises(ReviewReuseError) as context:
            carry_review(self.store, review, broken)
        self.assertEqual(context.exception.evaluation["needs_recheck_count"], 1)

    def test_12_failed_run_and_incomplete_snapshot_still_block_confirmation(self):
        mock_site.STATE.fail(["zk01-p2"])
        try:
            self.engine.start(RunConfig("run-bad", self.rule["rule_id"],
                                        self.rule["text"], self.manifest))
            failed = self.engine.run("run-bad")
        finally:
            mock_site.STATE.reset()
        self.assertEqual(failed["status"], "failed")
        with self.assertRaises(ValueError):
            create_review(self.store, "run-bad", self._decision())
        snapshot = self.snapshots.load(failed["snapshot_id"])
        self.assertFalse(snapshot.is_complete)
        with self.assertRaises(ValueError):
            confirm_baseline(self.store, failed, snapshot)

    def test_13_evaluation_flags_new_unreviewed_conflicts(self):
        review, _baseline = self._baseline_with_review()
        replay = self._replay("replay-extra")
        rows = read_jsonl(replay["output_path"])
        zk02 = next(row for row in rows if row["borehole_id"] == "ZK02")
        zk02["conflicts"].append({
            "identity": "sample=T201|analyte=Cu|method=ICP-AES", "field": "value",
            "values": [{"kind": "number", "value": 1.0, "unit": "mg/kg",
                        "raw_text": "1.0"}],
            "sources": [{"page_id": "zk02-p1", "table": "assay_table"}],
        })
        evaluation = evaluate_review_on_rows(review, rows)
        self.assertEqual(evaluation["reusable_count"], 1)
        self.assertEqual(evaluation["needs_recheck_count"], 1)
        self.assertEqual(evaluation["unreviewed_conflicts"][0]["borehole_id"], "ZK02")


if __name__ == "__main__":
    unittest.main()
