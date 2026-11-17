"""End-to-end extraction vs the hand-written expected dataset.

Covers nested interval/sample ownership, dirty tables, cross-page
complementary measurements, unit equivalence, detection-limit semantics and the
deliberate cross-page conflict.
"""
import os
import tempfile
import unittest

from tests._helpers import rule_text, static_manifest, read_jsonl, EXPECTED, almost_equal
from corebench import mock_site
from corebench.rules import Rule
from corebench.sources import fetch_page
from corebench.extractor import extract_page
from corebench.merge import merge_extractions


def build_documents(layout_b: bool):
    rule = Rule.from_text(rule_text(2 if layout_b else 1))
    tmp = tempfile.mkdtemp()
    manifest = static_manifest(tmp, layout_b=layout_b)
    extractions = [extract_page(fetch_page(item["page_id"], item["url"]), rule) for item in manifest]
    return merge_extractions(extractions), extractions


class ExpectedDatasetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents, cls.extractions = build_documents(layout_b=False)
        cls.by_id = {doc.borehole_id: doc for doc in cls.documents}
        cls.expected = {row["borehole_id"]: row for row in read_jsonl(EXPECTED)}

    def test_borehole_and_hierarchy_counts(self):
        self.assertEqual(set(self.by_id), {"ZK01", "ZK02"})
        zk01 = self.by_id["ZK01"]
        self.assertEqual(len(zk01.intervals), 2)
        self.assertEqual(len(zk01.samples), 3)
        # 8 wide + 3 long measurements across the three pages.
        self.assertEqual(len(zk01.measurements), 11)
        zk02 = self.by_id["ZK02"]
        self.assertEqual(len(zk02.measurements), 2)

    def test_sample_belongs_to_interval_via_ref(self):
        refs = {s["fields"]["sample_id"]["value"]: s["fields"]["interval_ref"]["value"]
                for s in self.by_id["ZK01"].samples}
        self.assertEqual(refs["S103"], "I-2")
        self.assertEqual(refs["S101"], "I-1")

    def test_depth_normalisation_matches_expected(self):
        expected_samples = {s["sample_id"]: s for s in self.expected["ZK01"]["samples"]}
        for sample in self.by_id["ZK01"].samples:
            sid = sample["fields"]["sample_id"]["value"]
            depth = sample["fields"]["depth"]
            self.assertTrue(almost_equal(depth["value"], expected_samples[sid]["depth_m"]))
            self.assertEqual(depth["unit"], "m")

    def test_measurement_value_kinds(self):
        index = {m["identity"]: m for m in self.by_id["ZK01"].measurements}
        def kind(ident):
            return index[ident]["value"]["kind"]
        self.assertEqual(kind("sample=S101|analyte=Au|method=FA"), "censored")
        self.assertEqual(kind("sample=S102|analyte=Zn|method=ICP-AES"), "zero")
        self.assertEqual(kind("sample=S101|analyte=Zn|method=ICP-AES"), "missing")
        self.assertEqual(kind("sample=S103|analyte=Mo|method=ICP-MS"), "missing")
        self.assertEqual(kind("sample=S103|analyte=Au|method=GFAA"), "censored")

    def test_ug_kg_equivalence_preserved(self):
        index = {m["identity"]: m for m in self.by_id["ZK01"].measurements}
        cu = index["sample=S102|analyte=Cu|method=ICP-AES"]["value"]
        self.assertAlmostEqual(cu["value"], 35.0)
        self.assertEqual(cu["unit"], "mg/kg")
        self.assertEqual(cu["raw_unit"], "ug/kg")
        gfaa = index["sample=S103|analyte=Au|method=GFAA"]["value"]
        self.assertAlmostEqual(gfaa["value"], 0.002)
        self.assertEqual(gfaa["raw_unit"], "ug/kg")

    def test_cross_page_complementary_measurements_merge(self):
        # S101 Pb only exists on page 2; S101 itself is repeated on both pages.
        index = {m["identity"]: m for m in self.by_id["ZK01"].measurements}
        pb = index["sample=S101|analyte=Pb|method=ICP-MS"]
        self.assertEqual(pb["provenance"]["page_id"], "zk01-p2")
        sample = next(s for s in self.by_id["ZK01"].samples
                      if s["identity"] == "sample_id=S101")
        self.assertEqual(sample["provenance"]["page_ids"], ["zk01-p1", "zk01-p2"])

    def test_conflicting_measurement_is_recorded_and_blocks(self):
        zk01 = self.by_id["ZK01"]
        self.assertEqual(len(zk01.conflicts), 1)
        conflict = zk01.conflicts[0]
        self.assertEqual(conflict.identity, "sample=S102|analyte=Au|method=FA")
        values = [item["value"] for item in conflict.values]
        self.assertEqual(sorted(values), [0.008, 0.012])
        # Both conflicting sources remain traceable.
        pages = {source["page_id"] for source in conflict.sources}
        self.assertEqual(pages, {"zk01-p1", "zk01-p2"})
        self.assertTrue(any(doc.conflicts for doc in self.documents))

    def test_clean_borehole_has_no_conflict(self):
        self.assertEqual(self.by_id["ZK02"].conflicts, [])

    def test_full_expected_measurement_set(self):
        for borehole_id, expected_doc in self.expected.items():
            doc = self.by_id[borehole_id]
            actual = {m["identity"]: m for m in doc.measurements}
            self.assertEqual(set(actual), {item["identity"] for item in expected_doc["measurements"]})
            for item in expected_doc["measurements"]:
                value = actual[item["identity"]]["value"]
                self.assertEqual(value["kind"], item["kind"])
                self.assertEqual(value["unit"], item["unit"])
                if item["value"] is None:
                    self.assertIsNone(value["value"])
                else:
                    self.assertTrue(almost_equal(value["value"], item["value"]))

    def test_order_independence(self):
        # Feed pages in reverse order; merged result must stay identical.
        rule = Rule.from_text(rule_text(1))
        tmp = tempfile.mkdtemp()
        manifest = static_manifest(tmp, False)
        pages_forward = [fetch_page(item["page_id"], item["url"]) for item in manifest]
        pages_reversed = [fetch_page(item["page_id"], item["url"]) for item in reversed(manifest)]
        forward_docs = merge_extractions([extract_page(page, rule) for page in pages_forward])
        reversed_docs = merge_extractions([extract_page(page, rule) for page in pages_reversed])
        import json
        a = sorted(json.dumps(d.to_jsonl_obj(), sort_keys=True, ensure_ascii=False) for d in reversed_docs)
        b = sorted(json.dumps(d.to_jsonl_obj(), sort_keys=True, ensure_ascii=False) for d in forward_docs)
        self.assertEqual(a, b)

    def test_provenance_points_at_raw_content(self):
        m = next(m for m in self.by_id["ZK01"].measurements if m["analyte"] == "Mo")
        prov = m["provenance"]
        self.assertEqual(prov["page_id"], "zk01-p3")
        self.assertIn("raw_row", prov)
        self.assertTrue(prov["content_sha1"])
        self.assertTrue(prov["url"].endswith(".html"))

    def test_unknown_header_is_flagged_not_silently_dropped(self):
        from corebench.sources import PageContent
        html = mock_site.render_page("ZK01", mock_site.DATASET["ZK01"][2])
        html = html.replace("<th>结果</th>", "<th>结果</th><th>新仪器读数</th>")
        rule = Rule.from_text(rule_text(1))
        page = PageContent.make("zk01-p3", "injected", html)
        result = extract_page(page, rule)
        unexplained = [w for w in result.warnings if w.code == "UNEXPLAINED_COLUMN"]
        self.assertTrue(unexplained)
        self.assertIn("新仪器读数", unexplained[0].raw)


class LayoutBEquivalenceTest(unittest.TestCase):
    def test_v2_extracts_same_semantics_on_layout_b(self):
        documents, _ = build_documents(layout_b=True)
        expected = {row["borehole_id"]: row for row in read_jsonl(EXPECTED)}
        by_id = {doc.borehole_id: doc for doc in documents}
        for borehole_id, expected_doc in expected.items():
            actual = {m["identity"]: m for m in by_id[borehole_id].measurements}
            for item in expected_doc["measurements"]:
                value = actual[item["identity"]]["value"]
                self.assertEqual(value["kind"], item["kind"], item["identity"])
                if item["value"] is not None:
                    self.assertTrue(almost_equal(value["value"], item["value"]), item["identity"])
                self.assertEqual(value["unit"], "mg/kg")


if __name__ == "__main__":
    unittest.main()
