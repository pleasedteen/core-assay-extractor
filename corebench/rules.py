"""Declarative extraction rule model.

Rules are pure data (edited as text, never Python).  A rule describes:

* page selectors for the borehole id and each structural table;
* field business meaning (depth / concentration / text), label aliases and
  default units;
* record identity (which fields make an interval / sample / measurement unique);
* analyte header aliases, so header text changes stay declarative;
* how many header layers each table has (multi-row headers).

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
    # A critical table must be extracted on every page that declares it: a
    # missing selector match / unparseable table / missing required column is a
    # data-loss gap that blocks baseline confirmation and regression passes.
    # Rules may opt out per table with ``required: false``.
    required: bool = True


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
                )
            tables[key] = TableSpec(
                key=key,
                selector=spec["selector"],
                header_layers=int(spec.get("header_layers", 1)),
                fields=fields,
                identity=list(spec.get("identity") or []),
                required=bool(spec.get("required", True)),
            )
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
