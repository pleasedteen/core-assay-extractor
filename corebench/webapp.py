"""Workbench web application (Python standard library only).

Serves a single-page UI plus a JSON API for rule editing/versioning, run
control and resume, snapshot replay, baseline confirmation and semantic diff
reports.  No browser automation, framework or database service is required.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from .diff import confirm_baseline, regression_report
from .runner import RunConfig, RunEngine
from .rules import Rule
from .sources import SnapshotStore
from .storage import WorkbenchStore

STATIC_DIR = os.path.join(os.path.dirname(__file__), "web")


class WorkbenchApp:
    def __init__(self, data_dir: str):
        self.store = WorkbenchStore(data_dir)
        self.snapshots = SnapshotStore(os.path.join(data_dir, "snapshots"))
        # Expose snapshots on the store so diffing can verify completeness.
        self.store.snapshots = self.snapshots
        self.engine = RunEngine(self.store, self.snapshots)
        self._run_locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, run_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._run_locks.setdefault(run_id, threading.Lock())

    # ----------------------------------------------------------------- rules
    def save_rule(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        text = payload["text"]
        # Validate before saving: a broken rule never becomes a version.
        Rule.from_text(text)
        return self.store.save_rule_version(payload["name"], text, payload.get("note", ""))

    def start_run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        rule_version = self.store.get_rule(payload["rule_id"])
        config = RunConfig(
            run_id=payload["run_id"],
            rule_id=rule_version["rule_id"],
            rule_text=rule_version["text"],  # pinned: later rule saves cannot affect this run
            pages=payload["pages"],
            note=payload.get("note", ""),
        )
        return self.engine.start(config)

    def resume_run(self, run_id: str) -> Dict[str, Any]:
        with self._lock_for(run_id):
            return self.engine.run(run_id)

    def replay_run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        candidate_text = None
        if payload.get("candidate_rule_id"):
            candidate_text = self.store.get_rule(payload["candidate_rule_id"])["text"]
        return self.engine.replay(payload["run_id"], rule_text=candidate_text,
                                  new_run_id=payload.get("new_run_id"))

    def confirm(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        run = self.store.load_run(payload["run_id"])
        snapshot = self.snapshots.load(run["snapshot_id"])
        return confirm_baseline(self.store, run, snapshot, payload.get("note", ""))

    def diff(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        baseline = self.store.get_baseline(payload["baseline_id"])
        candidate = self.store.load_run(payload["candidate_run_id"])
        return regression_report(self.store, baseline, candidate)

    def snapshot_page(self, snapshot_id: str, page_id: str) -> Optional[Dict[str, Any]]:
        snapshot = self.snapshots.load(snapshot_id)
        page = snapshot.get(page_id)
        if page is None:
            return None
        return {"page_id": page.page_id, "url": page.url, "html": page.html,
                "content_sha1": page.content_sha1, "failures": snapshot.failures}


class ApiHandler(BaseHTTPRequestHandler):
    app: WorkbenchApp = None  # type: ignore[assignment]

    def log_message(self, *args):
        pass

    # ------------------------------------------------------------- routing --
    def do_GET(self):  # noqa: N802
        try:
            self._handle_get()
        except FileNotFoundError as exc:
            self._error(404, str(exc))
        except (ValueError, KeyError) as exc:
            self._error(400, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - keep the API alive
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _handle_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/" or path == "/index.html":
            self._serve_file("index.html", "text/html; charset=utf-8")
            return
        if path.startswith("/static/"):
            self._serve_file(path[len("/static/"):], None)
            return
        if path == "/api/rules":
            self._json({"rules": self.app.store.list_rule_versions()})
            return
        if path == "/api/runs":
            self._json({"runs": self.app.store.list_runs()})
            return
        if path == "/api/baselines":
            self._json({"baselines": self.app.store.list_baselines()})
            return
        if path == "/api/reports":
            self._json({"reports": self.app.store.list_reports()})
            return
        if path == "/api/report":
            report_id = query.get("id", [""])[0]
            with open(os.path.join(self.app.store.reports_dir, f"{report_id}.json"), encoding="utf-8") as fh:
                self._json(json.load(fh))
            return
        if path == "/api/run":
            run = self.app.store.load_run(query["id"][0])
            self._json(run)
            return
        if path == "/api/output":
            run = self.app.store.load_run(query["run_id"][0])
            if not run.get("output_path") or not os.path.exists(run["output_path"]):
                self._json({"lines": []})
                return
            lines = []
            with open(run["output_path"], encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        lines.append(json.loads(line))
            self._json({"lines": lines})
            return
        if path == "/api/snapshot-page":
            data = self.app.snapshot_page(query["snapshot_id"][0], query["page_id"][0])
            if data is None:
                self._error(404, "page not in snapshot")
            else:
                self._json(data)
            return
        self._error(404, "unknown endpoint")

    def do_POST(self):  # noqa: N802
        payload = self._read_payload()
        try:
            if self.path == "/api/rules/save":
                self._json(self.app.save_rule(payload))
            elif self.path == "/api/runs/start":
                self._json(self.app.start_run(payload))
            elif self.path == "/api/runs/resume":
                self._json(self.app.resume_run(payload["run_id"]))
            elif self.path == "/api/runs/replay":
                self._json(self.app.replay_run(payload))
            elif self.path == "/api/baselines/confirm":
                self._json(self.app.confirm(payload))
            elif self.path == "/api/diff":
                self._json(self.app.diff(payload))
            else:
                self._error(404, "unknown endpoint")
        except FileNotFoundError as exc:
            self._error(404, str(exc))
        except (ValueError, KeyError) as exc:
            self._error(400, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - keep the API alive
            self._error(500, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------- helpers --
    def _read_payload(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status=status)

    def _serve_file(self, name: str, content_type: Optional[str]) -> None:
        # Prevent path traversal in static names.
        safe = os.path.normpath(os.path.join(STATIC_DIR, name))
        if not safe.startswith(STATIC_DIR + os.sep) or not os.path.exists(safe):
            self._error(404, "file not found")
            return
        if content_type is None:
            content_type = "text/css; charset=utf-8" if safe.endswith(".css") else "application/javascript"
        with open(safe, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(data_dir: str, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    app = WorkbenchApp(data_dir)
    handler = type("BoundApiHandler", (ApiHandler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.app = app  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="corebench drill-archive extraction workbench")
    parser.add_argument("--data-dir", default=os.path.join(os.getcwd(), "workbench-data"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8077)
    args = parser.parse_args()
    server = create_server(args.data_dir, args.host, args.port)
    print(f"corebench workbench listening on http://{args.host}:{args.port}")
    print(f"data directory: {os.path.abspath(args.data_dir)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
