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

Gap attribution
---------------
An ``extraction_failed`` / ``unexplained_field`` entry is a *gap*: a spot on an
original page (table / column / field) that the candidate rule could no longer
extract.  Gaps are attributed to the actual affected page and field, and each
gap is counted **once**:

* the gap's identity carries the borehole, page, table and column/field;
* the old records or fields it explains are attached to it under ``affected``
  instead of also being reported as ``removed``;
* the same column gap on two different pages is two gaps, while repeated rows
  of one column on one page collapse into a single gap;
* gaps already present in the baseline are not regressions;
* unrelated records elsewhere keep their normal ``added`` / ``removed`` /
  ``value_changed`` classification, and legitimate missing cells (``kind ==
  missing``) are never mistaken for a gap.

Critical tables
---------------
A table a rule marks ``required`` (the default) that fails to extract on a page
-- selector matches nothing, the table cannot be parsed, or a required column is
absent -- is a *critical* gap: baseline confirmation and regression passes are
blocked.  Rules can opt a table out with ``required: false``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .merge import read_jsonl
from .values import values_equivalent

CHANGE_ADDED = "added"
CHANGE_REMOVED = "removed"
CHANGE_VALUE = "value_changed"
CHANGE_MEANING = "type_or_meaning_changed"
CHANGE_FAILED = "extraction_failed"
CHANGE_UNEXPLAINED = "unexplained_field"

BLOCKING_CATEGORIES = {CHANGE_VALUE, CHANGE_MEANING, CHANGE_FAILED, CHANGE_UNEXPLAINED}

# Warnings that describe a *gap*: an existing page/table/column the rule could
# not extract.  One gap is reported once and absorbs the records and fields it
# explains, even when those records live on several rows of the same page.
GAP_CODES = {"EXTRACTION_FAILED", "TABLE_NOT_FOUND", "UNEXPLAINED_COLUMN",
             "MISSING_REQUIRED_FIELD"}


@dataclass
class Gap:
    code: str
    page_id: str
    table: Optional[str]
    raw: Optional[str]
    field: Optional[str]
    borehole_id: str
    warning: Dict[str, Any]
    critical: bool
    affected: List[Dict[str, Any]] = field(default_factory=list)
    # Empty-identity records the gap already accounts for (a lost identity
    # column leaves one such remnant per row); counted with ``affected``.
    remnant_count: int = 0

    @property
    def category(self) -> str:
        return CHANGE_UNEXPLAINED if self.code == "UNEXPLAINED_COLUMN" else CHANGE_FAILED

    @property
    def source(self) -> Dict[str, Any]:
        return {
            "page_id": self.page_id,
            "url": self.warning.get("url"),
            "content_sha1": self.warning.get("content_sha1"),
            "table": self.table,
            "header_path": [self.raw] if self.raw else None,
            "raw_cell": self.field or self.raw,
        }


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
    # Records/fields absorbed by a gap: enumerated for traceability but never
    # counted again in the summary (one gap == one count).
    affected: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "record_type": self.record_type,
            "identity": self.identity,
            "category": self.category,
            "field_changes": [change.to_dict() for change in self.field_changes],
            "old_source": _source_ref(self.old_provenance),
            "new_source": _source_ref(self.new_provenance),
        }
        if self.affected:
            out["affected"] = self.affected
        return out


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


def _gaps(rows: List[Dict[str, Any]]) -> List[Gap]:
    """Collects the data-loss gaps reported by one run, one Gap per warning."""
    gaps: List[Gap] = []
    for warning in _all_warnings(rows):
        if warning.get("code") not in GAP_CODES:
            continue
        gaps.append(Gap(
            code=warning["code"],
            page_id=warning.get("page_id") or "",
            table=warning.get("table"),
            raw=warning.get("raw"),
            field=warning.get("field"),
            borehole_id=warning["borehole_id"],
            warning=warning,
            critical=bool(warning.get("critical")),
        ))
    return gaps


def _gap_key(gap: Gap) -> Tuple[str, str, Optional[str], Optional[str], Optional[str]]:
    """Identity of one gap: *where* on the original page the loss happened.

    Warnings for the same page/table/column collapse into one gap; the same
    column on two different pages are two distinct gaps.
    """
    return (gap.code, gap.page_id, gap.table, gap.field, gap.raw)


def _dedupe_gaps(gaps: List[Gap]) -> List[Gap]:
    seen: Dict[Tuple[str, str, Optional[str], Optional[str], Optional[str]], Gap] = {}
    ordered: List[Gap] = []
    for gap in gaps:
        key = _gap_key(gap)
        if key not in seen:
            seen[key] = gap
            ordered.append(gap)
    # A missing identity column loses *every* record of that table on that
    # page, so it subsumes the sibling unexplained-column warnings for the same
    # page/table: that is one gap, not a missing field plus a stray column.
    lost_tables = {(gap.page_id, gap.table) for gap in ordered
                   if gap.code == "MISSING_REQUIRED_FIELD"}
    if lost_tables:
        ordered = [gap for gap in ordered
                   if not (gap.code == "UNEXPLAINED_COLUMN"
                           and (gap.page_id, gap.table) in lost_tables)]
    return ordered


