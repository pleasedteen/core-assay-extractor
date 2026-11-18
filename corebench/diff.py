"""Baseline confirmation and semantic record/field level diffing.

Two rule versions are compared on the **same** complete snapshot.  The diff
understands the business records (interval / sample / measurement) rather than
raw text, so these are *not* reported as data changes:

* field renames (the output keeps a stable canonical field name);
* list reordering (records are matched by identity, then compared);
* equivalent unit expressions (values compare after unit normalisation).

Change categories: ``added``, ``removed``, ``value_changed``,
``type_or_meaning_changed`` (e.g. number -> censored, unit dimension change),
``extraction_failed`` and ``unexplained_field`` (an old field that the new rule
can no longer interpret -- never downgraded to a plain removal).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .merge import read_jsonl
from .review import STATUS_NEEDS_RECHECK, STATUS_REUSABLE, evaluate_review_on_rows
from .values import values_equivalent

CHANGE_ADDED = "added"
CHANGE_REMOVED = "removed"
CHANGE_VALUE = "value_changed"
CHANGE_MEANING = "type_or_meaning_changed"
CHANGE_FAILED = "extraction_failed"
CHANGE_UNEXPLAINED = "unexplained_field"
CHANGE_RECHECK = "review_recheck_required"

BLOCKING_CATEGORIES = {CHANGE_VALUE, CHANGE_MEANING, CHANGE_FAILED,
                       CHANGE_UNEXPLAINED, CHANGE_RECHECK}


@dataclass
class FieldChange:
    category: str
    field: str
    old: Any = None
    new: Any = None
    old_source: Optional[Dict[str, Any]] = None
    new_source: Optional[Dict[str, Any]] = None
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out = {"category": self.category, "field": self.field}
        if self.old is not None:
            out["old"] = self.old
        if self.new is not None:
            out["new"] = self.new
        if self.old_source:
            out["old_source"] = _source_ref(self.old_source)
        if self.new_source:
            out["new_source"] = _source_ref(self.new_source)
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class RecordChange:
    record_type: str
    identity: str
    category: str
    field_changes: List[FieldChange] = field(default_factory=list)
    old_provenance: Optional[Dict[str, Any]] = None
    new_provenance: Optional[Dict[str, Any]] = None
    borehole_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "borehole_id": self.borehole_id,
            "record_type": self.record_type,
            "identity": self.identity,
            "category": self.category,
            "field_changes": [change.to_dict() for change in self.field_changes],
            "old_source": _source_ref(self.old_provenance),
            "new_source": _source_ref(self.new_provenance),
        }


def _source_ref(provenance: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not provenance:
        return None
    return {
        "page_id": provenance.get("page_id"),
        "url": provenance.get("url"),
        "content_sha1": provenance.get("content_sha1"),
        "table": provenance.get("table"),
        "selector": provenance.get("selector"),
        "raw_row": provenance.get("raw_row"),
        "raw_cell": provenance.get("raw_cell"),
        "header_path": provenance.get("header_path"),
    }


# ---------------------------------------------------------------- flattening
def _index_documents(rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        borehole = row["borehole_id"]
        for interval in row.get("intervals", []):
            index[(borehole, f"interval::{interval['identity']}")] = interval
        for sample in row.get("samples", []):
            index[(borehole, f"sample::{sample['identity']}")] = sample
        for measurement in row.get("measurements", []):
            index[(borehole, f"measurement::{measurement['identity']}")] = measurement
    return index

def _all_warnings(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        for warning in row.get("warnings", []):
            out.append({**warning, "borehole_id": row["borehole_id"]})
    return out


# ------------------------------------------------------------------- compare
def compare_runs(old_rows: List[Dict[str, Any]], new_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    old_index = _index_documents(old_rows)
    new_index = _index_documents(new_rows)
    changes: List[RecordChange] = []

    for key in sorted(set(old_index) | set(new_index)):
        borehole_id, typed_identity = key
        if key not in old_index:
            changes.append(RecordChange(_record_type(key), typed_identity, CHANGE_ADDED,
                                        new_provenance=new_index[key].get("provenance"),
                                        borehole_id=borehole_id))
            continue
        if key not in new_index:
            old_record = old_index[key]
            category = CHANGE_REMOVED
            if _record_disappeared_due_to_failure(key, old_record, new_rows):
                category = CHANGE_FAILED
            changes.append(RecordChange(_record_type(key), typed_identity, category,
                                        old_provenance=old_record.get("provenance"),
                                        borehole_id=borehole_id))
            continue
        field_changes = _compare_records(key, old_index[key], new_index[key])
        if field_changes:
            changes.append(RecordChange(
                _record_type(key), typed_identity, "changed", field_changes,
                old_index[key].get("provenance"), new_index[key].get("provenance"),
                borehole_id=borehole_id,
            ))

    changes.extend(_diff_warnings(old_rows, new_rows))
    blocking = [change for change in changes if change.category in BLOCKING_CATEGORIES
                or any(fc.category in BLOCKING_CATEGORIES for fc in change.field_changes)]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": _summarize(changes),
        "changes": [change.to_dict() for change in changes],
        "regression_passed": not blocking,
    }


def _record_type(key: Tuple[str, str]) -> str:
    return key[1].split("::", 1)[0]


def _record_disappeared_due_to_failure(key, old_record, new_rows) -> bool:
    for row in new_rows:
        for warning in row.get("warnings", []):
            if warning.get("code") in ("EXTRACTION_FAILED", "TABLE_NOT_FOUND"):
                if warning.get("table") == old_record.get("provenance", {}).get("table"):
                    return True
    return False


def _compare_records(key, old_record: Dict[str, Any], new_record: Dict[str, Any]) -> List[FieldChange]:
    changes: List[FieldChange] = []
    if "fields" in old_record or "fields" in new_record:
        old_fields = old_record.get("fields", {})
        new_fields = new_record.get("fields", {})
        for field_name in sorted(set(old_fields) | set(new_fields)):
            change = _compare_field(field_name, old_fields.get(field_name), new_fields.get(field_name),
                                    old_record.get("provenance"), new_record.get("provenance"))
            if change:
                changes.append(change)
    if key[1].startswith("measurement::"):
        change = _compare_field("value", old_record.get("value"), new_record.get("value"),
                                old_record.get("provenance"), new_record.get("provenance"))
        if change:
            changes.append(change)
        if old_record.get("method") != new_record.get("method"):
            changes.append(FieldChange(CHANGE_MEANING, "method",
                                       old_record.get("method"), new_record.get("method"),
                                       detail="measurement method identity changed"))
    return changes


def _compare_field(field_name, old, new, old_prov, new_prov) -> Optional[FieldChange]:
    if old is None and new is None:
        return None
    if old is None:
        return FieldChange(CHANGE_ADDED, field_name, new=new, new_source=new_prov)
    if new is None:
        return FieldChange(CHANGE_REMOVED, field_name, old=old, old_source=old_prov)
    old_kind, new_kind = old.get("kind"), new.get("kind")
    if old_kind != new_kind:
        return FieldChange(CHANGE_MEANING, field_name, old, new, old_prov, new_prov,
                           detail=f"kind changed {old_kind} -> {new_kind}")
    if old.get("unit") != new.get("unit"):
        return FieldChange(CHANGE_MEANING, field_name, old, new, old_prov, new_prov,
                           detail=f"unit changed {old.get('unit')} -> {new.get('unit')}")
    if old_kind == "text":
        if old.get("value") != new.get("value"):
            return FieldChange(CHANGE_VALUE, field_name, old, new, old_prov, new_prov)
        return None
    if not values_equivalent(old, new):
        return FieldChange(CHANGE_VALUE, field_name, old, new, old_prov, new_prov)
    return None


def _diff_warnings(old_rows: List[Dict[str, Any]], new_rows: List[Dict[str, Any]]) -> List[RecordChange]:
    changes: List[RecordChange] = []
    new_warnings = _all_warnings(new_rows)
    old_warnings = _all_warnings(old_rows)
    old_seen = {(w["code"], w.get("table"), w.get("page_id"), w.get("raw")) for w in old_warnings}
    for warning in new_warnings:
        if warning["code"] not in ("EXTRACTION_FAILED", "UNEXPLAINED_COLUMN", "TABLE_NOT_FOUND"):
            continue
        signature = (warning["code"], warning.get("table"), warning.get("page_id"), warning.get("raw"))
        if signature in old_seen:
            continue
        category = CHANGE_UNEXPLAINED if warning["code"] == "UNEXPLAINED_COLUMN" else CHANGE_FAILED
        changes.append(RecordChange(
            record_type="warning",
            identity=f"{warning['borehole_id']}::{warning.get('table')}::{warning.get('raw') or warning['code']}",
            category=category,
            field_changes=[FieldChange(category, "warning", new=warning,
                                       detail=warning.get("message", ""))],
            borehole_id=warning["borehole_id"],
        ))
    return changes


def _summarize(changes: List[RecordChange]) -> Dict[str, int]:
    summary: Dict[str, int] = {}
    for change in changes:
        if change.field_changes:
            for field_change in change.field_changes:
                summary[field_change.category] = summary.get(field_change.category, 0) + 1
        else:
            summary[change.category] = summary.get(change.category, 0) + 1
    summary["total"] = len(changes)
    return summary


# ------------------------------------------------------------------ baseline
def confirm_baseline(store, run: Dict[str, Any], snapshot, note: str = "") -> Dict[str, Any]:
    """Promote a successful, complete, conflict-free run to a baseline.

    A reviewed run with no remaining unresolved conflicts is confirmable; the
    baseline remembers the adjudication so replay regressions can guard it.
    Failed runs, incomplete snapshots and runs still carrying unresolved
    conflicts are always rejected.
    """
    if run["status"] != "completed":
        raise ValueError("only completed runs can become baselines")
    if run.get("has_conflicts"):
        raise ValueError("run contains unresolved measurement conflicts")
    if not snapshot.is_complete:
        raise ValueError("snapshot is incomplete (failed pages); cannot confirm baseline")
    baseline_id = f"baseline-{run['run_id']}"
    baseline = {
        "baseline_id": baseline_id,
        "run_id": run["run_id"],
        "kind": run.get("kind", "collection"),
        "review_id": run.get("review_id"),
        "source_run_id": run.get("source_run_id"),
        "rule_id": run["rule_id"],
        "rule_text": run["rule_text"],
        "snapshot_id": run["snapshot_id"],
        "output_path": run["output_path"],
        "note": note,
        "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    store.save_baseline(baseline)
    return baseline


def regression_report(store, baseline: Dict[str, Any], candidate_run: Dict[str, Any]) -> Dict[str, Any]:
    """Compare a candidate run against a baseline, both on the same snapshot.

    When the baseline was produced by a manual review, the candidate replay is
    checked against the frozen adjudication: a past decision carries over only
    if the replay's candidate values *and* sources match exactly.  A vanished
    conflict -- even one where only the originally selected value remains --
    forces a re-review and fails the regression.
    """
    if baseline["snapshot_id"] != candidate_run["snapshot_id"]:
        raise ValueError("regression comparison requires the same complete snapshot")
    snapshot = store.snapshots.load(baseline["snapshot_id"]) if hasattr(store, "snapshots") else None
    old_rows = read_jsonl(baseline["output_path"])
    new_rows = read_jsonl(candidate_run["output_path"])
    report = compare_runs(old_rows, new_rows)
    report.update({
        "baseline_id": baseline["baseline_id"],
        "candidate_run_id": candidate_run["run_id"],
        "snapshot_id": baseline["snapshot_id"],
        "candidate_status": candidate_run["status"],
        "candidate_has_conflicts": candidate_run.get("has_conflicts", False),
    })
    snapshot_complete = snapshot.is_complete if snapshot is not None else True
    # An incomplete snapshot can never produce a passing regression: missing
    # pages would hide deletions and failures instead of proving equivalence.
    report["snapshot_complete"] = snapshot_complete

    # --------------------------------------------------------- review guard --
    review_id = baseline.get("review_id")
    review_evaluation = None
    if review_id:
        review = store.load_review(review_id)
        review_evaluation = evaluate_review_on_rows(review, new_rows)
        report["review_reuse"] = review_evaluation
        reusable_keys = {
            (item["borehole_id"], item["identity"], item["field"])
            for item in review_evaluation["items"]
            if item["status"] == STATUS_REUSABLE
        }
        # A reusable adjudication explains the value change between the
        # adjudicated baseline output and the still-conflicting replay output:
        # mark it carried over instead of counting it as a regression.
        prefixes = ("measurement::", "interval::", "sample::")
        for change in report["changes"]:
            raw_identity = change.get("identity", "")
            for prefix in prefixes:
                if raw_identity.startswith(prefix):
                    raw_identity = raw_identity[len(prefix):]
                    break
            covered = any(
                (change.get("borehole_id", ""), raw_identity, fc["field"]) in reusable_keys
                and fc["category"] in (CHANGE_VALUE, CHANGE_MEANING)
                for fc in change.get("field_changes", []))
            if covered:
                for field_change in change["field_changes"]:
                    field_change["detail"] = (
                        (field_change.get("detail", "") + "；"
                         if field_change.get("detail") else "")
                        + "人工裁决可沿用（候选值与来源一致）")
                change["adjudication_carried"] = True
        # Every decision that cannot be reused becomes an explicit blocking item.
        for item in review_evaluation["items"]:
            if item["status"] != STATUS_NEEDS_RECHECK:
                continue
            report["changes"].append({
                "record_type": "review",
                "identity": f"{item['borehole_id']}::{item['identity']}::{item['field']}",
                "category": CHANGE_RECHECK,
                "field_changes": [{
                    "category": CHANGE_RECHECK,
                    "field": item["field"],
                    "detail": item["detail"],
                    "old": None,
                    "new": None,
                }],
                "old_source": None,
                "new_source": None,
            })
        for item in review_evaluation.get("unreviewed_conflicts", []):
            report["changes"].append({
                "record_type": "review",
                "identity": f"{item['borehole_id']}::{item['identity']}::{item['field']}",
                "category": CHANGE_RECHECK,
                "field_changes": [{
                    "category": CHANGE_RECHECK,
                    "field": item["field"],
                    "detail": "回放出现原复核未覆盖的新冲突，需人工复核",
                    "old": None,
                    "new": None,
                }],
                "old_source": None,
                "new_source": None,
            })
        report["summary"] = _summarize_dict(report["changes"])

    blocking = []
    for change in report["changes"]:
        if change.get("adjudication_carried"):
            continue
        if change["category"] in BLOCKING_CATEGORIES or any(
                fc["category"] in BLOCKING_CATEGORIES for fc in change["field_changes"]):
            blocking.append(change)
    all_decisions_reusable = (
        review_evaluation is not None
        and review_evaluation["needs_recheck_count"] == 0
        and review_evaluation["reusable_count"] > 0
        and not review_evaluation.get("unreviewed_conflicts"))
    report["regression_passed"] = (
        not blocking
        and candidate_run["status"] == "completed"
        and snapshot_complete
        and (not candidate_run.get("has_conflicts") or all_decisions_reusable)
    )
    report_id = f"diff-{baseline['baseline_id']}-{candidate_run['run_id']}"
    report["report_id"] = report_id
    store.save_report(report_id, report)
    return report


def _summarize_dict(changes: List[Dict[str, Any]]) -> Dict[str, int]:
    summary: Dict[str, int] = {}
    for change in changes:
        if change.get("field_changes"):
            for field_change in change["field_changes"]:
                category = field_change["category"] if isinstance(field_change, dict) else field_change.category
                summary[category] = summary.get(category, 0) + 1
        else:
            summary[change["category"]] = summary.get(change["category"], 0) + 1
    summary["total"] = len(changes)
    return summary
