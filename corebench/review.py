"""Manual adjudication (review) of assay/record conflicts.

A *review* is an independent, persisted adjudication artifact created by a
human reviewer against an **existing run, which is never modified**.  For every
conflict the reviewer sees each candidate value together with its original-page
source, picks one candidate and records a reason.  Applying the review produces
a separate *reviewed run* whose output adopts the selected candidate's value
and source; unresolved conflicts stay in place and keep blocking baseline
confirmation.

When the same frozen snapshot is replayed with a new rule version, a past
adjudication may only be carried over when the replay produces the **same
candidate values and the same sources** for that conflict.  If the conflict
vanishes -- even when only the originally selected value remains -- or any
candidate value/source changes, the decision is flagged for re-review and the
regression fails.
"""
from __future__ import annotations

import copy
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from .merge import read_jsonl
from .values import values_equivalent

# Provenance fields that identify where a candidate came from.  A carried-over
# decision requires every one of these to match the replay's candidate source.
PINNED_SOURCE_KEYS = (
    "page_id", "url", "content_sha1", "table", "selector",
    "raw_row", "raw_cell", "header_path",
)

REVIEW_RESOLVED = "resolved"
REVIEW_PARTIAL = "partially_resolved"
STATUS_REUSABLE = "reusable"
STATUS_NEEDS_RECHECK = "needs_recheck"


class ReviewReuseError(ValueError):
    """Raised when an old adjudication cannot be carried onto a replay."""

    def __init__(self, message: str, evaluation: Dict[str, Any]):
        super().__init__(message)
        self.evaluation = evaluation


