"""Value and unit semantics.

Three distinct meanings must survive extraction and serialisation:

* **censored detection limits** -- ``<0.05`` means "below detection limit
  0.05", it is not the number 0.05;
* **measured zero** -- ``0`` is an actual measurement of zero;
* **missing** -- an empty cell / "N/A" means no measurement at all.

Depth is normalised to metres (centimetres are converted) and concentrations
to mg/kg (ug/kg values are divided by 1000).  The original text and unit are
always kept alongside the normalised value for traceability.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

MISSING_TOKENS = {"", "-", "--", "n/a", "na", "null", "none", "缺失", "无数据", "/"}

# Unit aliases -> canonical unit.
DEPTH_UNITS = {
    "m": "m", "米": "m", "metre": "m", "meter": "m", "meters": "m",
    "cm": "cm", "厘米": "cm",
}
CONC_UNITS = {
    "mg/kg": "mg/kg",
    "mg kg-1": "mg/kg",
    "mg·kg-1": "mg/kg",
    "毫克每千克": "mg/kg",
    "毫克/千克": "mg/kg",
    "ug/kg": "ug/kg",
    "μg/kg": "ug/kg",
    "ug kg-1": "ug/kg",
    "μg kg-1": "ug/kg",
    "微克每千克": "ug/kg",
    "微克/千克": "ug/kg",
    "ppb": "ug/kg",
}

UNIT_PATTERN = re.compile(r"\[\s*([^\]]+?)\s*\]|（\s*([^）]+?)\s*）|\(\s*([^)]+?)\s*\)")
NUMBER_PATTERN = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"


@dataclass
class ParsedValue:
    kind: str            # "number" | "censored" | "zero" | "missing"
    value: Optional[float]
    unit: Optional[str]
    raw_text: str
    limit: Optional[float] = None
    raw_unit: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "kind": self.kind,
            "value": self.value,
            "unit": self.unit,
            "raw_text": self.raw_text,
        }
        if self.raw_unit is not None:
            data["raw_unit"] = self.raw_unit
        if self.limit is not None:
            data["detection_limit"] = self.limit
        return data


def normalize_unit(raw_unit: Optional[str], dimension: str) -> Optional[str]:
    if raw_unit is None:
        return None
    key = re.sub(r"\s+", " ", raw_unit.strip().lower())
    table = DEPTH_UNITS if dimension == "depth" else CONC_UNITS
    if key in table:
        return table[key]
    # Tolerate surrounding whitespace inside symbols such as "mg / kg".
    compact = key.replace(" / ", "/").replace(" ", "")
    for alias, canonical in table.items():
        if alias.replace(" ", "") == compact:
            return canonical
    return raw_unit.strip()


def extract_unit(text: str) -> tuple[str, Optional[str]]:
    """Split a header like 'Au (mg/kg)' into ('Au', 'mg/kg')."""
    match = UNIT_PATTERN.search(text)
    if not match:
        return text.strip(), None
    unit = next(group for group in match.groups() if group)
    label = (text[: match.start()] + " " + text[match.end():]).strip()
    return re.sub(r"\s+", " ", label), unit.strip()


def _convert(value: float, canonical_unit: str, dimension: str) -> float:
    if dimension == "depth" and canonical_unit == "m":
        return value
    if dimension == "depth" and canonical_unit == "cm":
        return round(value / 100.0, 6)
    if dimension == "concentration" and canonical_unit == "mg/kg":
        return value
    if dimension == "concentration" and canonical_unit == "ug/kg":
        return round(value / 1000.0, 9)
    return value


def parse_value(raw: Any, dimension: str = "concentration",
                unit_hint: Optional[str] = None) -> ParsedValue:
    """Parse a cell into typed value semantics (see module docstring)."""
    text = "" if raw is None else str(raw).strip()
    canonical = normalize_unit(unit_hint, dimension)

    if text.lower() in MISSING_TOKENS or text in MISSING_TOKENS:
        return ParsedValue("missing", None, canonical, text, raw_unit=unit_hint)

    censored = text.startswith(("<", "＜"))
    numeric_text = text[1:].strip() if censored else text
    numeric = re.fullmatch(NUMBER_PATTERN, numeric_text.strip())
    if not numeric:
        # Unrecognised content is kept as missing-with-raw rather than invented.
        return ParsedValue("missing", None, canonical, text, raw_unit=unit_hint)

    number = float(numeric.group())
    converted = _convert(number, canonical, dimension) if canonical else number
    # After conversion the stored unit is the canonical one; the original unit
    # remains in raw_unit, so equivalent expressions compare as identical.
    stored_unit = _canonical_target(canonical, dimension)
    if censored:
        return ParsedValue(
            "censored", converted, stored_unit, text,
            limit=converted, raw_unit=unit_hint,
        )
    if number == 0.0:
        return ParsedValue("zero", 0.0, stored_unit, text, raw_unit=unit_hint)
    return ParsedValue("number", converted, stored_unit, text, raw_unit=unit_hint)


def _canonical_target(canonical_unit: Optional[str], dimension: str) -> Optional[str]:
    if canonical_unit is None:
        return None
    if dimension == "depth":
        return "m"
    if dimension == "concentration":
        return "mg/kg"
    return canonical_unit


def values_equivalent(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    """Semantic equality: same kind/value/unit; raw text may differ."""
    if a is None or b is None:
        return a is b
    if a.get("kind") != b.get("kind") or a.get("unit") != b.get("unit"):
        return False
    av, bv = a.get("value"), b.get("value")
    if av is None and bv is None:
        return True
    if av is None or bv is None:
        return False
    return abs(float(av) - float(bv)) < 1e-12
