"""HTTP 429 retry-after handling, verified with two local sites.

Two in-process mock servers on ephemeral ports act as two independent
"sites".  The tests prove that while a site is on a retry-after hold it
receives zero requests, that the hold is shared across runs and survives a
(service) restart, that redirects into a held site are held too, and that
after recovery only the still-missing pages are fetched -- while ordinary
fetch failures keep the original fail-and-manually-resume behaviour.
"""
import email.utils
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse

from tests._helpers import rule_text
from corebench import mock_site
from corebench.runner import RunConfig, RunEngine
from corebench.sources import (
    DEFAULT_RETRY_AFTER,
    Fetcher,
    RateLimitedError,
    RateLimitStore,
    SnapshotStore,
    parse_retry_after,
)
from corebench.storage import WorkbenchStore


def wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TwoSiteTestCase(unittest.TestCase):
    """Two independent local sites (A and B) plus a workbench data dir."""

    def setUp(self):
        self.state_a = mock_site.MockState()
        self.state_b = mock_site.MockState()
        self.server_a, self.url_a = mock_site.start_mock_server(state=self.state_a)
        self.server_b, self.url_b = mock_site.start_mock_server(state=self.state_b)
        self.tmp = tempfile.mkdtemp()
        self.store = WorkbenchStore(self.tmp)
        self.snapshots = SnapshotStore(os.path.join(self.tmp, "snapshots"))
        self.rule = self.store.save_rule_version("drill-archive", rule_text(1), "v1")

    def tearDown(self):
        for server in (self.server_a, self.server_b):
            server.shutdown()
            server.server_close()

    def make_engine(self):
        # Fresh objects on the same data directory == a service restart.
        return RunEngine(WorkbenchStore(self.tmp),
                         SnapshotStore(os.path.join(self.tmp, "snapshots")))

    def manifest(self, *specs):
        bases = {"a": self.url_a, "b": self.url_b}
        return [{"page_id": page_id, "url": f"{bases[site]}/site/{borehole}/{page_id}"}
                for site, borehole, page_id in specs]

    def run_in_thread(self, engine, run_id):
        outcome = {}

        def target():
            outcome["run"] = engine.run(run_id)

        thread = threading.Thread(target=target)
        thread.start()
        return thread, outcome


