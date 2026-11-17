"""Unit tests for value kinds, unit normalisation and the tolerant parser."""
import unittest

from corebench.values import parse_value, values_equivalent, extract_unit, normalize_unit
from corebench.html_dom import parse_html
from corebench.tables import build_table, detect_header_layers
from corebench import dsl


class ValueSemanticsTest(unittest.TestCase):
    def test_censored_is_not_a_number(self):
        v = parse_value("<0.05", "concentration", "mg/kg")
        self.assertEqual(v.kind, "censored")
        self.assertEqual(v.limit, 0.05)
        self.assertEqual(v.unit, "mg/kg")

    def test_measured_zero_distinct_from_missing(self):
        zero = parse_value("0", "concentration", "mg/kg")
        missing = parse_value("", "concentration", "mg/kg")
        missing_na = parse_value("N/A", "concentration", "mg/kg")
        self.assertEqual(zero.kind, "zero")
        self.assertEqual(missing.kind, "missing")
        self.assertEqual(missing_na.kind, "missing")
        self.assertNotEqual(zero.kind, missing.kind)

    def test_depth_cm_to_metres(self):
        v = parse_value("1300", "depth", "cm")
        self.assertEqual(v.kind, "number")
        self.assertEqual(v.value, 13.0)
        self.assertEqual(v.unit, "m")
        self.assertEqual(v.raw_unit, "cm")

    def test_concentration_ug_to_mg(self):
        v = parse_value("35000", "concentration", "ug/kg")
        self.assertEqual(v.value, 35.0)
        self.assertEqual(v.unit, "mg/kg")
        self.assertEqual(v.raw_unit, "ug/kg")

    def test_censored_ug_conversion(self):
        v = parse_value("<2", "concentration", "μg/kg")
        self.assertEqual(v.kind, "censored")
        self.assertAlmostEqual(v.value, 0.002)
        self.assertEqual(v.unit, "mg/kg")

    def test_equivalent_unit_expressions_compare_equal(self):
        a = parse_value("35", "concentration", "mg/kg").to_dict()
        b = parse_value("35000", "concentration", "μg kg-1").to_dict()
        self.assertTrue(values_equivalent(a, b))

    def test_fullmatch_rejects_garbage(self):
        v = parse_value("N/A", "concentration", "mg/kg")
        self.assertEqual(v.kind, "missing")
        self.assertIsNone(v.value)

    def test_header_unit_extraction(self):
        label, unit = extract_unit("Au (mg/kg)")
        self.assertEqual(label, "Au")
        self.assertEqual(unit, "mg/kg")
        label2, unit2 = extract_unit("Depth（cm）")
        self.assertEqual(label2, "Depth")
        self.assertEqual(unit2, "cm")

    def test_unit_alias_tables(self):
        self.assertEqual(normalize_unit("毫克/千克", "concentration"), "mg/kg")
        self.assertEqual(normalize_unit("ppb", "concentration"), "ug/kg")
        self.assertEqual(normalize_unit("厘米", "depth"), "cm")


class DirtyHtmlTest(unittest.TestCase):
    def test_unclosed_td_and_paragraph(self):
        dom = parse_html("<table><tr><td>a<td>b<tr><td>c<td>d</table><p>tail")
        tds = dom.find_all("td")
        self.assertEqual([td.normalize_text() for td in tds], ["a", "b", "c", "d"])
        # The implicitly-closed paragraph still exists and keeps its text.
        self.assertTrue(any(node.normalize_text() == "tail" for node in dom.iter_descendants()))

    def test_rowspan_colspan_grid(self):
        html = (
            "<table><thead><tr><th rowspan='2'>S</th>"
            "<th colspan='2'>G</th></tr><tr><th>x</th><th>y</th></tr></thead>"
            "<tbody><tr><td>S1</td><td>1</td><td>2</td></tr></tbody></table>"
        )
        dom = parse_html(html)
        table = build_table(dom.find("table"), header_layers=2)
        self.assertEqual(table.ncols, 3)
        headers = table.header_columns()
        self.assertEqual(headers[0], ["S", "S"])       # rowspan repeats down
        self.assertEqual(headers[1], ["G", "x"])       # colspan + second layer
        self.assertEqual(headers[2], ["G", "y"])
        body = table.body_rows()
        self.assertEqual([c.clean for c in body[0]], ["S1", "1", "2"])

    def test_detect_header_layers(self):
        dom = parse_html("<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>")
        self.assertEqual(detect_header_layers(dom.find("table")), 1)


class DslTest(unittest.TestCase):
    def test_flow_list_with_bare_strings(self):
        data = dsl.parse("labels: [区间编号, 岩性]\nversion: \"1\"\n")
        self.assertEqual(data["labels"], ["区间编号", "岩性"])
        self.assertEqual(data["version"], "1")

    def test_nested_roundtrip(self):
        text = "name: x\npage:\n  tables:\n    t:\n      selector: t.a\n      identity:\n        - sample_id\n"
        data = dsl.parse(text)
        self.assertEqual(data["page"]["tables"]["t"]["identity"], ["sample_id"])
        self.assertEqual(dsl.parse(dsl.dump(data)), data)


if __name__ == "__main__":
    unittest.main()
