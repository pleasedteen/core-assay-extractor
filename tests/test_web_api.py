"""HTTP-level tests for the workbench API and static UI delivery."""
import json
import os
import tempfile
import unittest
import urllib.request
import urllib.error

from tests._helpers import rule_text
from corebench import mock_site
from corebench.webapp import create_server


def get(base_url, path):
    with urllib.request.urlopen(base_url + path, timeout=10) as resp:
        return resp.status, resp.headers.get("Content-Type"), resp.read()


def get_json(base_url, path):
    status, _ctype, body = get(base_url, path)
    return status, json.loads(body)


def post(base_url, path, payload):
    req = urllib.request.Request(
        base_url + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class WebApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock, cls.mock_url = mock_site.start_mock_server()
        cls.tmp = tempfile.mkdtemp()
        cls.server = create_server(cls.tmp)
        cls.server_thread = __import__("threading").Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.manifest = [
            {"page_id": page["page_id"],
             "url": f"{cls.mock_url}/site/{borehole}/{page['page_id']}"}
            for borehole, pages in mock_site.DATASET.items() for page in pages
        ]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.mock.shutdown()

    def test_01_ui_served(self):
        status, ctype, body = get(self.base, "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("corebench", body.decode("utf-8"))
        status, _, js = get(self.base, "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("addEventListener", js.decode("utf-8"))

    def test_02_rule_save_rejects_invalid(self):
        status, payload = post(self.base, "/api/rules/save",
                               {"name": "bad", "text": "name: broken\npage: {}\n"})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        status, saved = post(self.base, "/api/rules/save",
                             {"name": "drill-archive", "text": rule_text(1), "note": "v1"})
        self.assertEqual(status, 200)
        self.assertEqual(saved["version_number"], 1)

    def test_03_full_run_and_output(self):
        _, rules = get_json(self.base, "/api/rules")
        rule_id = rules["rules"][0]["rule_id"]
        status, run = post(self.base, "/api/runs/start",
                           {"run_id": "web-run", "rule_id": rule_id, "pages": self.manifest})
        self.assertEqual(status, 200)
        status, run = post(self.base, "/api/runs/resume", {"run_id": "web-run"})
        self.assertEqual(status, 200)
        self.assertEqual(run["status"], "completed")
        status, output = get_json(self.base, "/api/output?run_id=web-run")
        self.assertEqual(status, 200)
        self.assertEqual(len(output["lines"]), 2)

    def test_04_snapshot_traceability(self):
        _, run = get_json(self.base, "/api/run?id=web-run")
        snap_id = run["snapshot_id"]
        status, page = get_json(self.base, f"/api/snapshot-page?snapshot_id={snap_id}&page_id=zk01-p1")
        self.assertEqual(status, 200)
        self.assertIn("<table", page["html"])
        self.assertEqual(len(page["content_sha1"]), 40)

    def test_05_baseline_confirm_blocked_then_clean(self):
        # web-run contains the ZK01 conflict -> confirmation blocked.
        status, payload = post(self.base, "/api/baselines/confirm",
                               {"run_id": "web-run", "note": "should fail"})
        self.assertEqual(status, 400)
        # Clean ZK02-only run can be confirmed.
        _, rules = get_json(self.base, "/api/rules")
        rule_id = rules["rules"][0]["rule_id"]
        zk02 = [page for page in self.manifest if "zk02" in page["page_id"]]
        post(self.base, "/api/runs/start", {"run_id": "web-clean", "rule_id": rule_id, "pages": zk02})
        post(self.base, "/api/runs/resume", {"run_id": "web-clean"})
        status, baseline = post(self.base, "/api/baselines/confirm",
                                {"run_id": "web-clean", "note": "clean"})
        self.assertEqual(status, 200)
        self.assertTrue(baseline["baseline_id"].startswith("baseline-"))

    def test_06_diff_report_endpoint(self):
        # Replay the clean run with the same rule on the frozen snapshot:
        # an equivalent replay must yield an empty diff.
        _, rules = get_json(self.base, "/api/rules")
        rule_id = rules["rules"][0]["rule_id"]
        status, replay = post(self.base, "/api/runs/replay",
                              {"run_id": "web-clean", "candidate_rule_id": rule_id,
                               "new_run_id": "web-clean-v2"})
        self.assertEqual(status, 200)
        _, baselines = get_json(self.base, "/api/baselines")
        baseline_id = baselines["baselines"][0]["baseline_id"]
        status, report = post(self.base, "/api/diff",
                              {"baseline_id": baseline_id, "candidate_run_id": "web-clean-v2"})
        self.assertEqual(status, 200)
        self.assertTrue(report["regression_passed"])
        self.assertEqual(report["summary"]["total"], 0)
        # The report is persisted and retrievable as machine-readable JSON.
        status, fetched = get_json(self.base, f"/api/report?id={report['report_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["report_id"], report["report_id"])


if __name__ == "__main__":
    unittest.main()