class RateLimitedRunTest(TwoSiteTestCase):
    def test_429_defers_site_and_retries_only_missing_pages(self):
        engine = RunEngine(self.store, self.snapshots)
        engine.start(RunConfig("rl-1", self.rule["rule_id"], self.rule["text"],
                               self.manifest(("a", "ZK01", "zk01-p1"),
                                             ("a", "ZK01", "zk01-p2"),
                                             ("b", "ZK02", "zk02-p1"),
                                             ("a", "ZK01", "zk01-p3"))))

        # Arm site A's 1s 429 window right after its first page succeeds.
        inner = engine.fetcher

        def arm_after_first(page_id, url):
            page = inner(page_id, url)
            if "zk01-p1" in url:
                self.state_a.set_rate_limit(1.0)
            return page

        engine.fetcher = arm_after_first
        started = time.time()
        thread, outcome = self.run_in_thread(engine, "rl-1")

        # Mid-hold: p1 done, p2 got the 429, p3 was never requested, B went on.
        self.assertTrue(wait_for(lambda: self.state_a.request_count() >= 2))
        time.sleep(0.5)
        self.assertEqual(self.state_a.request_count(), 2)  # zero requests during the hold
        self.assertEqual(self.state_b.request_count(), 1)  # other site kept crawling
        mid_run = self.store.load_run("rl-1")
        self.assertEqual(mid_run["status"], "waiting")
        states = {p["page_id"]: p["state"] for p in mid_run["pages"]}
        self.assertEqual(states["zk01-p3"], "waiting")
        self.assertEqual(states["zk02-p1"], "completed")

        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        run = outcome["run"]
        self.assertEqual(run["status"], "completed")
        self.assertTrue(self.snapshots.load(run["snapshot_id"]).is_complete)
        # The run really waited for the retry-after moment.
        self.assertGreaterEqual(time.time() - started, 0.9)
        # After recovery only the missing pages were fetched: p1 exactly once.
        self.assertEqual(self.state_a.request_count(contains="zk01-p1"), 1)
        self.assertEqual(self.state_a.request_count(contains="zk01-p2"), 2)  # 429 + retry
        self.assertEqual(self.state_a.request_count(contains="zk01-p3"), 1)
        self.assertEqual(self.state_a.request_count(), 4)

    def test_hold_shared_across_runs_and_survives_restart(self):
        engine1 = RunEngine(self.store, self.snapshots)
        self.state_a.set_rate_limit(1.2)
        engine1.start(RunConfig("rl-a", self.rule["rule_id"], self.rule["text"],
                                self.manifest(("a", "ZK01", "zk01-p1"),
                                              ("b", "ZK02", "zk02-p1"))))
        thread1, outcome1 = self.run_in_thread(engine1, "rl-a")
        self.assertTrue(wait_for(lambda: self.state_a.request_count() >= 1))

        # The hold is persisted at its original absolute moment.
        registry_path = os.path.join(self.tmp, "rate_limits.json")
        with open(registry_path, encoding="utf-8") as handle:
            persisted = json.load(handle)
        site_a_key = urllib.parse.urlparse(self.url_a).netloc
        self.assertIn(site_a_key, persisted)
        deadline_before_restart = persisted[site_a_key]

        # Restart: brand-new engine/store objects on the same data directory.
        engine2 = self.make_engine()
        reloaded = RateLimitStore(registry_path).wait_until(
            f"{self.url_a}/site/ZK01/zk01-p2")
        self.assertAlmostEqual(reloaded, deadline_before_restart, places=3)

        # A second run on the restarted engine shares the hold: no requests to A.
        engine2.start(RunConfig("rl-b", self.rule["rule_id"], self.rule["text"],
                                self.manifest(("a", "ZK01", "zk01-p2"),
                                              ("b", "ZK02", "zk02-p1"))))
        thread2, outcome2 = self.run_in_thread(engine2, "rl-b")
        time.sleep(0.6)  # still inside the original 1.2s window
        self.assertEqual(self.state_a.request_count(), 1)  # only the first 429
        self.assertEqual(self.state_b.request_count(), 2)  # both runs crawled B

        for thread in (thread1, thread2):
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
        self.assertEqual(outcome1["run"]["status"], "completed")
        self.assertEqual(outcome2["run"]["status"], "completed")
        # After the shared moment passed, each run fetched exactly its missing page.
        self.assertEqual(self.state_a.request_count(contains="zk01-p1"), 2)
        self.assertEqual(self.state_a.request_count(contains="zk01-p2"), 1)
        self.assertEqual(self.state_a.request_count(), 3)

    def test_redirect_into_held_site_is_held(self):
        engine = RunEngine(self.store, self.snapshots)
        target = f"{self.url_a}/site/ZK01/zk01-p2"
        redirect_url = f"{self.url_b}/redirect?to={urllib.parse.quote(target, safe='')}"
        self.state_a.set_rate_limit(1.0)
        # A direct page hits the 429 first; the redirect page must then be
        # held at the redirect step without any request reaching site A.
        engine.start(RunConfig("rl-r", self.rule["rule_id"], self.rule["text"],
                               self.manifest(("a", "ZK01", "zk01-p1")) +
                               [{"page_id": "zk01-p2", "url": redirect_url}]))
        thread, outcome = self.run_in_thread(engine, "rl-r")

        self.assertTrue(wait_for(lambda: self.state_b.request_count() >= 1))
        time.sleep(0.5)
        # The 302 was fetched, but the redirect target on the held site was not.
        self.assertEqual(self.state_b.request_count(), 1)
        self.assertEqual(self.state_a.request_count(), 1)  # only the first 429

        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome["run"]["status"], "completed")
        # After the hold expired, both pages were fetched exactly once more.
        self.assertEqual(self.state_a.request_count(contains="zk01-p1"), 2)
        self.assertEqual(self.state_a.request_count(contains="zk01-p2"), 1)
        self.assertEqual(self.state_a.request_count(), 3)
        self.assertEqual(self.state_b.request_count(), 2)

    def test_ordinary_failure_still_fails_for_manual_resume(self):
        engine = RunEngine(self.store, self.snapshots)
        self.state_a.set_rate_limit(0.8)
        self.state_b.fail(["zk02-p1"])
        engine.start(RunConfig("rl-f", self.rule["rule_id"], self.rule["text"],
                               self.manifest(("a", "ZK01", "zk01-p1"),
                                             ("b", "ZK02", "zk02-p1"))))
        try:
            run = engine.run("rl-f")
        finally:
            self.state_b.reset()
        # A plain 500 fails the run as before; the 429 hold is not a failure.
        self.assertEqual(run["status"], "failed")
        states = {p["page_id"]: p["state"] for p in run["pages"]}
        self.assertEqual(states["zk01-p1"], "waiting")
        self.assertEqual(states["zk02-p1"], "failed")
        # Manual resume: waits out the rest of the hold, then completes.
        run = engine.run("rl-f")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(self.state_a.request_count(contains="zk01-p1"), 2)
        self.assertEqual(self.state_b.request_count(contains="zk02-p1"), 2)


