"""Page extraction: DOM -> typed, provenance-annotated business records.

One :class:`PageExtraction` holds every interval / sample / measurement found on
a single page plus structured warnings.  Every record carries ``provenance``
(page id, source url, content hash, table selector and raw row text) so results
and errors can always be traced back to the original page and raw content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .html_dom import Node, parse_html
from .rules import Rule, TableSpec, _norm
from .tables import Table, build_table, detect_header_layers
from .values import extract_unit, parse_value


@dataclass
class Warning_:
    code: str
    message: str
    page_id: str
    table: Optional[str] = None
    raw: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {"code": self.code, "message": self.message, "page_id": self.page_id}
        if self.table:
            data["table"] = self.table
        if self.raw is not None:
            data["raw"] = self.raw
        return data


@dataclass
class PageExtraction:
    page_id: str
    url: str
    content_sha1: str
    borehole_id: Optional[str]
    intervals: List[Dict[str, Any]] = field(default_factory=list)
    samples: List[Dict[str, Any]] = field(default_factory=list)
    measurements: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[Warning_] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_id": self.page_id,
            "url": page_url(self),
            "content_sha1": self.content_sha1,
            "borehole_id": self.borehole_id,
            "intervals": self.intervals,
            "samples": self.samples,
            "measurements": self.measurements,
            "warnings": [w.to_dict() for w in self.warnings],
        }


def page_url(extraction: PageExtraction) -> str:
    return extraction.url


def select_nodes(root: Node, selector: str) -> List[Node]:
    """Very small selector: tag, ``.class`` or ``tag.class`` (no combinators)."""
    selector = selector.strip()
    if "." in selector:
        tag, class_name = selector.split(".", 1)
    else:
        tag, class_name = selector, None
    result = []
    for node in root.iter_descendants(tag or None):
        if class_name is None or class_name in node.get("class", "").split():
            result.append(node)
    return result


def _header_key(header_path: Sequence[str]) -> str:
    return "|".join(_norm(part) for part in header_path if part)


def _match_field(spec: TableSpec, header_path: Sequence[str]):
    for field_spec in spec.fields.values():
        if field_spec.matches(header_path):
            return field_spec
    return None


def _coerce_field(field_spec, cell_text: str, header_path: Sequence[str],
                  unit_hint: Optional[str]) -> Dict[str, Any]:
    _, header_unit = extract_unit(" ".join(header_path))
    if field_spec.type == "text":
        return {"kind": "text", "value": cell_text, "unit": None, "raw_text": cell_text}
    # Header-declared and inline units override the rule default: the unit is
    # written in the table header on these archive pages.
    unit = unit_hint or header_unit or field_spec.default_unit
    dimension = field_spec.type if field_spec.type in ("depth", "concentration") else "text"
    return parse_value(cell_text, dimension=dimension, unit_hint=unit).to_dict()


def extract_page(page, rule: Rule) -> PageExtraction:
    root = parse_html(page.html)
    result = PageExtraction(
        page_id=page.page_id, url=page.url, content_sha1=page.content_sha1,
        borehole_id=None,
    )

    result.borehole_id = _extract_borehole_id(root, rule, result)
    provenance_base = {"page_id": page.page_id, "url": page.url, "content_sha1": page.content_sha1}

    for key, spec in rule.tables.items():
        nodes = select_nodes(root, spec.selector)
        if not nodes:
            result.warnings.append(Warning_("TABLE_NOT_FOUND", f"selector {spec.selector!r} matched nothing",
                                            page.page_id, table=key))
            continue
        for table_index, table_node in enumerate(nodes):
            try:
                layers = spec.header_layers or detect_header_layers(table_node)
                table = build_table(table_node, header_layers=layers)
                _extract_table(table, spec, rule, key, table_index, provenance_base, result)
            except Exception as exc:  # noqa: BLE001 - extraction failure is a recorded warning
                result.warnings.append(
                    Warning_("EXTRACTION_FAILED", f"{type(exc).__name__}: {exc}", page.page_id,
                             table=key, raw=table_node.outer_html()[:2000])
                )
    return result


def _extract_borehole_id(root: Node, rule: Rule, result: PageExtraction) -> Optional[str]:
    for node in select_nodes(root, rule.borehole_selector):
        text = node.normalize_text()
        for label in rule.borehole_labels:
            match = re.search(re.escape(label) + r"\s*[:：]?\s*([A-Za-z0-9_\-]+)", text)
            if match:
                return match.group(1)
        if text and not rule.borehole_labels:
            return text
    result.warnings.append(Warning_("BOREHOLE_ID_MISSING", "borehole id could not be located",
                                    result.page_id))
    return None


def _extract_table(table: Table, spec: TableSpec, rule: Rule, table_key: str,
                   table_index: int, provenance_base: dict, result: PageExtraction) -> None:
    header_paths = table.header_columns()
    field_by_col: Dict[int, Any] = {}
    analyte_by_col: Dict[int, Any] = {}
    unit_by_col: Dict[int, Optional[str]] = {}
    explained_cols: set = set()

    if table_key == rule.measurement_table and rule.long_format and _is_long_table(header_paths, rule):
        _extract_long_table(table, header_paths, spec, rule, provenance_base, result)
        return

    for col, path in enumerate(header_paths):
        field_spec = _match_field(spec, path)
        if field_spec:
            field_by_col[col] = field_spec
            explained_cols.add(col)
            continue
        if table_key == rule.measurement_table:
            analyte = rule.analyte_for(path)
            if analyte:
                # Unit is declared in the method layer of the multi-row header,
                # e.g. ["Cu", "ICP-AES (mg/kg)"]; search every layer.
                header_unit = None
                for part in path:
                    _, part_unit = extract_unit(part)
                    if part_unit:
                        header_unit = part_unit
                analyte_by_col[col] = (analyte, header_unit, path)
                explained_cols.add(col)
    for col, path in enumerate(header_paths):
        header_unit = None
        for part in path:
            _, part_unit = extract_unit(part)
            if part_unit:
                header_unit = part_unit
        unit_by_col[col] = header_unit
        # Rule cannot interpret an existing column: record it so a later diff
        # never mistakes an unexplained field for a normal deletion.
        if col not in explained_cols and any(part.strip() for part in path):
            result.warnings.append(
                Warning_("UNEXPLAINED_COLUMN",
                         f"rule cannot interpret header {_header_key(path)!r} in table {table_key!r}",
                         result.page_id, table=table_key, raw=" / ".join(path))
            )

    for row in table.body_rows():
        cells = [row[c] if c < len(row) else None for c in range(table.ncols)]
        if not any(cell and cell.clean for cell in cells):
            continue
        raw_row = [cell.clean if cell else "" for cell in cells]
        record: Dict[str, Any] = {"fields": {}, "provenance": {
            **provenance_base,
            "table": table_key,
            "table_index": table_index,
            "selector": spec.selector,
            "raw_row": raw_row,
        }}
        identity_parts: Dict[str, str] = {}
        for col, field_spec in field_by_col.items():
            cell = cells[col]
            text, inline_unit = _split_inline_unit(cell.clean if cell else "")
            unit = inline_unit or unit_by_col.get(col)
            typed = _coerce_field(field_spec, text, header_paths[col], unit)
            record["fields"][field_spec.name] = typed
            identity_parts[field_spec.name] = text
        record["identity"] = _build_identity(spec.identity, identity_parts, record["fields"])
        if table_key == rule.measurement_table:
            _collect_measurements(cells, analyte_by_col, header_paths, record, provenance_base,
                                  raw_row, spec, result, rule)
        _attach_record(table_key, record, result)


def _split_inline_unit(text: str) -> tuple[str, Optional[str]]:
    # Cell text may contain residual inline markup such as "1300<small>(cm)</small>",
    # sometimes repeated because of missing closing tags.
    stripped = re.sub(r"</?[a-zA-Z][^>]*>", "", text)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    unit: Optional[str] = None
    def _consume(match: re.Match) -> str:
        nonlocal unit
        if unit is None:
            unit = match.group(1).strip()
        return " "
    stripped = re.sub(r"[(（]\s*([^)）]+?)\s*[)）]", _consume, stripped)
    return stripped.strip(), unit


def _is_long_table(header_paths: Sequence[Sequence[str]], rule: Rule) -> bool:
    flat = {_norm(part) for path in header_paths for part in path}
    wanted = {_norm(label) for label in rule.long_format.analyte_labels}
    return bool(wanted & flat)


def _find_col(header_paths: Sequence[Sequence[str]], labels: Sequence[str]) -> Optional[int]:
    wanted = {_norm(label) for label in labels}
    for col, path in enumerate(header_paths):
        if wanted & {_norm(part) for part in path}:
            return col
    return None


def _extract_long_table(table: Table, header_paths, spec, rule, provenance_base, result) -> None:
    sample_field = rule.sample_identity_field(spec.key)
    sample_col = _find_col(header_paths, spec.fields[sample_field].labels)
    analyte_col = _find_col(header_paths, rule.long_format.analyte_labels)
    method_col = _find_col(header_paths, rule.long_format.method_labels)
    value_col = _find_col(header_paths, rule.long_format.value_labels)
    if None in (sample_col, analyte_col, method_col, value_col):
        result.warnings.append(Warning_("EXTRACTION_FAILED",
                                        "long-format assay table is missing a required column",
                                        result.page_id, table=spec.key))
        return
    # Any header column the rule cannot map must never be silently dropped:
    # it is recorded as unexplained so a later diff cannot call it a deletion.
    explained = {sample_col, analyte_col, method_col, value_col}
    for col, path in enumerate(header_paths):
        if col in explained or not any(part.strip() for part in path):
            continue
        result.warnings.append(Warning_(
            "UNEXPLAINED_COLUMN",
            f"rule cannot interpret header {_header_key(path)!r} in table {spec.key!r}",
            result.page_id, table=spec.key, raw=" / ".join(path)))
    for row in table.body_rows():
        cells = [row[c] if c < len(row) else None for c in range(table.ncols)]
        if not any(cell and cell.clean for cell in cells):
            continue
        raw_row = [cell.clean if cell else "" for cell in cells]
        sample_id = cells[sample_col].clean if cells[sample_col] else ""
        analyte_text = cells[analyte_col].clean if cells[analyte_col] else ""
        method_raw = cells[method_col].clean if cells[method_col] else None
        method = rule.canonicalize_method(method_raw)
        value_text, inline_unit = _split_inline_unit(cells[value_col].clean if cells[value_col] else "")
        analyte = rule.analyte_for([analyte_text])
        if analyte is None:
            result.warnings.append(Warning_("UNEXPLAINED_COLUMN",
                                            f"unknown analyte token {analyte_text!r}",
                                            result.page_id, table=spec.key, raw=analyte_text))
            continue
        parsed = parse_value(value_text, dimension="concentration", unit_hint=inline_unit)
        # The identity token stays the stable legacy spelling while the field
        # keeps its historical name; a renamed identity field (e.g. sample_id
        # -> sample_code) renames the token as well, and the diff bridges both.
        identity_token = "sample" if sample_field == "sample_id" else sample_field
        result.measurements.append({
            sample_field: sample_id,
            "analyte": analyte.code,
            "method": method,
            "value": parsed.to_dict(),
            "identity": f"{identity_token}={sample_id}|analyte={analyte.code}|method={method}",
            "header": " / ".join(header_paths[value_col]),
            "provenance": {
                **provenance_base,
                "table": spec.key,
                "selector": spec.selector,
                "raw_cell": value_text,
                "raw_row": raw_row,
                "header_path": list(header_paths[value_col]),
            },
        })


def _build_identity(identity_fields: List[str], raw_parts: Dict[str, str],
                    typed_fields: Dict[str, Any]) -> str:
    parts = []
    for name in identity_fields:
        if name in identity_fields and raw_parts.get(name, "") != "":
            parts.append(f"{name}={raw_parts[name]}")
        elif name in typed_fields and typed_fields[name].get("value") is not None:
            parts.append(f"{name}={typed_fields[name]['value']}")
    return "|".join(parts)


def _method_from_path(path: Sequence[str], analyte, rule: "Rule") -> Optional[str]:
    analyte_norms = {_norm(label) for label in analyte.labels}
    for part in path:
        label, _unit = extract_unit(part)
        norm = _norm(label)
        if norm and norm not in analyte_norms:
            return rule.canonicalize_method(label.strip())
    return None


def _collect_measurements(cells, analyte_by_col, header_paths, record,
                          provenance_base, raw_row, spec, result, rule) -> None:
    sample_field = rule.sample_identity_field(spec.key)
    sample_id = None
    if sample_field in record.get("fields", {}):
        sample_id = record["fields"][sample_field].get("value")
    if sample_id is None:
        # Tolerate legacy rules where the identity column keeps an older name.
        for field_name, value in record["fields"].items():
            if "sample" in field_name.lower():
                sample_id = value.get("value")
                break
    for col, (analyte, header_unit, path) in analyte_by_col.items():
        cell = cells[col]
        text = cell.clean if cell else ""
        parsed = parse_value(text, dimension="concentration", unit_hint=header_unit)
        method = _method_from_path(path, analyte, rule)
        identity_token = "sample" if sample_field == "sample_id" else sample_field
        measurement = {
            sample_field: sample_id,
            "analyte": analyte.code,
            "method": method,
            "value": parsed.to_dict(),
            "identity": f"{identity_token}={sample_id}|analyte={analyte.code}|method={method}",
            "header": " / ".join(path),
            "provenance": {
                **provenance_base,
                "table": spec.key,
                "selector": spec.selector,
                "raw_cell": text,
                "raw_row": raw_row,
                "header_path": list(path),
            },
        }
        result.measurements.append(measurement)


def _attach_record(table_key: str, record: Dict[str, Any], result: PageExtraction) -> None:
    # Only dedicated structural tables create interval/sample records; assay
    # tables merely contribute measurements (wide tables still echo sample ids).
    if table_key.endswith("interval_table"):
        result.intervals.append(record)
    elif table_key.endswith("sample_table"):
        result.samples.append(record)
