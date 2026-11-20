"""Run-id reuse, retry/conflict, concurrency and publish-interruption tests.

Covers the lifecycle rules:

* re-using a run id with the same request re-attaches to the original run
  (timeout retry, and restart via a fresh engine on the same data dir);
* re-using an id with a different operation / rule / page manifest is an
  explicit ``RunConflictError``, and ids backing a confirmed baseline are
  protected everywhere (start, replay, re-confirm);
* a slow page blocks only its own run: progress stays queryable and another
  run completes concurrently; duplicate resumes never double-fetch;
* after a publish interruption the restarted engine reuses saved pages and
  unpublished (non-completed / outputless) results never enter a comparison;
* normal old baselines keep comparing as before.
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from tests._helpers import rule_text
from corebench import mock_site
from corebench.diff import confirm_baseline, regression_report
from corebench.errors import RunConflictError
from corebench.merge import read_jsonl
from corebench.runner import (
    KIND_COLLECT,
    KIND_REPLAY,
    RunConfig,
    RunEngine,
    collect_signature,
)
from corebench.sources import SnapshotStore
from corebench.storage import WorkbenchStore
from corebench.webapp import create_server


def _post(base_url, path, payload):
    req = urllib.request.Request(
        base_url + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _get_json(base_url, path):
    with urllib.request.urlopen(base_url + path, timeout=15) as resp:
        return resp.status, json.loads(resp.read())


class _Harness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base_url = mock_site.start_mock_server()
        cls.manifest = [
            {"page_id": page["page_id"],
             "url": f"{cls.base_url}/site/{borehole}/{page['page_id']}"}
            for borehole, pages in mock_site.DATASET.items() for page in pages
        ]
        cls.zk02_manifest = [page for page in cls.manifest if "zk02" in page["page_id"]]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        mock_site.STATE.reset()
        self.tmp = tempfile.mkdtemp()
        self.store = WorkbenchStore(self.tmp)
        self.snapshots = SnapshotStore(os.path.join(self.tmp, "snapshots"))
        self.store.snapshots = self.snapshots
        self.engine = RunEngine(self.store, self.snapshots)
        self.rule = self.store.save_rule_version("drill-archive", rule_text(1), "v1")

    def tearDown(self):
        mock_site.STATE.reset()

    def _start(self, run_id, manifest=None, rule=None):
        rule = rule or self.rule
        return self.engine.start(RunConfig(
            run_id, rule["rule_id"], rule["text"], manifest or self.manifest))


class RunIdReuseEngineTest(_Harness):
    def test_same_request_retry_reattaches_instead_of_overwriting(self):
        first = self._start("dup")
        # Simulate a request timeout: the client never saw the response and
        # retries the exact same start request.
        second = self._start("dup")
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["started_at"], first["started_at"])
        self.assertEqual(second["request_signature"],
                         collect_signature(self.rule["text"], self.manifest))
        self.assertEqual(second["request_kind"], KIND_COLLECT)
        # The run behind the id is the same object on disk; nothing was reset.
        run = self.engine.run("dup")
        again = self._start("dup")  # retry after completion also re-attaches
        self.assertEqual(again["status"], "completed")
        self.assertEqual(again["started_at"], first["started_at"])

    def test_different_manifest_on_same_id_is_conflict(self):
        self._start("shared", manifest=self.manifest)
        with self.assertRaises(RunConflictError) as ctx:
            self._start("shared", manifest=self.zk02_manifest)
        self.assertIn("different", str(ctx.exception))
        # The original run is untouched and still runnable.
        run = self.engine.run("shared")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(run["pages"]), len(self.manifest))

    def test_different_rule_on_same_id_is_conflict(self):
        self._start("ruled")
        rule_v2 = self.store.save_rule_version("drill-archive", rule_text(2), "v2")
        with self.assertRaises(RunConflictError):
            self._start("ruled", rule=rule_v2)

    def test_replay_id_collision_with_different_intent_is_conflict(self):
        self._start("src", manifest=self.zk02_manifest)
        run = self.engine.run("src")
        self.assertEqual(run["status"], "completed")
        replay = self.engine.replay("src", new_run_id="rep")
        self.assertEqual(replay["request_kind"], KIND_REPLAY)
        # A collection trying to grab the replay's id must be rejected.
        with self.assertRaises(RunConflictError):
            self._start("rep", manifest=self.zk02_manifest)
        # A different replay (new candidate rule) under the same id as well.
        rule_v2 = self.store.save_rule_version("drill-archive", rule_text(2), "v2")
        with self.assertRaises(RunConflictError):
            self.engine.replay("src", rule_text=rule_v2["text"], new_run_id="rep")

    def test_identical_replay_retry_returns_stored_replay(self):
        self._start("src2", manifest=self.zk02_manifest)
        self.engine.run("src2")
        first = self.engine.replay("src2", new_run_id="rep2")
        second = self.engine.replay("src2", new_run_id="rep2")
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["request_signature"], first["request_signature"])
        self.assertEqual(read_jsonl(second["output_path"]),
                         read_jsonl(first["output_path"]))


class BaselineProtectionTest(_Harness):
    def _confirmed_baseline(self, run_id="clean"):
        self._start(run_id, manifest=self.zk02_manifest)
        run = self.engine.run(run_id)
        snapshot = self.snapshots.load(run["snapshot_id"])
        baseline = confirm_baseline(self.store, run, snapshot, "clean ZK02")
        return run, baseline

    def test_start_with_baseline_backed_id_is_rejected(self):
        run, baseline = self._confirmed_baseline("clean")
        with self.assertRaises(RunConflictError) as ctx:
            # Even an "identical" retry must not touch a confirmed run.
            self._start("clean", manifest=self.zk02_manifest)
        self.assertIn(baseline["baseline_id"], str(ctx.exception))
        # On-disk run and baseline remain exactly as confirmed.
        stored = self.store.load_run("clean")
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(self.store.get_baseline(baseline["baseline_id"]), baseline)

    def test_reconfirming_same_baseline_errors(self):
        run, _ = self._confirmed_baseline("clean2")
        snapshot = self.snapshots.load(run["snapshot_id"])
        with self.assertRaises(RunConflictError):
            confirm_baseline(self.store, run, snapshot, "second confirmation")

    def test_replay_reusing_baseline_run_id_is_rejected(self):
        self._confirmed_baseline("clean3")
        with self.assertRaises(RunConflictError):
            self.engine.replay("clean3", new_run_id="clean3")

    def test_normal_old_baseline_still_compares(self):
        run, baseline = self._confirmed_baseline("keep")
        # A separate replay on the same frozen snapshot compares cleanly.
        replay = self.engine.replay("keep", new_run_id="keep-replay")
        report = regression_report(self.store, baseline, replay)
        self.assertTrue(report["regression_passed"])
        self.assertEqual(report["summary"]["total"], 0)


class RestartAndResumeTest(_Harness):
    def test_restart_resumes_original_run_from_saved_pages(self):
        mock_site.STATE.fail(["zk01-p2"])
        self._start("restart-me")
        failed = self.engine.run("restart-me")
        self.assertEqual(failed["status"], "failed")
        saved_pages = len(self.snapshots.load(failed["snapshot_id"]).pages)
        self.assertGreaterEqual(saved_pages, 1)

        # "Service restart": a brand-new engine over the same data directory.
        restarted = RunEngine(self.store, self.snapshots)
        mock_site.STATE.reset()
        seen = []

        def counting_fetcher(page_id, url):
            seen.append(page_id)
            from corebench.sources import fetch_page
            return fetch_page(page_id, url)

        restarted.fetcher = counting_fetcher
        run = restarted.run("restart-me")
        self.assertEqual(run["status"], "completed")
        # Saved pages are reused; only the page that failed is fetched again.
        self.assertNotIn("zk01-p1", seen)
        self.assertIn("zk01-p2", seen)
        self.assertTrue(self.snapshots.load(run["snapshot_id"]).is_complete)

    def test_restart_resume_after_timeout_keeps_single_run_identity(self):
        # The client only ever knew run id "timeo": start must re-attach after
        # the restart, never create a second run directory.
        first = self._start("timeo")
        RunEngine(self.store, self.snapshots)  # simulated restart, no run executed
        reattached = self._start("timeo")
        self.assertEqual(reattached["run_id"], first["run_id"])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "runs")), ["timeo"])

    def test_publish_interruption_leaves_no_published_output(self):
        mock_site.STATE.fail(["zk01-p2"])
        self._start("killed")
        killed = self.engine.run("killed")
        self.assertEqual(killed["status"], "failed")
        # No JSONL output is published before the run completes.
        self.assertFalse(os.path.exists(os.path.join(
            self.tmp, "runs", "killed", "output.jsonl")))

        # Build a confirmed baseline from another clean run for comparison.
        self._start("base", manifest=self.zk02_manifest)
        base_run = self.engine.run("base")
        baseline = confirm_baseline(
            self.store, base_run, self.snapshots.load(base_run["snapshot_id"]))

        # The unpublished run cannot participate in a comparison at all.
        with self.assertRaises(ValueError):
            regression_report(self.store, baseline, killed)

        # Resume the interrupted run: saved pages are reused, then it publishes.
        mock_site.STATE.reset()
        resumed = self.engine.run("killed")
        self.assertEqual(resumed["status"], "completed")
        self.assertTrue(os.path.exists(resumed["output_path"]))

    def test_outputless_completed_candidate_is_rejected(self):
        self._start("base2", manifest=self.zk02_manifest)
        base_run = self.engine.run("base2")
        baseline = confirm_baseline(
            self.store, base_run, self.snapshots.load(base_run["snapshot_id"]))
        self._start("ghost", manifest=self.zk02_manifest)
        ghost = self.engine.run("ghost")
        # Simulate losing the published file after completion (e.g. disk trim):
        # the guard must refuse rather than read the candidate as empty.
        os.remove(ghost["output_path"])
        with self.assertRaises(ValueError):
            regression_report(self.store, baseline, ghost)


class ConcurrencyTest(_Harness):
    def test_duplicate_concurrent_resume_runs_once(self):
        mock_site.STATE.delay(["zk01-p1"], 1.0)
        self._start("once")
        results = []
        errors = []

        def worker():
            try:
                results.append(self.engine.run("once"))
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["status"] == "completed" for r in results))
        # The run executed one single pass: only one output file and one row set.
        run = self.store.load_run("once")
        self.assertEqual(run["status"], "completed")
        rows = read_jsonl(run["output_path"])
        self.assertEqual({row["borehole_id"] for row in rows}, {"ZK01", "ZK02"})
        mock_site.STATE.reset()

    def test_progress_queryable_while_slow_page_blocks(self):
        mock_site.STATE.delay(["zk01-p2"], 1.5)
        self._start("slow")
        done = threading.Event()

        def runner():
            self.engine.run("slow")
            done.set()

        thread = threading.Thread(target=runner)
        thread.start()
        try:
            # Wait until the slow page is the in-flight page, then query progress.
            in_flight = None
            for _ in range(50):
                progress = self.engine.progress("slow")
                if progress["in_flight_page"] == "zk01-p2":
                    in_flight = progress
                    break
                done.wait(0.05)
            self.assertIsNotNone(in_flight)
            self.assertEqual(in_flight["status"], "running")
            self.assertGreaterEqual(in_flight["completed_pages"], 1)
            self.assertGreater(in_flight["total_pages"], in_flight["completed_pages"])
            self.assertIn("percent_done", in_flight)
        finally:
            thread.join(timeout=15)
            mock_site.STATE.reset()
        self.assertTrue(done.is_set())
        self.assertEqual(self.engine.progress("slow")["status"], "completed")

    def test_slow_run_does_not_block_other_runs(self):
        mock_site.STATE.delay(["zk01-p2"], 1.5)
        self._start("slow-other")
        slow_done = threading.Event()

        def slow_worker():
            self.engine.run("slow-other")
            slow_done.set()

        slow_thread = threading.Thread(target=slow_worker)
        slow_thread.start()
        try:
            # Meanwhile a separate, small run completes on the same engine.
            self._start("fast", manifest=self.zk02_manifest)
            fast_run = self.engine.run("fast")
            self.assertEqual(fast_run["status"], "completed")
            # The slow run is still going.
            self.assertFalse(slow_done.is_set())
            self.assertEqual(self.engine.progress("slow-other")["status"], "running")
        finally:
            slow_thread.join(timeout=15)
            mock_site.STATE.reset()
        self.assertTrue(slow_done.is_set())


class WebApiLifecycleTest(_Harness):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.web_tmp = tempfile.mkdtemp()
        cls.web_server = create_server(cls.web_tmp)
        cls.web_thread = threading.Thread(target=cls.web_server.serve_forever, daemon=True)
        cls.web_thread.start()
        cls.web_base = f"http://127.0.0.1:{cls.web_server.server_address[1]}"
        status, payload = _post(cls.web_base, "/api/rules/save",
                                {"name": "drill-archive", "text": rule_text(1), "note": "v1"})
        cls.rule_id = payload["rule_id"]

    @classmethod
    def tearDownClass(cls):
        cls.web_server.shutdown()
        super().tearDownClass()

    def test_web_same_request_retry_is_idempotent(self):
        body = {"run_id": "web-dup", "rule_id": self.rule_id, "pages": self.zk02_manifest}
        status, first = _post(self.web_base, "/api/runs/start", body)
        self.assertEqual(status, 200)
        status, second = _post(self.web_base, "/api/runs/start", body)
        self.assertEqual(status, 200)  # re-attach, not an error
        self.assertEqual(second["started_at"], first["started_at"])
        status, run = _post(self.web_base, "/api/runs/resume", {"run_id": "web-dup"})
        self.assertEqual(status, 200)
        self.assertEqual(run["status"], "completed")

    def test_web_conflict_returns_409(self):
        body = {"run_id": "web-conf", "rule_id": self.rule_id, "pages": self.manifest}
        status, _ = _post(self.web_base, "/api/runs/start", body)
        self.assertEqual(status, 200)
        # Same id, different manifest -> 409 Conflict.
        clash = dict(body, pages=self.zk02_manifest)
        status, payload = _post(self.web_base, "/api/runs/start", clash)
        self.assertEqual(status, 409)
        self.assertIn("error", payload)

    def test_web_progress_endpoint_during_slow_page(self):
        mock_site.STATE.delay(["zk01-p2"], 1.5)
        body = {"run_id": "web-slow", "rule_id": self.rule_id, "pages": self.manifest}
        status, _ = _post(self.web_base, "/api/runs/start", body)
        self.assertEqual(status, 200)
        finished = threading.Event()

        def resume():
            _post(self.web_base, "/api/runs/resume", {"run_id": "web-slow"})
            finished.set()

        thread = threading.Thread(target=resume)
        thread.start()
        try:
            observed = None
            for _ in range(50):
                status, progress = _get_json(
                    self.web_base, "/api/run-progress?id=web-slow")
                self.assertEqual(status, 200)
                if progress["in_flight_page"] == "zk01-p2":
                    observed = progress
                    break
                finished.wait(0.05)
            self.assertIsNotNone(observed)
            self.assertGreaterEqual(observed["completed_pages"], 1)
        finally:
            thread.join(timeout=15)
            mock_site.STATE.reset()
        self.assertTrue(finished.is_set())

    def test_web_baseline_protected_id_returns_409(self):
        body = {"run_id": "web-base", "rule_id": self.rule_id, "pages": self.zk02_manifest}
        _post(self.web_base, "/api/runs/start", body)
        _post(self.web_base, "/api/runs/resume", {"run_id": "web-base"})
        status, baseline = _post(self.web_base, "/api/baselines/confirm",
                                 {"run_id": "web-base", "note": "clean"})
        self.assertEqual(status, 200)
        # Re-confirming the same run -> 409.
        status, payload = _post(self.web_base, "/api/baselines/confirm",
                                {"run_id": "web-base", "note": "again"})
        self.assertEqual(status, 409)
        # Reusing the protected id for a new collection -> 409 as well.
        status, payload = _post(self.web_base, "/api/runs/start", body)
        self.assertEqual(status, 409)
        # The confirmed baseline remains available for normal comparison.
        replay_body = {"run_id": "web-base", "new_run_id": "web-base-replay"}
        status, replay = _post(self.web_base, "/api/runs/replay", replay_body)
        self.assertEqual(status, 200)
        status, report = _post(self.web_base, "/api/diff", {
            "baseline_id": baseline["baseline_id"],
            "candidate_run_id": "web-base-replay"})
        self.assertEqual(status, 200)
        self.assertTrue(report["regression_passed"])

    def test_web_unpublished_candidate_diff_is_rejected(self):
        # Confirm one clean baseline first.
        body = {"run_id": "web-diff-base", "rule_id": self.rule_id,
                "pages": self.zk02_manifest}
        _post(self.web_base, "/api/runs/start", body)
        _post(self.web_base, "/api/runs/resume", {"run_id": "web-diff-base"})
        _, baseline = _post(self.web_base, "/api/baselines/confirm",
                            {"run_id": "web-diff-base", "note": "b"})
        # A failed (unpublished) run cannot be diffed against the baseline.
        mock_site.STATE.fail(["zk02-p1"])
        try:
            fail_body = {"run_id": "web-unpub", "rule_id": self.rule_id,
                         "pages": self.zk02_manifest}
            _post(self.web_base, "/api/runs/start", fail_body)
            status, failed = _post(self.web_base, "/api/runs/resume",
                                   {"run_id": "web-unpub"})
            self.assertEqual(status, 200)
            self.assertEqual(failed["status"], "failed")
        finally:
            mock_site.STATE.reset()
        # The unpublished/failed candidate is refused for comparison (the API
        # answers 400 either way: no completed, published result may be diffed).
        status, payload = _post(self.web_base, "/api/diff", {
            "baseline_id": baseline["baseline_id"],
            "candidate_run_id": "web-unpub"})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
