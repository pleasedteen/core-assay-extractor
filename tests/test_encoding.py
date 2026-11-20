"""Per-page encoding: rule corrections, mixed batches, replay without source.

Old archive pages are often GB18030 while the HTTP header claims UTF-8, and one
batch may mix encodings.  These checks use both a local file source and the
in-process HTTP site (never a real host) to prove that:

* a rule can correct the encoding of one named page in a mixed batch;
* a page whose bytes cannot be decoded keeps its original bytes and stays
  replayable instead of being turned into replacement-character text;
* after the source is shut down, the same snapshot still replays and can be
  compared against a baseline;
* inspecting the original page reports the encoding the corresponding run
  actually used and the text it derived;
* a snapshot that only stored decoded text keeps replaying with that text.
"""
import json
import os
import tempfile
import unittest

from corebench import mock_site
from corebench.diff import compare_runs, confirm_baseline, regression_report
from corebench.merge import read_jsonl
from corebench.rules import EncodingSpec, Rule
from corebench.runner import RunConfig, RunEngine
from corebench.sources import SnapshotStore
from corebench.storage import WorkbenchStore
from corebench.webapp import create_server
from tests._helpers import rule_text


def _legacy_manifest(base_url):
    return [
        {"page_id": page["page_id"], "url": f"{base_url}/legacy/{page['page_id']}"}
        for page in mock_site.LEGACY_DATASET
    ]


def _with_encoding(base, pages=None, default=None):
    """Append an ``encoding:`` block to a rule text."""
    lines = ["encoding:"]
    if default:
        lines.append(f"  default: {default}")
    if pages:
        lines.append("  pages:")
        for page_id, encoding in pages.items():
            lines.append(f"    {page_id}: {encoding}")
    return base.rstrip("\n") + "\n" + "\n".join(lines) + "\n"


def _by_page(run):
    return {item["page_id"]: item for item in run["page_encodings"]}


