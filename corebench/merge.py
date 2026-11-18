"""Cross-page merge and canonical JSONL assembly.

Pages of the same borehole may repeat the same sample or only carry part of
its measurements.  Merging is identity based and order independent:

* intervals / samples are keyed by their rule-declared identity;
* measurements are keyed by ``(sample, analyte, method)``;
* repeated equal measurements collapse (unit-equivalent normalised values);
* repeated contradictory measurements produce a **conflict** that blocks
  baseline confirmation;
* missing values never overwrite real measurements and vice versa.

When a :class:`~corebench.rules.Rule` is supplied, each emitted document embeds
the rule's self-describing output schema, including per-field ``renamed_from``
history, so a later semantic diff can align renamed business fields (and
renamed identity fields) across rule versions without the rule text.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .extractor import PageExtraction
from .values import values_equivalent

if TYPE_CHECKING:
    from .rules import Rule


@dataclass
class Conflict:
    identity: str
    field: str
    values: List[Dict[str, Any]]
    sources: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "identity": self.identity,
            "field": self.field,
            "values": self.values,
            "sources": self.sources,
        }


@dataclass
class BoreholeDocument:
    borehole_id: str
    intervals: List[Dict[str, Any]] = field(default_factory=list)
    samples: List[Dict[str, Any]] = field(default_factory=list)
    measurements: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    page_ids: List[str] = field(default_factory=list)
    schema: Optional[Dict[str, Any]] = None

    def to_jsonl_obj(self) -> Dict[str, Any]:
        out = {
            "borehole_id": self.borehole_id,
            "intervals": self.intervals,
            "samples": self.samples,
            "measurements": self.measurements,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "warnings": self.warnings,
            "source_pages": sorted(self.page_ids),
        }
        if self.schema is not None:
            # Self-describing field-rename metadata: a diff against an older
            # baseline can match renamed business fields without the rule text.
            out["schema"] = self.schema
        return out


def merge_extractions(extractions: List[PageExtraction],
                      rule: Optional["Rule"] = None) -> List[BoreholeDocument]:
    docs: Dict[str, BoreholeDocument] = {}
    schema = rule.output_schema() if rule is not None else None
    for extraction in sorted(extractions, key=lambda e: e.page_id):  # deterministic; order independent result
        doc = docs.setdefault(extraction.borehole_id or f"unknown:{extraction.page_id}",
                              BoreholeDocument(borehole_id=extraction.borehole_id or "unknown",
                                               schema=schema))
        doc.page_ids.append(extraction.page_id)
        for warning in extraction.warnings:
            doc.warnings.append(warning.to_dict())
        _merge_records(doc.intervals, extraction.intervals, doc, kind="interval")
        _merge_records(doc.samples, extraction.samples, doc, kind="sample")
        _merge_measurements(doc, extraction.measurements)
    for doc in docs.values():
        doc.intervals.sort(key=lambda r: r.get("identity", ""))
        doc.samples.sort(key=lambda r: r.get("identity", ""))
        doc.measurements.sort(key=lambda m: m["identity"])
        doc.page_ids.sort()
    return [docs[key] for key in sorted(docs)]


def _merge_records(bucket: List[Dict[str, Any]], incoming: List[Dict[str, Any]],
                   doc: BoreholeDocument, kind: str) -> None:
    existing = {record["identity"]: record for record in bucket}
    for record in incoming:
        ident = record["identity"]
        if ident not in existing:
            bucket.append(record)
            existing[ident] = record
            continue
        current = existing[ident]
        for field_name, new_value in record["fields"].items():
            old_value = current["fields"].get(field_name)
            if old_value is None:
                if not _is_missing(new_value):
                    current["fields"][field_name] = new_value
                continue
            if _is_missing(new_value):
                continue
            if not _field_equivalent(field_name, old_value, new_value):
                doc.conflicts.append(Conflict(
                    identity=ident,
                    field=field_name,
                    values=[old_value, new_value],
                    sources=[current["provenance"], record["provenance"]],
                ))
        current["provenance"] = _merge_provenance(current["provenance"], record["provenance"])


def _merge_measurements(doc: BoreholeDocument, incoming: List[Dict[str, Any]]) -> None:
    existing = {m["identity"]: m for m in doc.measurements}
    for measurement in incoming:
        ident = measurement["identity"]
        if ident not in existing:
            doc.measurements.append(measurement)
            existing[ident] = measurement
            continue
        current = existing[ident]
        if _is_missing(measurement["value"]):
            continue
        if _is_missing(current["value"]):
            current["value"] = measurement["value"]
            current["provenance"] = _merge_provenance(current["provenance"], measurement["provenance"])
            continue
        if not values_equivalent(current["value"], measurement["value"]):
            doc.conflicts.append(Conflict(
                identity=ident,
                field="value",
                values=[current["value"], measurement["value"]],
                sources=[current["provenance"], measurement["provenance"]],
            ))


def _is_missing(value: Optional[Dict[str, Any]]) -> bool:
    return value is None or value.get("kind") == "missing"


def _field_equivalent(field_name: str, a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if a.get("kind") == "text" or b.get("kind") == "text":
        return a.get("value") == b.get("value")
    return values_equivalent(a, b)


def _merge_provenance(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(a)
    pages = []
    for source in (a, b):
        page = source.get("page_id")
        if page and page not in pages:
            pages.append(page)
    merged["page_ids"] = pages
    merged["also_seen_in"] = b.get("page_id")
    return merged


def write_jsonl(documents: List[BoreholeDocument], path: str) -> str:
    with open(path, "w", encoding="utf-8") as handle:
        for doc in documents:
            handle.write(json.dumps(doc.to_jsonl_obj(), ensure_ascii=False, sort_keys=True) + "\n")
    return path


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def has_blocking_conflicts(documents: List[BoreholeDocument]) -> bool:
    return any(doc.conflicts for doc in documents)
