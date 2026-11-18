"""Declarative extraction rule model.

Rules are pure data (edited as text, never Python).  A rule describes:

* page selectors for the borehole id and each structural table;
* field business meaning (depth / concentration / text), label aliases and
  default units;
* record identity (which fields make an interval / sample / measurement unique);
* analyte header aliases, so header text changes stay declarative;
* how many header layers each table has (multi-row headers);
* output field rename history (``renamed_from``): historical output names that
  denote the same business field as the current field name.  New JSONL is
  written with the new name; the rule's output schema (embedded in every JSONL
  document) lets semantic diffs align the field across rule versions --
  including renamed identity fields -- instead of reporting delete + add.

Changing page structure means editing this text -- no Python changes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from . import dsl
from .values import extract_unit


@dataclass
class FieldSpec:
    name: str
    labels: List[str]
    type: str = "text"                    # text | depth | concentration
    default_unit: Optional[str] = None
    required: bool = False
    renamed_from: List[str] = field(default_factory=list)

    def matches(self, header_path: Sequence[str]) -> bool:
        return _header_matches(header_path, self.labels)


@dataclass
class AnalyteSpec:
    code: str
    labels: List[str]

    def matches(self, header_path: Sequence[str]) -> bool:
        return _header_matches(header_path, self.labels)


@dataclass
class MethodSpec:
    code: str
    labels: List[str]

    def match_text(self, text: str) -> bool:
        if not text:
            return False
        return _norm(text) in {_norm(label) for label in self.labels}


@dataclass
class TableSpec:
    key: str
    selector: str
    header_layers: int = 1
    fields: Dict[str, FieldSpec] = field(default_factory=dict)
    identity: List[str] = field(default_factory=list)

    def rename_map(self) -> Dict[str, str]:
        """Historical output name -> current output name for this table."""
        mapping: Dict[str, str] = {}
        for spec in self.fields.values():
            for old_name in spec.renamed_from:
                mapping[old_name] = spec.name
        return mapping

    def identity_specs(self) -> List[FieldSpec]:
        return [self.fields[name] for name in self.identity if name in self.fields]


@dataclass
class LongFormatSpec:
    analyte_labels: List[str]
    method_labels: List[str]
    value_labels: List[str]


@dataclass
class Rule:
    name: str
    rule_version: str
    borehole_labels: List[str]
    borehole_selector: str
    tables: Dict[str, TableSpec]
    analytes: Dict[str, AnalyteSpec]
    methods: Dict[str, MethodSpec]
    measurement_table: str
    long_format: Optional[LongFormatSpec]
    raw: Dict[str, Any]

    # ------------------------------------------------------------------ load
    @classmethod
    def from_text(cls, text: str) -> "Rule":
        data = dsl.parse(text)
        if not isinstance(data, dict):
            raise ValueError("rule root must be a mapping")
        page = data.get("page", {})
        if not isinstance(page, dict):
            raise ValueError("rule 'page' section must be a mapping")
        borehole = page.get("borehole", {})
        if not isinstance(borehole, dict):
            raise ValueError("rule 'page.borehole' section must be a mapping")
        tables: Dict[str, TableSpec] = {}
        for key, spec in (page.get("tables") or {}).items():
            fields: Dict[str, FieldSpec] = {}
            for fname, fdef in (spec.get("fields") or {}).items():
                fields[fname] = FieldSpec(
                    name=fname,
                    labels=list(fdef.get("labels") or []),
                    type=fdef.get("type", "text"),
                    default_unit=fdef.get("unit"),
                    required=bool(fdef.get("required", False)),
                    renamed_from=list(fdef.get("renamed_from") or []),
                )
            tables[key] = TableSpec(
                key=key,
                selector=spec["selector"],
                header_layers=int(spec.get("header_layers", 1)),
                fields=fields,
                identity=list(spec.get("identity") or []),
            )
            _validate_table_renames(key, tables[key])
        analytes: Dict[str, AnalyteSpec] = {}
        for item in data.get("analytes", []):
            analytes[item["code"]] = AnalyteSpec(code=item["code"], labels=list(item.get("labels") or []))
        methods: Dict[str, MethodSpec] = {}
        for item in data.get("methods", []):
            methods[item["code"]] = MethodSpec(code=item["code"], labels=list(item.get("labels") or []))
        measurement_table = data.get("measurement", {}).get("table")
        if measurement_table not in tables:
            raise ValueError(f"measurement.table {measurement_table!r} is not declared in page.tables")
        long_cfg = data.get("measurement", {}).get("long_format")
        long_format = None
        if long_cfg:
            long_format = LongFormatSpec(
                analyte_labels=list(long_cfg.get("analyte_column") or []),
                method_labels=list(long_cfg.get("method_column") or []),
                value_labels=list(long_cfg.get("value_column") or []),
            )
        return cls(
            name=str(data.get("name", "unnamed rule")),
            rule_version=str(data.get("rule_version", data.get("version", "0"))),
            borehole_labels=list(borehole.get("labels") or []),
            borehole_selector=borehole.get("selector", "h1"),
            tables=tables,
            analytes=analytes,
            methods=methods,
            measurement_table=measurement_table,
            long_format=long_format,
            raw=data,
        )

    # --------------------------------------------------------------- helpers
    def analyte_for(self, header_path: Sequence[str]) -> Optional[AnalyteSpec]:
        for analyte in self.analytes.values():
            if analyte.matches(header_path):
                return analyte
        return None

    def canonicalize_method(self, text: Optional[str]) -> Optional[str]:
        if text is None:
            return None
        for method in self.methods.values():
            if method.match_text(text):
                return method.code
        return text.strip() or None

    def to_text(self) -> str:
        return dsl.dump(self.raw)

    # ------------------------------------------------- field rename metadata
    def field_renames(self) -> Dict[str, Dict[str, str]]:
        """Per table: historical output field name -> current output name.

        A rename declared here means the two names denote the *same* business
        field: new JSONL is written with the new name, while diffs against old
        baselines match the field instead of reporting a delete plus an add.
        """
        return {key: table.rename_map() for key, table in self.tables.items()}

    def output_schema(self) -> Dict[str, Any]:
        """Self-describing output schema, embedded in every JSONL document."""
        tables: Dict[str, Any] = {}
        for key, table in self.tables.items():
            tables[key] = {
                "identity": list(table.identity),
                "fields": {
                    spec.name: {
                        "type": spec.type,
                        "renamed_from": list(spec.renamed_from),
                    }
                    for spec in table.fields.values()
                },
            }
        return {
            "rule_version": self.rule_version,
            "measurement_table": self.measurement_table,
            "tables": tables,
        }

    def sample_identity_field(self, table_key: Optional[str] = None) -> str:
        """The current output name of the assay table's sample identity field.

        Measurement records carry the sample id under this name even when the
        field was renamed from e.g. ``sample_id`` to ``sample_code``.
        """
        table = self.tables[table_key or self.measurement_table]
        for name in table.identity:
            if "sample" in name.lower():
                return name
        # Fall back to any field whose own/historical name mentions sample.
        for spec in table.fields.values():
            if "sample" in spec.name.lower() or any(
                    "sample" in old.lower() for old in spec.renamed_from):
                return spec.name
        return "sample_id"


def _validate_table_renames(table_key: str, table: TableSpec) -> None:
    current_names = set(table.fields)
    seen_old: Dict[str, str] = {}
    for spec in table.fields.values():
        for old_name in spec.renamed_from:
            if old_name == spec.name:
                raise ValueError(
                    f"table {table_key!r} field {spec.name!r}: renamed_from cannot "
                    f"list its own name")
            if old_name in current_names:
                raise ValueError(
                    f"table {table_key!r} field {spec.name!r}: renamed_from name "
                    f"{old_name!r} is itself a declared field")
            if old_name in seen_old:
                raise ValueError(
                    f"table {table_key!r}: historical field {old_name!r} is claimed "
                    f"by both {seen_old[old_name]!r} and {spec.name!r}")
            seen_old[old_name] = spec.name
    for identity_name in table.identity:
        if identity_name not in table.fields:
            raise ValueError(
                f"table {table_key!r}: identity field {identity_name!r} is not "
                f"declared in fields")
        spec = table.fields[identity_name]
        for other_name, other in table.fields.items():
            if other_name == spec.name:
                continue
            if spec.name in other.renamed_from or any(
                    old in other.renamed_from for old in spec.renamed_from):
                raise ValueError(
                    f"table {table_key!r}: identity field {spec.name!r} overlaps "
                    f"with rename chain of {other_name!r}")


def _norm(label: str) -> str:
    label, _ = extract_unit(label)
    return re.sub(r"[\s:：*]+", "", label).lower()


def _header_matches(header_path: Sequence[str], candidates: Sequence[str]) -> bool:
    if not header_path:
        return False
    norms = [_norm(part) for part in header_path if part]
    joined = "".join(norms)
    for candidate in candidates:
        wanted = _norm(candidate)
        if wanted in norms or wanted == joined:
            return True
    return False
