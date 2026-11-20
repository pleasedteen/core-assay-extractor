"""Explicit text decoding for archive pages.

Old drill-archive pages are frequently encoded in GB18030 while the HTTP
response claims ``utf-8``; a single batch may mix encodings page by page.  The
decoder therefore never assumes the header is right:

* a rule may name the exact encoding for a page (or a default);
* the header charset is only a hint used when the rule says nothing;
* a byte-order mark wins over a header when present;
* decoding is **strict**.  A page that cannot be decoded is not turned into
  invented replacement text -- the original bytes are kept and the failure is
  reported so a later rule can correct the encoding and re-decode the frozen
  snapshot without the source being reachable.
"""
from __future__ import annotations

import codecs
from typing import Optional

DEFAULT_ENCODING = "utf-8"

# Longest signatures first so UTF-32 is not mistaken for UTF-16.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def normalize_encoding(name: str) -> str:
    """Canonicalise an encoding name, rejecting unknown codecs early."""
    try:
        return codecs.lookup(str(name).strip()).name
    except (LookupError, TypeError) as exc:
        raise ValueError(f"unknown text encoding {name!r}") from exc


def sniff_bom(raw: bytes) -> Optional[str]:
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return encoding
    return None


def decode_bytes(raw: bytes, encoding: str) -> tuple[str, Optional[str]]:
    """Strictly decode ``raw``.

    Returns ``(text, error)``.  On any decoding failure the text is empty and
    ``error`` describes the problem: callers must keep the raw bytes rather
    than treat replacement characters as page content.
    """
    try:
        return raw.decode(encoding), None
    except (UnicodeDecodeError, LookupError) as exc:
        return "", f"{type(exc).__name__}: {exc}"


def resolve_encoding(page_id: str, declared: Optional[str], spec, recorded: Optional[str],
                     raw: Optional[bytes]) -> str:
    """Pick the encoding for one page.

    Priority: per-page rule override, rule default, the encoding the original
    run recorded, a byte-order mark, the header-declared charset, UTF-8.
    Rules win over the header because archive headers are known to lie; the
    recorded choice is a replay fallback so an unchanged rule reproduces the
    original text exactly.
    """
    if spec is not None:
        override = spec.for_page(page_id)
        if override:
            return normalize_encoding(override)
        if spec.default:
            return normalize_encoding(spec.default)
    if recorded:
        return normalize_encoding(recorded)
    bom = sniff_bom(raw or b"")
    if bom:
        return bom
    if declared:
        try:
            return normalize_encoding(declared)
        except ValueError:
            pass
    return DEFAULT_ENCODING


def try_normalize_encoding(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    try:
        return normalize_encoding(name)
    except ValueError:
        return None