def _gap_applies_to_record(gap: Gap, record: Dict[str, Any]) -> bool:
    """A table/page level gap explains every record that table produced there."""
    prov = record.get("provenance") or {}
    if gap.page_id and prov.get("page_id") and gap.page_id != prov.get("page_id"):
        return False
    if gap.table and prov.get("table") and gap.table != prov.get("table"):
        return False
    if gap.page_id or gap.table:
        return True
    return False


def _gap_applies_to_column(gap: Gap, header_path: Optional[Sequence[str]],
                           record: Optional[Dict[str, Any]] = None) -> bool:
    if not gap.field and not gap.raw:
        return False
    wanted = gap.field or gap.raw
    candidates = {gap.raw, gap.field, " / ".join(header_path or [])}
    if wanted in candidates:
        return True
    # Long-format tables carry the analyte token in the data row rather than a
    # dedicated column header; the original row text is the evidence that ties
    # the gap to that measurement.
    if record is not None and gap.raw:
        raw_row = (record.get("provenance") or {}).get("raw_row") or []
        if any(cell.strip() == gap.raw.strip() for cell in raw_row if isinstance(cell, str)):
            return True
    return False


def _gap_for_record(gaps: List[Gap], record: Dict[str, Any]) -> Optional[Gap]:
    prov = record.get("provenance") or {}
    for gap in gaps:
        if not _gap_applies_to_record(gap, record):
            continue
        # A column-level gap only explains a measurement that lived in exactly
        # that column; a table/page level gap explains any record it produced.
        if gap.field or (gap.code == "UNEXPLAINED_COLUMN" and gap.raw):
            if _gap_applies_to_column(gap, prov.get("header_path"), record):
                return gap
            continue
        return gap
    return None


def _gap_for_field(gaps: List[Gap], record: Dict[str, Any], field_name: str,
                   header_path: Optional[Sequence[str]]) -> Optional[Gap]:
    for gap in gaps:
        if not _gap_applies_to_record(gap, record):
            continue
        if _gap_applies_to_column(gap, header_path) or gap.field == field_name:
            return gap
    return None


# ------------------------------------------------------------------- compare
def compare_runs(old_rows: List[Dict[str, Any]], new_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    old_index = _index_documents(old_rows)
    new_index = _index_documents(new_rows)
    changes: List[RecordChange] = []

    # Gaps already present in the old run are not regressions.  Every remaining
    # gap is reported exactly once and absorbs the old records/fields it
    # explains, so a vanished column never counts as both a deletion and an
    # unexplained field.
    old_gap_keys = {_gap_key(gap) for gap in _gaps(old_rows)}
    gaps = [gap for gap in _dedupe_gaps(_gaps(new_rows)) if _gap_key(gap) not in old_gap_keys]

    for key in sorted(set(old_index) | set(new_index)):
        record_type = _record_type(key)
        if key not in old_index:
            # A record with an empty identity is a mere remnant of a lost
            # identity column: the gap for that column already reports it, so
            # it must not add a spurious ``added`` record.
            if "::" in key[1] and not key[1].split("::", 1)[1]:
                new_record = new_index[key]
                gap = _gap_for_record(gaps, new_record)
                if gap is not None:
                    gap.remnant_count = getattr(gap, "remnant_count", 0) + 1
                    continue
            changes.append(RecordChange(record_type, key[1], CHANGE_ADDED,
                                        new_provenance=new_index[key].get("provenance")))
            continue
        old_record = old_index[key]
        if key not in new_index:
            gap = _gap_for_record(gaps, old_record)
            if gap is not None:
                gap.affected.append({
                    "record_type": record_type,
                    "identity": key[1],
                    "old_source": _source_ref(old_record.get("provenance")),
                })
                continue
            changes.append(RecordChange(record_type, key[1], CHANGE_REMOVED,
                                        old_provenance=old_record.get("provenance")))
            continue
        field_changes = _compare_records(key, old_record, new_index[key], gaps)
        if field_changes:
            changes.append(RecordChange(
                record_type, key[1], "changed", field_changes,
                old_record.get("provenance"), new_index[key].get("provenance"),
            ))

    changes.extend(_gap_changes(gaps))
    blocking = [change for change in changes if change.category in BLOCKING_CATEGORIES
                or any(fc.category in BLOCKING_CATEGORIES for fc in change.field_changes)]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": _summarize(changes),
        "changes": [change.to_dict() for change in changes],
        "critical_gaps": [_gap_identity(gap) for gap in gaps if gap.critical],
        "regression_passed": not blocking,
    }


def _record_type(key: Tuple[str, str]) -> str:
    return key[1].split("::", 1)[0]