# --------------------------------------------------------------- small utils
def pin_source(source: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Project a provenance dict onto the source identity keys."""
    source = source or {}
    return {key: source.get(key) for key in PINNED_SOURCE_KEYS if key in source}


def _conflict_key(borehole_id: str, conflict: Dict[str, Any]) -> Tuple[str, str, str]:
    return borehole_id, conflict["identity"], conflict["field"]


def collect_conflicts(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten every conflict in a run's JSONL output with locating metadata."""
    flat: List[Dict[str, Any]] = []
    for row in rows:
        borehole_id = row["borehole_id"]
        for conflict_index, conflict in enumerate(row.get("conflicts", [])):
            flat.append({
                "borehole_id": borehole_id,
                "conflict_index": conflict_index,
                "identity": conflict["identity"],
                "field": conflict["field"],
                "values": conflict["values"],
                "sources": conflict["sources"],
            })
    return flat


def _candidate_pairs(conflict: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {"value": copy.deepcopy(value), "source": pin_source(source)}
        for value, source in zip(conflict["values"], conflict["sources"])
    ]


def _same_value(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return values_equivalent(left, right)


def _same_source(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return pin_source(left) == pin_source(right)


def _candidate_sets_match(frozen: List[Dict[str, Any]],
                          current: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """Every frozen candidate (value + source) must still be present."""
    if len(frozen) != len(current):
        return False, "candidate_set_changed"
    remaining = list(current)
    value_changed = False
    source_changed = False
    for frozen_candidate in frozen:
        found = None
        for index, candidate in enumerate(remaining):
            if _same_source(frozen_candidate["source"], candidate["source"]):
                found = index
                if not _same_value(frozen_candidate["value"], candidate["value"]):
                    value_changed = True
                break
        if found is None:
            # Same cell may now carry a different parsed value/source.
            for index, candidate in enumerate(remaining):
                if _same_value(frozen_candidate["value"], candidate["value"]):
                    found = index
                    source_changed = True
                    break
        if found is None:
            return False, "candidate_set_changed"
        remaining.pop(found)
    if value_changed:
        return False, "value_mismatch"
    if source_changed:
        return False, "source_mismatch"
    return True, ""


# ---------------------------------------------------------------- creation --
def create_review(store, run_id: str, decisions: List[Dict[str, Any]],
                  reviewer: str = "", note: str = "") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Validate reviewer decisions against a run, persist the review and build
    the independent reviewed run.  The original run record is never touched.
    """
    run = store.load_run(run_id)
    if run["status"] != "completed":
        raise ValueError("only completed runs can be reviewed")
    if not run.get("output_path"):
        raise ValueError("run has no extraction output")
    rows = read_jsonl(run["output_path"])
    flat = collect_conflicts(rows)
    if not flat:
        raise ValueError("run has no conflicts to review")
    if not decisions:
        raise ValueError("at least one adjudication decision is required")

    located = {
        (item["borehole_id"], item["identity"], item["field"], item["conflict_index"]): item
        for item in flat
    }
    decision_records: List[Dict[str, Any]] = []
    seen: set = set()
    for raw_decision in decisions:
        key = (raw_decision["borehole_id"], raw_decision["identity"],
               raw_decision["field"], int(raw_decision["conflict_index"]))
        if key not in located:
            raise ValueError(f"decision references unknown conflict: {key}")
        if key in seen:
            raise ValueError(f"duplicate decision for conflict: {key}")
        seen.add(key)
        reason = (raw_decision.get("reason") or "").strip()
        if not reason:
            raise ValueError("every adjudication must include a reason")
        conflict = located[key]
        selected_index = int(raw_decision["selected_index"])
        if not 0 <= selected_index < len(conflict["values"]):
            raise ValueError(f"selected_index {selected_index} out of range for {key[:3]}")
        decision_records.append({
            "borehole_id": key[0],
            "identity": key[1],
            "field": key[2],
            "conflict_index": key[3],
            "selected_index": selected_index,
            "reason": reason,
            # Freeze the candidates exactly as presented to the reviewer: the
            # replay guard compares against these value/source pairs.
            "candidates": _candidate_pairs(conflict),
        })

    covered = {(d["borehole_id"], d["identity"], d["field"], d["conflict_index"])
               for d in decision_records}
    unresolved = [
        {"borehole_id": item["borehole_id"], "identity": item["identity"],
         "field": item["field"], "conflict_index": item["conflict_index"]}
        for item in flat
        if (item["borehole_id"], item["identity"], item["field"],
            item["conflict_index"]) not in covered
    ]
    review_id = f"review-{run_id}-{int(time.time() * 1000)}"
    review = {
        "review_id": review_id,
        "source_run_id": run_id,
        "snapshot_id": run["snapshot_id"],
        "rule_id": run["rule_id"],
        "reviewer": reviewer,
        "note": note,
        "status": REVIEW_RESOLVED if not unresolved else REVIEW_PARTIAL,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "decisions": decision_records,
        "unresolved": unresolved,
    }
    store.save_review(review)
    reviewed_run = apply_review(store, run, review)
    return review, reviewed_run


# ------------------------------------------------------------- application --
def apply_review(store, run: Dict[str, Any], review: Dict[str, Any],
                 new_run_id: Optional[str] = None) -> Dict[str, Any]:
    """Build the independent reviewed output and reviewed run record.

    The reviewed output adopts, for every adjudicated conflict, the selected
    candidate's value *and* source.  Conflicts without a decision remain and
    keep blocking confirmation.
    """
    rows = read_jsonl(run["output_path"])
    decisions = {
        (d["borehole_id"], d["identity"], d["field"], d["conflict_index"]): d
        for d in review["decisions"]
    }
    reviewed_rows: List[Dict[str, Any]] = []
    for row in rows:
        doc = copy.deepcopy(row)
        adjudications: List[Dict[str, Any]] = []
        remaining: List[Dict[str, Any]] = []
        for conflict_index, conflict in enumerate(doc.get("conflicts", [])):
            key = (doc["borehole_id"], conflict["identity"], conflict["field"], conflict_index)
            decision = decisions.get(key)
            if decision is None:
                remaining.append(conflict)
                continue
            selected = decision["candidates"][decision["selected_index"]]
            entry = _adjudicate_conflict(doc, conflict, selected, decision, review)
            adjudications.append(entry)
        doc["conflicts"] = remaining
        if adjudications:
            doc["adjudications"] = adjudications
            doc["review_id"] = review["review_id"]
        reviewed_rows.append(doc)

    reviewed_run_id = new_run_id or f"{run['run_id']}-reviewed-{int(time.time() * 1000)}"
    output_path = store.run_output_path(reviewed_run_id)
    with open(output_path, "w", encoding="utf-8") as handle:
        for doc in reviewed_rows:
            handle.write(json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n")

    has_unresolved = any(doc.get("conflicts") for doc in reviewed_rows)
    reviewed_run = {
        "run_id": reviewed_run_id,
        "kind": "reviewed",
        "source_run_id": run["run_id"],
        "review_id": review["review_id"],
        "rule_id": run["rule_id"],
        "rule_text": run["rule_text"],
        "note": f"manual review of {run['run_id']} ({review['review_id']})",
        "status": "completed",
        "pages": list(run.get("pages", [])),
        "snapshot_id": run["snapshot_id"],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "has_conflicts": has_unresolved,
        "unresolved_conflicts": sum(len(doc.get("conflicts", [])) for doc in reviewed_rows),
        "has_warnings": any(doc.get("warnings") for doc in reviewed_rows),
        "output_path": output_path,
        "error": None,
        "snapshot_complete": run.get("snapshot_complete", True),
    }
    store.save_run(reviewed_run)
    return reviewed_run


def _adjudicate_conflict(doc: Dict[str, Any], conflict: Dict[str, Any],
                         selected: Dict[str, Any], decision: Dict[str, Any],
                         review: Dict[str, Any]) -> Dict[str, Any]:
    annotation = {
        "review_id": review["review_id"],
        "identity": conflict["identity"],
        "field": conflict["field"],
        "selected_index": decision["selected_index"],
        "reason": decision["reason"],
        "reviewer": review.get("reviewer", ""),
        "selected_source": copy.deepcopy(selected["source"]),
    }
    if any(measurement.get("identity") == conflict["identity"]
           for measurement in doc.get("measurements", [])):
        for measurement in doc["measurements"]:
            if measurement.get("identity") == conflict["identity"] and conflict["field"] == "value":
                measurement["value"] = copy.deepcopy(selected["value"])
                measurement["provenance"] = copy.deepcopy(selected["source"])
                measurement["adjudication"] = annotation
                break
    else:
        # Interval/sample record field conflict.
        for bucket in ("intervals", "samples"):
            for record in doc.get(bucket, []):
                if record.get("identity") == conflict["identity"]:
                    record["fields"][conflict["field"]] = copy.deepcopy(selected["value"])
                    record["adjudication"] = annotation
                    break
    return annotation


# --------------------------------------------------------- replay reuse guard
def evaluate_review_on_rows(review: Dict[str, Any], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Check whether every past decision still applies to a replay's output.

    A decision is reusable only when the replay still carries the conflict and
    the full candidate set matches (value + source).  A missing conflict --
    even if only the selected value survives -- forces a re-review.
    """
    current: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        for conflict in row.get("conflicts", []):
            current.setdefault(_conflict_key(row["borehole_id"], conflict), [])
            current[_conflict_key(row["borehole_id"], conflict)].append(
                _candidate_pairs(conflict))

    items: List[Dict[str, Any]] = []
    reusable = 0
    needs_recheck = 0
    for decision in review["decisions"]:
        key = (decision["borehole_id"], decision["identity"], decision["field"])
        item: Dict[str, Any] = {
            "borehole_id": decision["borehole_id"],
            "identity": decision["identity"],
            "field": decision["field"],
            "selected_index": decision["selected_index"],
            "review_id": review["review_id"],
        }
        conflict_variants = current.get(key)
        if not conflict_variants:
            item.update(status=STATUS_NEEDS_RECHECK,
                        reason="conflict_no_longer_present",
                        detail="回放中该冲突已消失（即使仅剩原选中值），需重新人工复核")
            needs_recheck += 1
            items.append(item)
            continue
        # The merge emits at most one conflict per (identity, field); take it.
        matches, reason = _candidate_sets_match(decision["candidates"], conflict_variants[0])
        item["candidate_count"] = len(conflict_variants[0])
        if matches:
            item.update(status=STATUS_REUSABLE, reason="candidates_match",
                        detail="候选值与来源完全一致，可沿用原裁决")
            reusable += 1
        else:
            item.update(status=STATUS_NEEDS_RECHECK, reason=reason,
                        detail=_recheck_detail(reason))
            needs_recheck += 1
        items.append(item)

    # Any conflict the original review never covered must also be re-adjudicated.
    decision_keys = {(d["borehole_id"], d["identity"], d["field"])
                     for d in review["decisions"]}
    unreviewed: List[Dict[str, Any]] = []
    for (borehole_id, identity, field) in current:
        if (borehole_id, identity, field) not in decision_keys:
            unreviewed.append({"borehole_id": borehole_id, "identity": identity, "field": field})
            needs_recheck += 1

    all_reused = (needs_recheck == 0 and reusable == len(review["decisions"])
                  and not unreviewed)
    return {
        "review_id": review["review_id"],
        "source_run_id": review["source_run_id"],
        "status": REVIEW_RESOLVED if all_reused else REVIEW_PARTIAL,
        "reusable_count": reusable,
        "needs_recheck_count": needs_recheck,
        "items": items,
        "unreviewed_conflicts": unreviewed,
    }


def _recheck_detail(reason: str) -> str:
    return {
        "value_mismatch": "回放候选值与裁决时不一致，需重新人工复核",
        "source_mismatch": "回放候选来源与裁决时不一致，需重新人工复核",
        "candidate_set_changed": "回放候选集合发生变化，需重新人工复核",
    }.get(reason, "回放结果与裁决依据不一致，需重新人工复核")


def carry_review(store, review: Dict[str, Any], candidate_run: Dict[str, Any],
                 new_run_id: Optional[str] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Carry an adjudication onto a replay run, only when candidates match."""
    rows = read_jsonl(candidate_run["output_path"])
    evaluation = evaluate_review_on_rows(review, rows)
    if evaluation["needs_recheck_count"]:
        raise ReviewReuseError(
            "原裁决无法沿用：存在需重新复核的冲突，回归判为未通过", evaluation)

    # Re-freeze decisions against the candidate's own conflict entries; the
    # selected index is resolved by matching the frozen selected candidate.
    current_conflicts: Dict[Tuple[str, str, str], Tuple[Dict[str, Any], int, Dict[str, Any]]] = {}
    for row in rows:
        for index, conflict in enumerate(row.get("conflicts", [])):
            current_conflicts[(row["borehole_id"], conflict["identity"], conflict["field"])] = (
                row, index, conflict)

    carried_decisions: List[Dict[str, Any]] = []
    for decision in review["decisions"]:
        key = (decision["borehole_id"], decision["identity"], decision["field"])
        if key not in current_conflicts:  # pragma: no cover - guarded by evaluation
            raise ReviewReuseError("candidate conflict vanished during carry", evaluation)
        _row, conflict_index, conflict = current_conflicts[key]
        frozen_selected = decision["candidates"][decision["selected_index"]]
        selected_index = next(
            index for index, candidate in enumerate(_candidate_pairs(conflict))
            if _same_source(candidate["source"], frozen_selected["source"])
            and _same_value(candidate["value"], frozen_selected["value"]))
        carried_decisions.append({
            "borehole_id": decision["borehole_id"],
            "identity": decision["identity"],
            "field": decision["field"],
            "conflict_index": conflict_index,
            "selected_index": selected_index,
            "reason": decision["reason"],
        })

    note = f"carry-over of {review['review_id']}"
    if review.get("note"):
        note += f"; {review['note']}"
    carried_review, reviewed_run = create_review(
        store, candidate_run["run_id"], carried_decisions,
        reviewer=review.get("reviewer", ""), note=note)
    carried_review["carried_from_review_id"] = review["review_id"]
    carried_review["carried_from_run_id"] = review["source_run_id"]
    store.save_review(carried_review)
    if new_run_id and reviewed_run["run_id"] != new_run_id:  # pragma: no cover
        raise ValueError("new_run_id cannot currently be reassigned after creation")
    return carried_review, reviewed_run