class EncodingRuleTest(unittest.TestCase):
    def test_rule_parses_and_validates_encoding_block(self):
        rule = Rule.from_text(_with_encoding(
            rule_text(1), pages={"p2": "GB18030"}, default="utf-8"))
        self.assertEqual(rule.encoding.default, "utf-8")
        self.assertEqual(rule.encoding.for_page("p2"), "gb18030")
        self.assertIsNone(rule.encoding.for_page("p1"))
        # A typo must fail at rule load time, not silently at runtime.
        with self.assertRaises(ValueError):
            Rule.from_text(_with_encoding(rule_text(1), pages={"p1": "not-a-codec"}))
        # Old rules without the section still load.
        self.assertTrue(Rule.from_text(rule_text(1)).encoding.is_empty())

    def test_encoding_spec_for_page_precedence(self):
        spec = EncodingSpec(default="utf-8", pages={"special": "gb18030"})
        self.assertEqual(spec.for_page("special"), "gb18030")
        self.assertEqual(spec.for_page("other"), None)
        self.assertEqual(spec.default, "utf-8")

    def test_over_http_mixed_batch_corrects_one_page(self):
        server, base_url = mock_site.start_mock_server()
        try:
            tmp = tempfile.mkdtemp()
            store = WorkbenchStore(tmp)
            snapshots = SnapshotStore(os.path.join(tmp, "snapshots"))
            store.snapshots = snapshots
            engine = RunEngine(store, snapshots)
            rule = store.save_rule_version("legacy", rule_text(1), "no encoding")
            engine.start(RunConfig("enc", rule["rule_id"], rule["text"],
                                   _legacy_manifest(base_url)))
            engine.run("enc")
            # Without a correction the mislabeled GB18030 page cannot be read.
            pages = _by_page(store.load_run("enc"))
            self.assertEqual(pages["lp-gb-mislabeled"]["status"], "undecodable")
            self.assertEqual(pages["lp-gb-mislabeled"]["encoding"], "utf-8")
            # Correct only that page; the other pages keep their own choice.
            fixed_text = _with_encoding(rule_text(1),
                                        pages={"lp-gb-mislabeled": "gb18030"})
            replay = engine.replay("enc", rule_text=fixed_text, new_run_id="enc-fixed")
            pages = _by_page(replay)
            self.assertEqual(pages["lp-gb-mislabeled"]["encoding"], "gb18030")
            self.assertEqual(pages["lp-gb-mislabeled"]["encoding_source"], "rule-page")
            self.assertTrue(pages["lp-gb-mislabeled"]["overridden"])
            self.assertEqual(pages["lp-gb-mislabeled"]["status"], "decoded")
            # The already-honest GB page is untouched by the per-page override.
            self.assertEqual(pages["lp-gb-honest"]["encoding"], "gb18030")
            self.assertFalse(pages["lp-gb-honest"]["overridden"])
            # Chinese cell text came out correctly on both GB18030 pages.
            rows = read_jsonl(replay["output_path"])
            samples = {s["fields"]["sample_id"]["value"]: s
                       for row in rows for s in row["samples"]}
            self.assertEqual(samples["L2"]["fields"]["lithology"]["value"], "细粒花岗岩")
            self.assertEqual(samples["L3"]["fields"]["lithology"]["value"], "闪长岩")
        finally:
            server.shutdown()

    def test_undecodable_page_keeps_bytes_and_never_fakes_text(self):
        server, base_url = mock_site.start_mock_server()
        try:
            tmp = tempfile.mkdtemp()
            store = WorkbenchStore(tmp)
            snapshots = SnapshotStore(os.path.join(tmp, "snapshots"))
            store.snapshots = snapshots
            engine = RunEngine(store, snapshots)
            rule = store.save_rule_version("legacy", rule_text(1), "no encoding")
            engine.start(RunConfig("broken", rule["rule_id"], rule["text"],
                                   _legacy_manifest(base_url)))
            run = engine.run("broken")
            pages = _by_page(run)
            self.assertEqual(pages["lp-broken"]["status"], "undecodable")
            self.assertIn("decode_error", pages["lp-broken"])
            snapshot = snapshots.load(run["snapshot_id"])
            page = snapshot.get("lp-broken")
            # The original bytes survive byte-for-byte in the snapshot.
            self.assertIsNotNone(page.raw)
            self.assertEqual(page.raw, mock_site.legacy_page_bytes(
                next(p for p in mock_site.LEGACY_DATASET if p["page_id"] == "lp-broken")))
            self.assertEqual(page.html, "")
            rows = read_jsonl(run["output_path"])
            decode_warnings = [w for row in rows for w in row["warnings"]
                               if w["code"] == "DECODE_FAILED"
                               and w["page_id"] == "lp-broken"]
            self.assertEqual(len(decode_warnings), 1)
            # No samples/measurements were invented for the broken page.
            self.assertFalse(any(s["fields"]["sample_id"]["value"] == "L4"
                                 for row in rows for s in row["samples"]))
            # Even a UTF-8 default cannot rescue bytes that are invalid here.
            replay = engine.replay("broken", rule_text=_with_encoding(
                rule_text(1), default="utf-8"), new_run_id="broken-retry")
            self.assertEqual(_by_page(replay)["lp-broken"]["status"], "undecodable")
        finally:
            server.shutdown()


