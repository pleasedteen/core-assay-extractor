"""Baseline confirmation and semantic record/field level diffing.

Two rule versions are compared on the **same** complete snapshot.  The diff
understands the business records (interval / sample / measurement) rather than
raw text, so these are *not* reported as data changes:

* field renames declared via ``renamed_from`` in the rule (the new JSONL is
  written with the new field name; the rule's output schema, embedded in every
  JSONL document, lets the diff match the same business field across runs,
  including renamed identity fields such as a sample code);
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
from .values import values_equivalent

CHANGE_ADDED = "added"
CHANGE_REMOVED = "removed"
CHANGE_VALUE = "value_changed"
CHANGE_MEANING = "type_or_meaning_changed"
CHANGE_FAILED = "extraction_failed"
CHANGE_UNEXPLAINED = "unexplained_field"

BLOCKING_CATEGORIES = {CHANGE_VALUE, CHANGE_MEANING, CHANGE_FAILED, CHANGE_UNEXPLAINED}


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

    def to_dict(self) -> Dict[str, Any]:
        return {
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
# Record kinds map to the rule table whose declared identity produced them.
_RECORD_TABLES = {
    "interval": "interval_table",
    "sample": "sample_table",
    "measurement": "assay_table",
}


def _schema_of(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the most complete embedded output schema found in a run."""
    schema: Dict[str, Any] = {"tables": {}}
    for row in rows:
        embedded = row.get("schema")
        if not embedded:
            continue
        for table_key, table_schema in embedded.get("tables", {}).items():
            schema["tables"].setdefault(table_key, table_schema)
    return schema


def _table_rename_map(schema: Dict[str, Any], table_key: str) -> Dict[str, str]:
    """Historical field name -> current field name for one table."""
    table = schema.get("tables", {}).get(table_key) or {}
    mapping: Dict[str, str] = {}
    for current_name, meta in table.get("fields", {}).items():
        for old_name in meta.get("renamed_from", []) or []:
            mapping[old_name] = current_name
    return mapping


