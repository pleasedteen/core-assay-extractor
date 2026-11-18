"""Tests for declarative output-field renames (``renamed_from``).

Covers:

* rule parsing and validation of rename declarations (including rejects);
* new JSONL output uses the new field names and carries a self-describing
  schema, while identity-field renames keep measurements attached to the
  original sample;
* semantic diffs on the *same* snapshot treat a declared rename as the same
  business field (no delete+add noise), but still locate a real value/type
  change at the (new) field name;
* a baseline confirmed under the old rule keeps working against a candidate
  run produced by the rename rule on the frozen snapshot.
"""
import copy
import json
import os
import tempfile
import unittest

from tests._helpers import rule_text, static_manifest, read_jsonl
from corebench import mock_site
from corebench.diff import compare_runs, confirm_baseline, regression_report
from corebench.extractor import extract_page
from corebench.merge import merge_extractions, write_jsonl
from corebench.rules import Rule
from corebench.runner import RunConfig, RunEngine
from corebench.sources import SnapshotStore, fetch_page
from corebench.storage import WorkbenchStore


def _build(rule_version: int, layout_b: bool):
    rule = Rule.from_text(rule_text(rule_version))
    tmp = tempfile.mkdtemp()
    manifest = static_manifest(tmp, layout_b=layout_b)
    pages = [fetch_page(item["page_id"], item["url"]) for item in manifest]
    return merge_extractions([extract_page(page, rule) for page in pages], rule=rule), rule


class RenameRuleModelTest(unittest.TestCase):
    def test_renamed_from_is_parsed_and_exposed(self):
        rule = Rule.from_text(rule_text(3))
        sample_renames = rule.field_renames()["sample_table"]
        self.assertEqual(sample_renames["sample_id"], "sample_code")
        self.assertEqual(sample_renames["depth"], "sampling_depth")
        assay_renames = rule.field_renames()["assay_table"]
        self.assertEqual(assay_renames["sample_id"], "sample_code")
        spec = rule.tables["sample_table"].fields["sample_code"]
        self.assertEqual(spec.renamed_from, ["sample_id"])
        self.assertEqual(rule.sample_identity_field(), "sample_code")

    def test_output_schema_lists_rename_history(self):
        rule = Rule.from_text(rule_text(3))
        schema = rule.output_schema()
        sample_fields = schema["tables"]["sample_table"]["fields"]
        self.assertEqual(sample_fields["sample_code"]["renamed_from"], ["sample_id"])
        self.assertEqual(schema["tables"]["sample_table"]["identity"], ["sample_code"])

    def test_rename_to_an_existing_field_is_rejected(self):
        text = rule_text(3).replace(
            "renamed_from: [sample_id]",
            "renamed_from: [parent_interval]", 1)
        with self.assertRaises(ValueError):
            Rule.from_text(text)

    def test_duplicate_historical_name_is_rejected(self):
        text = rule_text(3).replace(
            "renamed_from: [interval_ref]",
            "renamed_from: [sample_id]")
        with self.assertRaises(ValueError):
            Rule.from_text(text)

    def test_self_rename_is_rejected(self):
        text = rule_text(3).replace(
            "renamed_from: [depth]",
            "renamed_from: [sampling_depth]")
        with self.assertRaises(ValueError):
            Rule.from_text(text)

    def test_legacy_rules_have_no_renames_and_still_parse(self):
        for version in (1, 2):
            rule = Rule.from_text(rule_text(version))
            self.assertEqual(rule.sample_identity_field(), "sample_id")
            for table in rule.tables.values():
                self.assertEqual(table.rename_map(), {})