def _compare_records(key, old_record: Dict[str, Any], new_record: Dict[str, Any],
                     gaps: List[Gap]) -> List[FieldChange]:
    changes: List[FieldChange] = []
    if "fields" in old_record or "fields" in new_record:
        old_fields = old_record.get("fields", {})
        new_fields = new_record.get("fields", {})
        field_prov = old_record.get("provenance", {}).get("field_provenance", {})
        for field_name in sorted(set(old_fields) | set(new_fields)):
            old_field, new_field = old_fields.get(field_name), new_fields.get(field_name)
            if old_field is not None and new_field is None:
                source = field_prov.get(field_name) or {}
                gap = _gap_for_field(gaps, old_record, field_name, source.get("header_path"))
                if gap is not None:
                    gap.affected.append({
                        "record_type": _record_type(key),
                        "identity": key[1],
                        "field": field_name,
                        "old": old_field,
                        "old_source": _source_ref({**old_record.get("provenance", {}), **source}),
                    })
                    continue
            change = _compare_field(field_name, old_field, new_field,
                                    old_record.get("provenance"), new_record.get("provenance"))
            if change:
                changes.append(change)
    if key[1].startswith("measurement::"):
        old_value, new_value = old_record.get("value"), new_record.get("value")
        if old_value is not None and new_value is None:
            prov = old_record.get("provenance") or {}
            gap = _gap_for_column(gaps, old_record, prov.get("header_path"))
            if gap is not None:
                gap.affected.append({
                    "record_type": "measurement", "identity": key[1], "field": "value",
                    "old": old_value, "old_source": _source_ref(prov),
                })
            else:
                changes.append(FieldChange(CHANGE_REMOVED, "value", old_value,
                                           old_source=prov))
        else:
            change = _compare_field("value", old_value, new_value,
                                    old_record.get("provenance"), new_record.get("provenance"))
            if change:
                changes.append(change)
        if old_record.get("method") != new_record.get("method"):
            changes.append(FieldChange(CHANGE_MEANING, "method",
                                       old_record.get("method"), new_record.get("method"),
                                       detail="measurement method identity changed"))
    return changes


def _gap_for_column(gaps: List[Gap], record: Dict[str, Any],
                    header_path: Optional[Sequence[str]]) -> Optional[Gap]:
    for gap in gaps:
        if _gap_applies_to_record(gap, record) and _gap_applies_to_column(gap, header_path):
            return gap
    return None


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


def _gap_identity(gap: Gap) -> str:
    location = "::".join(part for part in (gap.borehole_id, gap.page_id, gap.table) if part)
    return f"{location}::{gap.field or gap.raw or gap.code}"


def _gap_changes(gaps: List[Gap]) -> List[RecordChange]:
    changes: List[RecordChange] = []
    for gap in sorted(gaps, key=_gap_identity):
        source = gap.source
        detail = gap.warning.get("message", "")
        if gap.critical:
            detail = f"critical table gap: {detail}"
        affected = list(gap.affected)
        if gap.remnant_count:
            affected.append({"record_type": "remnant",
                             "identity": f"{gap.remnant_count} empty-identity record(s)",
                             "note": "left by the lost identity column; already covered by this gap"})
        change = RecordChange(
            record_type="gap",
            identity=_gap_identity(gap),
            category=gap.category,
            field_changes=[FieldChange(gap.category, gap.field or "warning",
                                       new=gap.warning, new_source=source, detail=detail)],
            new_provenance=source,
            affected=affected,
        )
        changes.append(change)
    return changes


def critical_gaps(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Critical data-loss gaps in a run's output (block baselines/regressions)."""
    return [
        {
            "identity": _gap_identity(gap),
            "code": gap.code,
            "page_id": gap.page_id,
            "table": gap.table,
            "field": gap.field or gap.raw,
            "message": gap.warning.get("message", ""),
        }
        for gap in _dedupe_gaps(_gaps(rows)) if gap.critical
    ]


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
    """Promote a successful, complete, conflict-free run with no critical gaps."""
    if run["status"] != "completed":
        raise ValueError("only completed runs can become baselines")
    if run.get("has_conflicts"):
        raise ValueError("run contains unresolved measurement conflicts")
    if not snapshot.is_complete:
        raise ValueError("snapshot is incomplete (failed pages); cannot confirm baseline")
    gaps = critical_gaps(read_jsonl(run["output_path"]))
    if gaps:
        locations = ", ".join(gap["identity"] for gap in gaps)
        raise ValueError(f"critical table extraction failed; cannot confirm baseline: {locations}")
    baseline_id = f"baseline-{run['run_id']}"
    baseline = {
        "baseline_id": baseline_id,
        "run_id": run["run_id"],
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
    """Compare a candidate run against a baseline, both on the same snapshot."""
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
    # A critical table that failed to extract in the candidate hides any data
    # change behind it, so it always blocks a passing regression.
    candidate_critical_gaps = critical_gaps(new_rows)
    report["candidate_critical_gaps"] = candidate_critical_gaps
    report["regression_passed"] = (
        report["regression_passed"]
        and candidate_run["status"] == "completed"
        and not candidate_run.get("has_conflicts")
        and snapshot_complete
        and not candidate_critical_gaps
    )
    report_id = f"diff-{baseline['baseline_id']}-{candidate_run['run_id']}"
    report["report_id"] = report_id
    store.save_report(report_id, report)
    return report