def _build_rename_context(old_rows: List[Dict[str, Any]],
                         new_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """Merge both runs' embedded schemas into per-table old-name -> new-name maps.

    The candidate (new) run is authoritative; a baseline produced by an older
    rule contributes its own historical names so a rename chain spanning more
    than one rule version still resolves.
    """
    old_schema = _schema_of(old_rows)
    new_schema = _schema_of(new_rows)
    context: Dict[str, Dict[str, str]] = {}
    for table_key in set(old_schema["tables"]) | set(new_schema["tables"]):
        mapping: Dict[str, str] = {}
        mapping.update(_table_rename_map(old_schema, table_key))
        mapping.update(_table_rename_map(new_schema, table_key))
        context[table_key] = mapping
    return context


def _split_identity(identity: str) -> List[Tuple[str, str]]:
    """Split ``sample_id=S101|analyte=Au`` into [(name, value), ...]."""
    parts: List[Tuple[str, str]] = []
    for chunk in identity.split("|"):
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        parts.append((name, value))
    return parts


def _canonical_identity(record_type: str, identity: str,
                        rename_context: Dict[str, Dict[str, str]]) -> Tuple[Tuple[str, str], ...]:
    """Position/value based identity immune to identity-field renames.

    A sample record keyed by ``sample_id=S101`` in the baseline and
    ``sample_code=S101`` in the candidate yields the same canonical key, so the
    assay measurements still attach to the original sample.  Measurement
    identities historically use the bare token ``sample``; that alias is
    normalised to the rule's current sample field name.
    """
    parts = _split_identity(identity)
    table_key = _RECORD_TABLES.get(record_type)
    rename = dict(rename_context.get(table_key or "", {}))
    if record_type == "measurement":
        current_sample = _current_sample_field(rename_context)
        rename.setdefault("sample", current_sample)
        rename.setdefault("sample_id", current_sample)
    canonical: List[Tuple[str, str]] = []
    for name, value in parts:
        canonical.append((rename.get(name, name), value))
    return tuple(canonical)


def _current_sample_field(rename_context: Dict[str, Dict[str, str]]) -> str:
    assay = rename_context.get("assay_table", {})
    if "sample_id" in assay:
        return assay["sample_id"]
    return "sample_id"


def _index_documents(rows: List[Dict[str, Any]],
                     rename_context: Dict[str, Dict[str, str]]
                     ) -> Dict[Tuple[str, str, Tuple[Tuple[str, str], ...]], Dict[str, Any]]:
    index: Dict[Tuple[str, str, Tuple[Tuple[str, str], ...]], Dict[str, Any]] = {}
    for row in rows:
        borehole = row["borehole_id"]
        for interval in row.get("intervals", []):
            key = (borehole, "interval",
                   _canonical_identity("interval", interval["identity"], rename_context))
            index[key] = interval
        for sample in row.get("samples", []):
            key = (borehole, "sample",
                   _canonical_identity("sample", sample["identity"], rename_context))
            index[key] = sample
        for measurement in row.get("measurements", []):
            key = (borehole, "measurement",
                   _canonical_identity("measurement", measurement["identity"], rename_context))
            index[key] = measurement
    return index


def _display_identity(record_type: str, identity: str,
                      rename_context: Dict[str, Dict[str, str]]) -> str:
    """Rewrite an old-style identity string to current field names."""
    table_key = _RECORD_TABLES.get(record_type)
    rename = dict(rename_context.get(table_key or "", {}))
    if record_type == "measurement":
        current_sample = _current_sample_field(rename_context)
        rename.setdefault("sample", current_sample)
        rename.setdefault("sample_id", current_sample)
    if not rename:
        return identity
    rewritten = []
    for chunk in identity.split("|"):
        if "=" not in chunk:
            rewritten.append(chunk)
            continue
        name, _, value = chunk.partition("=")
        rewritten.append(f"{rename.get(name, name)}={value}")
    return "|".join(rewritten)


def _all_warnings(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        for warning in row.get("warnings", []):
            out.append({**warning, "borehole_id": row["borehole_id"]})
    return out


# ------------------------------------------------------------------- compare
def compare_runs(old_rows: List[Dict[str, Any]], new_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    rename_context = _build_rename_context(old_rows, new_rows)
    old_index = _index_documents(old_rows, rename_context)
    new_index = _index_documents(new_rows, rename_context)
    changes: List[RecordChange] = []

    for key in sorted(set(old_index) | set(new_index), key=lambda k: (k[0], k[1], k[2])):
        _borehole, record_type, _canonical = key
        if key not in old_index:
            new_record = new_index[key]
            changes.append(RecordChange(record_type,
                                        _display_identity(record_type, new_record["identity"], rename_context),
                                        CHANGE_ADDED,
                                        new_provenance=new_record.get("provenance")))
            continue
        if key not in new_index:
            old_record = old_index[key]
            category = CHANGE_REMOVED
            if _record_disappeared_due_to_failure(old_record, new_rows):
                category = CHANGE_FAILED
            changes.append(RecordChange(record_type,
                                        _display_identity(record_type, old_record["identity"], rename_context),
                                        category,
                                        old_provenance=old_record.get("provenance")))
            continue
        field_changes = _compare_records(record_type, old_index[key], new_index[key], rename_context)
        if field_changes:
            changes.append(RecordChange(
                record_type,
                _display_identity(record_type, new_index[key]["identity"], rename_context),
                "changed", field_changes,
                old_index[key].get("provenance"), new_index[key].get("provenance"),
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


def _record_disappeared_due_to_failure(old_record, new_rows) -> bool:
    for row in new_rows:
        for warning in row.get("warnings", []):
            if warning.get("code") in ("EXTRACTION_FAILED", "TABLE_NOT_FOUND"):
                if warning.get("table") == old_record.get("provenance", {}).get("table"):
                    return True
    return False


def _compare_records(record_type: str, old_record: Dict[str, Any],
                     new_record: Dict[str, Any],
                     rename_context: Dict[str, Dict[str, str]]) -> List[FieldChange]:
    changes: List[FieldChange] = []
    table_key = _RECORD_TABLES.get(record_type, "")
    rename = rename_context.get(table_key, {})

    if "fields" in old_record or "fields" in new_record:
        old_fields = old_record.get("fields", {})
        new_fields = new_record.get("fields", {})
        # Align on the *current* field names: an old-only name is translated via
        # the rename map so a pure rename compares equal instead of looking like
        # a removal plus an addition.  Genuinely unknown old names stay untouched
        # and are reported as removals.
        aligned_old: Dict[str, Any] = {}
        for field_name, value in old_fields.items():
            aligned_old[rename.get(field_name, field_name)] = value
        for field_name in sorted(set(aligned_old) | set(new_fields)):
            old_name = _reverse_rename(rename, field_name)
            change = _compare_field(field_name, aligned_old.get(field_name),
                                    new_fields.get(field_name),
                                    old_record.get("provenance"),
                                    new_record.get("provenance"),
                                    old_name=old_name)
            if change:
                changes.append(change)

    if record_type == "measurement":
        change = _compare_field("value", old_record.get("value"), new_record.get("value"),
                                old_record.get("provenance"), new_record.get("provenance"))
        if change:
            changes.append(change)
        if old_record.get("method") != new_record.get("method"):
            changes.append(FieldChange(CHANGE_MEANING, "method",
                                       old_record.get("method"), new_record.get("method"),
                                       detail="measurement method identity changed"))
    return changes


def _reverse_rename(rename: Dict[str, str], current_name: str) -> Optional[str]:
    """The historical name (if any) that was renamed to ``current_name``."""
    for old_name, new_name in rename.items():
        if new_name == current_name and old_name != current_name:
            return old_name
    return None


def _compare_field(field_name, old, new, old_prov, new_prov,
                   old_name: Optional[str] = None) -> Optional[FieldChange]:
    rename_note = ""
    if old_name and old_name != field_name:
        rename_note = f"renamed from {old_name!r}"
    if old is None and new is None:
        return None
    if old is None:
        return FieldChange(CHANGE_ADDED, field_name, new=new, new_source=new_prov,
                           detail=rename_note)
    if new is None:
        # The field is reported under its current name so the report always
        # points at one stable vocabulary; the detail keeps the old name.
        return FieldChange(CHANGE_REMOVED, field_name, old=old, old_source=old_prov,
                           detail=rename_note)
    old_kind, new_kind = old.get("kind"), new.get("kind")
    if old_kind != new_kind:
        return FieldChange(CHANGE_MEANING, field_name, old, new, old_prov, new_prov,
                           detail="; ".join(x for x in (rename_note,
                                                        f"kind changed {old_kind} -> {new_kind}") if x))
    if old.get("unit") != new.get("unit"):
        return FieldChange(CHANGE_MEANING, field_name, old, new, old_prov, new_prov,
                           detail="; ".join(x for x in (rename_note,
                                                        f"unit changed {old.get('unit')} -> {new.get('unit')}") if x))
    if old_kind == "text":
        if old.get("value") != new.get("value"):
            return FieldChange(CHANGE_VALUE, field_name, old, new, old_prov, new_prov,
                               detail=rename_note)
        return None
    if not values_equivalent(old, new):
        return FieldChange(CHANGE_VALUE, field_name, old, new, old_prov, new_prov,
                           detail=rename_note)
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
    """Promote a successful, complete, conflict-free run to a baseline."""
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
    report["regression_passed"] = (
        report["regression_passed"]
        and candidate_run["status"] == "completed"
        and not candidate_run.get("has_conflicts")
        and snapshot_complete
    )
    report_id = f"diff-{baseline['baseline_id']}-{candidate_run['run_id']}"
    report["report_id"] = report_id
    store.save_report(report_id, report)
    return report