class FetcherHoldsTest(TwoSiteTestCase):
    def test_fetcher_records_hold_and_short_circuits(self):
        registry = RateLimitStore(os.path.join(self.tmp, "rate_limits.json"))
        fetcher = Fetcher(registry)
        url = f"{self.url_a}/site/ZK01/zk01-p1"
        self.state_a.set_rate_limit(0.6)
        with self.assertRaises(RateLimitedError) as caught:
            fetcher("p1", url)
        self.assertGreater(caught.exception.retry_at, time.time())
        self.assertIsNotNone(registry.wait_until(url))
        # While held, no request leaves the process at all.
        with self.assertRaises(RateLimitedError):
            fetcher("p1", url)
        self.assertEqual(self.state_a.request_count(), 1)
        # Once the moment passes the page is fetched normally.
        self.assertTrue(wait_for(lambda: registry.wait_until(url) is None, timeout=3))
        page = fetcher("p1", url)
        self.assertEqual(page.page_id, "p1")
        self.assertEqual(self.state_a.request_count(), 2)


class RetryAfterParsingTest(unittest.TestCase):
    def test_delta_seconds(self):
        self.assertEqual(parse_retry_after("3", now=100.0), 103.0)

    def test_http_date(self):
        moment = email.utils.formatdate(170.0, usegmt=True)
        self.assertEqual(parse_retry_after(moment, now=100.0), 170.0)

    def test_past_date_clamps_to_now(self):
        moment = email.utils.formatdate(50.0, usegmt=True)
        self.assertEqual(parse_retry_after(moment, now=100.0), 100.0)

    def test_missing_or_garbage_uses_default(self):
        self.assertEqual(parse_retry_after(None, now=100.0), 100.0 + DEFAULT_RETRY_AFTER)
        self.assertEqual(parse_retry_after("not-a-date", now=100.0),
                         100.0 + DEFAULT_RETRY_AFTER)


class RateLimitStoreTest(unittest.TestCase):
    def test_persists_per_site_across_instances(self):
        path = os.path.join(tempfile.mkdtemp(), "rate_limits.json")
        store = RateLimitStore(path)
        url = "http://site-a.example:9000/site/page1"
        deadline = time.time() + 300
        store.record(url, deadline)
        # A fresh instance (service restart) sees the same absolute moment.
        reloaded = RateLimitStore(path)
        self.assertEqual(reloaded.wait_until(url), deadline)
        # The hold is per site, not per page.
        self.assertEqual(reloaded.wait_until("http://site-a.example:9000/other"), deadline)
        # Other sites and local files are never held.
        self.assertIsNone(reloaded.wait_until("http://site-b.example:9000/site/page1"))
        self.assertIsNone(reloaded.wait_until("/tmp/page.html"))

    def test_later_deadline_wins_and_expired_entries_prune(self):
        path = os.path.join(tempfile.mkdtemp(), "rate_limits.json")
        store = RateLimitStore(path)
        url = "http://site.example:9000/p"
        later = time.time() + 300
        store.record(url, later)
        store.record(url, later - 100)  # earlier hold must not shorten the wait
        self.assertEqual(store.wait_until(url), later)
        store.record("http://gone.example:9000/p", time.time() + 0.05)
        time.sleep(0.1)
        self.assertIsNone(store.wait_until("http://gone.example:9000/p"))


if __name__ == "__main__":
    unittest.main()
