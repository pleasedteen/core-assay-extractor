"""Workbench storage: rule versions, run checkpoints and baselines.

Everything is plain JSON/JSONL files under a data directory -- no database
service is required.  Writes are atomic (temp file + ``os.replace``) so an
interrupted run never leaves half-written state behind.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional


def _atomic_write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class WorkbenchStore:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.rules_dir = os.path.join(self.root, "rules")
        self.runs_dir = os.path.join(self.root, "runs")
        self.snapshots_dir = os.path.join(self.root, "snapshots")
        self.baselines_dir = os.path.join(self.root, "baselines")
        self.reports_dir = os.path.join(self.root, "reports")
        for directory in (self.rules_dir, self.runs_dir, self.snapshots_dir,
                          self.baselines_dir, self.reports_dir):
            os.makedirs(directory, exist_ok=True)

    # ------------------------------------------------------------- rules ----
    def save_rule_version(self, name: str, text: str, note: str = "") -> Dict[str, Any]:
        existing = self.list_rule_versions(name)
        version = max((item["version_number"] for item in existing), default=0) + 1
        rule_id = f"{name}-v{version}"
        payload = {
            "rule_id": rule_id,
            "name": name,
            "version_number": version,
            "note": note,
            "text": text,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        _atomic_write_json(os.path.join(self.rules_dir, f"{rule_id}.json"), payload)
        return payload

    def list_rule_versions(self, name: Optional[str] = None) -> List[Dict[str, Any]]:
        items = []
        for filename in sorted(os.listdir(self.rules_dir)):
            if filename.endswith(".json"):
                payload = _read_json(os.path.join(self.rules_dir, filename))
                if name is None or payload["name"] == name:
                    items.append(payload)
        items.sort(key=lambda item: (item["name"], item["version_number"]))
        return items

    def get_rule(self, rule_id: str) -> Dict[str, Any]:
        return _read_json(os.path.join(self.rules_dir, f"{rule_id}.json"))

    # -------------------------------------------------------------- runs ----
    def run_path(self, run_id: str) -> str:
        return os.path.join(self.runs_dir, run_id, "run.json")

    def save_run(self, run: Dict[str, Any]) -> None:
        _atomic_write_json(self.run_path(run["run_id"]), run)

    def load_run(self, run_id: str) -> Dict[str, Any]:
        return _read_json(self.run_path(run_id))

    def list_runs(self) -> List[Dict[str, Any]]:
        items = []
        if not os.path.isdir(self.runs_dir):
            return items
        for run_id in sorted(os.listdir(self.runs_dir)):
            path = os.path.join(self.runs_dir, run_id, "run.json")
            if os.path.exists(path):
                items.append(_read_json(path))
        items.sort(key=lambda item: item.get("started_at", ""))
        return items

    def run_output_path(self, run_id: str) -> str:
        directory = os.path.join(self.runs_dir, run_id)
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, "output.jsonl")

    # ---------------------------------------------------------- baselines ----
    def save_baseline(self, baseline: Dict[str, Any]) -> None:
        _atomic_write_json(os.path.join(self.baselines_dir, f"{baseline['baseline_id']}.json"), baseline)

    def list_baselines(self) -> List[Dict[str, Any]]:
        items = []
        for filename in sorted(os.listdir(self.baselines_dir)):
            if filename.endswith(".json"):
                items.append(_read_json(os.path.join(self.baselines_dir, filename)))
        return items

    def get_baseline(self, baseline_id: str) -> Dict[str, Any]:
        return _read_json(os.path.join(self.baselines_dir, f"{baseline_id}.json"))

    # ------------------------------------------------------------ reports ----
    def save_report(self, report_id: str, payload: Dict[str, Any]) -> str:
        path = os.path.join(self.reports_dir, f"{report_id}.json")
        _atomic_write_json(path, payload)
        return path

    def list_reports(self) -> List[str]:
        return sorted(f[:-5] for f in os.listdir(self.reports_dir) if f.endswith(".json"))