class RenameExtractionTest(unittest.TestCase):
    def test_new_jsonl_uses_new_names_and_embeds_schema(self):
        docs, _rule = _build(3, layout_b=True)
        zk02 = next(d for d in docs if d.borehole_id == "ZK02")
        obj = zk02.to_jsonl_obj()
        self.assertIn("schema", obj)
        sample = zk02.samples[0]
        self.assertEqual(sample["identity"], "sample_code=T201")
        self.assertIn("sample_code", sample["fields"])
        self.assertNotIn("sample_id", sample["fields"])
        self.assertIn("sampling_depth", sample["fields"])
        self.assertNotIn("depth", sample["fields"])

    def test_measurements_stay_attached_to_original_sample(self):
        docs, _rule = _build(3, layout_b=True)
        zk01 = next(d for d in docs if d.borehole_id == "ZK01")
        sample_codes = {s["fields"]["sample_code"]["value"] for s in zk01.samples}
        self.assertEqual(sample_codes, {"S101", "S102", "S103"})
        # Every measurement references the renamed identity field and the same
        # sample values as before -- no sample is lost or re-homed.
        for measurement in zk01.measurements:
            self.assertIn("sample_code", measurement)
            self.assertNotIn("sample_id", measurement)
            self.assertTrue(measurement["identity"].startswith(
                f"sample_code={measurement['sample_code']}|"))
            self.assertIn(measurement["sample_code"], sample_codes)
        self.assertEqual(len(zk01.measurements), 11)

    def test_renamed_fields_keep_semantics_and_units(self):
        docs, _rule = _build(3, layout_b=True)
        zk02 = next(d for d in docs if d.borehole_id == "ZK02")
        depth = zk02.samples[0]["fields"]["sampling_depth"]
        self.assertAlmostEqual(depth["value"], 6.1)
        self.assertEqual(depth["unit"], "m")
        self.assertEqual(depth["raw_unit"], "cm")
        au = next(m for m in zk02.measurements if m["analyte"] == "Au")
        self.assertAlmostEqual(au["value"]["value"], 0.21)


class RenameDiffTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        # Same layout-B snapshot rendered once; both rules replay over it.
        cls.manifest = static_manifest(cls.tmp, layout_b=True)
        cls.pages = [fetch_page(item["page_id"], item["url"]) for item in cls.manifest]
        cls.docs_old, _ = cls._docs(2)
        cls.docs_new, _ = cls._docs(3)

    @classmethod
    def _docs(cls, version):
        rule = Rule.from_text(rule_text(version))
        return merge_extractions([extract_page(p, rule) for p in cls.pages], rule=rule), rule

    def _rows(self, docs):
        path = os.path.join(self.tmp, f"out-{id(docs)}.jsonl")
        write_jsonl(docs, path)
        return read_jsonl(path)

    def test_pure_field_rename_yields_empty_diff(self):
        report = compare_runs(self._rows(self.docs_old), self._rows(self.docs_new))
        self.assertTrue(report["regression_passed"], report["changes"])
        self.assertEqual(report["summary"]["total"], 0)

    def test_renamed_identity_still_matches_records(self):
        # No record may be reported added/removed merely because the identity
        # field changed name from sample_id to sample_code.
        report = compare_runs(self._rows(self.docs_old), self._rows(self.docs_new))
        categories = [c["category"] for c in report["changes"]]
        self.assertNotIn("added", categories)
        self.assertNotIn("removed", categories)

    def test_real_value_change_is_located_at_new_field_name(self):
        new_rows = self._rows(self.docs_new)
        tampered = copy.deepcopy(new_rows)
        doc = next(row for row in tampered if row["borehole_id"] == "ZK02")
        sample = doc["samples"][0]
        target = "sampling_depth"
        original = sample["fields"][target]["value"]
        sample["fields"][target]["value"] = original + 10.0
        report = compare_runs(self._rows(self.docs_old), tampered)
        self.assertFalse(report["regression_passed"])
        change = next(c for c in report["changes"] if c["record_type"] == "sample")
        self.assertEqual(change["identity"], "sample_code=T201")
        field_change = change["field_changes"][0]
        self.assertEqual(field_change["field"], "sampling_depth")
        self.assertEqual(field_change["category"], "value_changed")
        self.assertIn("renamed from 'depth'", field_change["detail"])

    def test_measurement_value_change_under_renamed_sample_field(self):
        new_rows = self._rows(self.docs_new)
        tampered = copy.deepcopy(new_rows)
        doc = next(row for row in tampered if row["borehole_id"] == "ZK02")
        measurement = next(m for m in doc["measurements"]
                           if m["sample_code"] == "T201" and m["analyte"] == "Au")
        measurement["value"]["value"] = 9.99
        report = compare_runs(self._rows(self.docs_old), tampered)
        self.assertFalse(report["regression_passed"])
        change = next(c for c in report["changes"] if c["record_type"] == "measurement")
        self.assertTrue(change["identity"].startswith("sample_code=T201|"))
        self.assertEqual(change["field_changes"][0]["field"], "value")
        self.assertEqual(change["field_changes"][0]["category"], "value_changed")

    def test_type_change_is_reported_on_renamed_field(self):
        new_rows = self._rows(self.docs_new)
        tampered = copy.deepcopy(new_rows)
        doc = next(row for row in tampered if row["borehole_id"] == "ZK02")
        doc["samples"][0]["fields"]["sampling_depth"]["kind"] = "text"
        report = compare_runs(self._rows(self.docs_old), tampered)
        field_change = report["changes"][0]["field_changes"][0]
        self.assertEqual(field_change["field"], "sampling_depth")
        self.assertEqual(field_change["category"], "type_or_meaning_changed")
        self.assertIn("renamed from 'depth'", field_change["detail"])

    def test_genuinely_removed_field_is_still_reported(self):
        # A field the new rule truly no longer carries (and did not rename)
        # must remain a removal, not be swallowed by rename matching.
        old_rows = self._rows(self.docs_old)
        new_rows = self._rows(self.docs_new)
        tampered = copy.deepcopy(new_rows)
        doc = next(row for row in tampered if row["borehole_id"] == "ZK02")
        del doc["samples"][0]["fields"]["lithology_detail"]
        report = compare_runs(old_rows, tampered)
        fields = {fc["field"]: fc["category"]
                  for c in report["changes"] for fc in c["field_changes"]}
        self.assertEqual(fields.get("lithology_detail"), "removed")

    def test_legacy_output_without_schema_still_diffs(self):
        # Baselines confirmed before this feature have no embedded schema; the
        # diff must keep working (fields simply compare under their own names).
        old_rows = self._rows(self.docs_old)
        for row in old_rows:
            row.pop("schema", None)
        new_rows = self._rows(self.docs_new)
        report = compare_runs(old_rows, new_rows)
        # Without rename metadata the renamed fields surface as changes, but
        # nothing crashes and measurement records still match by sample value.
        self.assertIn("summary", report)
        measurement_changes = [c for c in report["changes"]
                               if c["record_type"] == "measurement"]
        self.assertEqual(measurement_changes, [])


class RenameBaselineFlowTest(unittest.TestCase):
    def test_confirmed_baseline_survives_rename_rule_replay(self):
        server, base_url = mock_site.start_mock_server()
        try:
            tmp = tempfile.mkdtemp()
            store = WorkbenchStore(tmp)
            snapshots = SnapshotStore(os.path.join(tmp, "snapshots"))
            store.snapshots = snapshots
            engine = RunEngine(store, snapshots)
            manifest = [
                {"page_id": p["page_id"],
                 "url": f"{base_url}/site-b/{bh}/{p['page_id']}"}
                for bh, pages in mock_site.DATASET.items() for p in pages
                if bh == "ZK02"
            ]
            v2 = store.save_rule_version("drill-archive", rule_text(2), "v2")
            engine.start(RunConfig("base", v2["rule_id"], v2["text"], manifest))
            base_run = engine.run("base")
            snapshot = snapshots.load(base_run["snapshot_id"])
            baseline = confirm_baseline(store, base_run, snapshot, "ZK02 baseline")

            replay = engine.replay("base", rule_text=rule_text(3), new_run_id="base-v3")
            self.assertEqual(replay["snapshot_id"], base_run["snapshot_id"])
            report = regression_report(store, baseline, replay)
            self.assertTrue(report["snapshot_complete"])
            self.assertTrue(report["regression_passed"], report["changes"])
            self.assertEqual(report["summary"]["total"], 0)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