class ReplayAfterSourceGoneTest(unittest.TestCase):
    def test_replay_and_compare_after_source_closed(self):
        server, base_url = mock_site.start_mock_server()
        tmp = tempfile.mkdtemp()
        store = WorkbenchStore(tmp)
        snapshots = SnapshotStore(os.path.join(tmp, "snapshots"))
        store.snapshots = snapshots
        engine = RunEngine(store, snapshots)
        rule = store.save_rule_version("legacy", rule_text(1), "run")
        engine.start(RunConfig("src", rule["rule_id"], rule["text"],
                               _legacy_manifest(base_url)))
        run = engine.run("src")
        snapshot = snapshots.load(run["snapshot_id"])
        baseline = confirm_baseline(store, run, snapshot)
        # The origin is gone; nothing may be refetched from this point on.
        server.shutdown()

        def refusing_fetcher(page_id, url):
            raise AssertionError(f"must not refetch {url}")

        replay_engine = RunEngine(store, snapshots, fetcher=refusing_fetcher)
        corrected = _with_encoding(rule_text(1),
                                   pages={"lp-gb-mislabeled": "gb18030"},
                                   default="utf-8")
        # A UTF-8 default breaks the honest GB18030 page, so name it too.
        corrected = corrected.replace(
            "  pages:\n", "  pages:\n    lp-gb-honest: gb18030\n")
        replay = replay_engine.replay("src", rule_text=corrected, new_run_id="src-fixed")
        self.assertEqual(replay["snapshot_id"], run["snapshot_id"])
        pages = _by_page(replay)
        self.assertEqual(pages["lp-gb-mislabeled"]["status"], "decoded")
        self.assertEqual(pages["lp-gb-honest"]["status"], "decoded")

        # The same frozen snapshot still yields a semantic comparison.
        report = regression_report(store, baseline, replay)
        self.assertEqual(report["snapshot_id"], baseline["snapshot_id"])
        self.assertTrue(report["regression_passed"], report["changes"])
        # The mislabeled page now contributes a sample and measurement that
        # were previously unreadable: reported as additions, not failures.
        categories = {change["category"] for change in report["changes"]}
        self.assertTrue(categories <= {"added"}, report["changes"])
        self.assertIn("added", categories)
        # lp-broken cannot be rescued by any rule, so it is still reported as a
        # decode failure: a passing summary never hides unreadable content.
        failures = {item["page_id"] for item in report["candidate_decode_failures"]}
        self.assertEqual(failures, {"lp-broken"})
        # Comparing against the *original* run reports its decode failures too.
        reverse = compare_runs(read_jsonl(replay["output_path"]), read_jsonl(run["output_path"]))
        self.assertIn("DECODE_FAILED", json.dumps(reverse))

    def test_legacy_text_only_snapshot_replays_original_text(self):
        # A snapshot written before raw bytes were stored: only decoded text.
        tmp = tempfile.mkdtemp()
        store = WorkbenchStore(tmp)
        snapshots = SnapshotStore(os.path.join(tmp, "snapshots"))
        store.snapshots = snapshots
        html = mock_site.render_page("ZK02", mock_site.DATASET["ZK02"][0])
        legacy_payload = {
            "snapshot_id": "snap-legacy",
            "pages": [{
                "page_id": "zk02-p1",
                "url": "file:///gone/zk02-p1.html",
                "html": html,
                "content_sha1": __import__("hashlib").sha1(html.encode("utf-8")).hexdigest(),
            }],
            "failures": {},
        }
        with open(os.path.join(snapshots.root, "snap-legacy.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(legacy_payload, handle, ensure_ascii=False)
        rule = store.save_rule_version("legacy", rule_text(1), "text-only")
        # Record a run over the legacy snapshot with no source at all.
        store.save_run({
            "run_id": "old", "rule_id": rule["rule_id"], "rule_text": rule["text"],
            "note": "", "status": "completed",
            "pages": [{"page_id": "zk02-p1", "url": legacy_payload["pages"][0]["url"],
                       "state": "completed", "error": None}],
            "snapshot_id": "snap-legacy", "started_at": "x", "finished_at": "x",
            "has_conflicts": False, "has_warnings": False, "output_path": None,
            "error": None,
        })
        engine = RunEngine(store, snapshots, fetcher=lambda page_id, url: (
            _ for _ in ()).throw(AssertionError("no fetch")))
        # A rule asking for a different encoding cannot rewrite text-only pages.
        replay = engine.replay(
            "old", rule_text=_with_encoding(rule_text(1), default="gb18030"),
            new_run_id="old-replay")
        self.assertTrue(_by_page(replay)["zk02-p1"]["legacy_text_only"])
        rows = read_jsonl(replay["output_path"])
        self.assertEqual({row["borehole_id"] for row in rows}, {"ZK02"})
        measurements = {m["identity"]: m["value"]["value"]
                        for row in rows for m in row["measurements"]}
        self.assertAlmostEqual(measurements["sample=T201|analyte=Au|method=FA"], 0.21)
        # The derived text is exactly the original text, not GB18030 mojibake:
        # Chinese lithology survives untouched despite the rule asking for GB.
        lithology = {s["fields"]["sample_id"]["value"]: s["fields"]["lithology"]["value"]
                     for row in rows for s in row["samples"]}
        self.assertEqual(lithology["T201"], "气孔玄武岩")


class FileSourceEncodingTest(unittest.TestCase):
    def test_file_source_uses_rule_encoding_and_reports_it(self):
        tmp = tempfile.mkdtemp()
        site_dir = tempfile.mkdtemp()
        manifest = mock_site.write_legacy_static_site(site_dir)
        store = WorkbenchStore(tmp)
        snapshots = SnapshotStore(os.path.join(tmp, "snapshots"))
        store.snapshots = snapshots
        engine = RunEngine(store, snapshots)
        fixed = _with_encoding(rule_text(1), pages={"lp-gb-mislabeled": "gb18030"})
        rule = store.save_rule_version("legacy", fixed, "file run")
        engine.start(RunConfig("files", rule["rule_id"], rule["text"], manifest))
        run = engine.run("files")
        pages = _by_page(run)
        # A file has no header charset; the rule is the only correction.
        self.assertEqual(pages["lp-gb-mislabeled"]["encoding"], "gb18030")
        self.assertEqual(pages["lp-gb-mislabeled"]["encoding_source"], "rule-page")
        self.assertEqual(pages["lp-gb-mislabeled"]["declared_encoding"], None)
        self.assertEqual(pages["lp-utf8"]["encoding"], "utf-8")
        rows = read_jsonl(run["output_path"])
        identities = {m["identity"] for row in rows for m in row["measurements"]}
        self.assertIn("sample=L2|analyte=Cu|method=ICP-AES", identities)


class EncodingApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock, cls.mock_url = mock_site.start_mock_server()
        cls.tmp = tempfile.mkdtemp()
        cls.server = create_server(cls.tmp)
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.mock.shutdown()

    def _post(self, path, payload):
        import urllib.request
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def _get(self, path):
        import urllib.request
        with urllib.request.urlopen(self.base + path, timeout=10) as resp:
            return json.loads(resp.read())

    def test_snapshot_page_reports_run_encoding_and_correction(self):
        rule_text_value = rule_text(1)  # no encoding correction: raw header wins
        rule = self._post("/api/rules/save",
                          {"name": "legacy", "text": rule_text_value, "note": "enc"})
        run_id = "api-raw"
        self._post("/api/runs/start", {"run_id": run_id, "rule_id": rule["rule_id"],
                                       "pages": _legacy_manifest(self.mock_url)})
        self._post("/api/runs/resume", {"run_id": run_id})
        run = self._get(f"/api/run?id={run_id}")
        page = self._get(
            f"/api/snapshot-page?snapshot_id={run['snapshot_id']}"
            f"&page_id=lp-gb-mislabeled&run_id={run_id}")
        # Uncorrected, the run trusted the lying header and failed to decode.
        self.assertEqual(page["encoding"], "utf-8")
        self.assertEqual(page["run_encoding"]["encoding_source"], "header")
        self.assertEqual(page["run_encoding"]["declared_encoding"], "utf-8")
        self.assertFalse(page["run_encoding"]["overridden"])
        self.assertTrue(page["undecodable"])
        self.assertTrue(page["has_raw_bytes"])
        # A replay that corrects the encoding shows the corrected text as well.
        fixed = _with_encoding(rule_text(1), pages={"lp-gb-mislabeled": "gb18030"})
        candidate = self._post("/api/rules/save",
                               {"name": "legacy-fixed", "text": fixed, "note": "fix"})
        self._post("/api/runs/replay", {"run_id": "api-raw", "candidate_rule_id": candidate["rule_id"],
                                        "new_run_id": "api-fixed"})
        fixed_run = self._get("/api/run?id=api-fixed")
        fixed_page = self._get(
            f"/api/snapshot-page?snapshot_id={fixed_run['snapshot_id']}"
            f"&page_id=lp-gb-mislabeled&run_id=api-fixed")
        self.assertEqual(fixed_page["run_encoding"]["encoding_source"], "rule-page")
        self.assertIn("细粒花岗岩", fixed_page["html"])
        # Inspecting with the *original* run still shows the original failure.
        raw_page = self._get(
            f"/api/snapshot-page?snapshot_id={fixed_run['snapshot_id']}"
            f"&page_id=lp-gb-mislabeled&run_id=api-raw")
        self.assertTrue(raw_page["undecodable"])
        self.assertEqual(raw_page["encoding"], "utf-8")
        # Viewing with another explicit encoding re-decodes differing text.
        alt = self._get(
            f"/api/snapshot-page?snapshot_id={run['snapshot_id']}"
            f"&page_id=lp-gb-mislabeled&encoding=utf-8&run_id={run_id}")
        self.assertEqual(alt["requested_encoding"], "utf-8")
        self.assertTrue(alt["undecodable"])
        self.assertEqual(alt["html"], "")
        # A failed page reports its bytes were kept rather than fake text.
        broken = self._get(
            f"/api/snapshot-page?snapshot_id={run['snapshot_id']}"
            f"&page_id=lp-broken&run_id={run_id}")
        self.assertTrue(broken["undecodable"])
        self.assertTrue(broken["has_raw_bytes"])
        self.assertIn("decode_error", broken)


if __name__ == "__main__":
    unittest.main()
