"""Shared helpers for the corebench test suite (offline, stdlib only)."""
from __future__ import annotations

import os
import sys
import tempfile
import urllib.request
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
RULE_V1 = os.path.join(FIXTURES, "rules", "rule_v1.dsl")
RULE_V2 = os.path.join(FIXTURES, "rules", "rule_v2.dsl")
EXPECTED = os.path.join(FIXTURES, "expected", "expected.jsonl")


def rule_text(version: int) -> str:
    path = RULE_V1 if version == 1 else RULE_V2
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def read_jsonl(path: str):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def static_manifest(directory: str, layout_b: bool = False):
    from corebench import mock_site
    return mock_site.write_static_site(directory, layout_b=layout_b)


def almost_equal(a, b, tol: float = 1e-9) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def scaled_dataset(factor: int):
    """Borehole pages repeated ``factor`` times as separate boreholes.

    The test corpus is grown by adding *complete boreholes* of the same shape
    and size as the fixtures, not by making any single borehole bigger.  Page
    ids carry a ``-c<n>`` suffix for copies so every page stays distinct.
    """
    from corebench import mock_site
    dataset = {}
    for copy_index in range(factor):
        for borehole, pages in mock_site.DATASET.items():
            name = borehole if copy_index == 0 else f"{borehole}-c{copy_index}"
            dataset[name] = []
            for page in pages:
                page = json.loads(json.dumps(page))
                if copy_index:
                    page["page_id"] = f"{page['page_id']}-c{copy_index}"
                dataset[name].append(page)
    return dataset


def write_scaled_site(directory: str, factor: int) -> list:
    """Render the scaled dataset to disk and return a fetch manifest."""
    from corebench import mock_site
    manifest = []
    for borehole, pages in scaled_dataset(factor).items():
        sub = os.path.join(directory, borehole)
        os.makedirs(sub, exist_ok=True)
        for page in pages:
            path = os.path.join(sub, f"{page['page_id']}.html")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(mock_site.render_page(borehole, page))
            manifest.append({"page_id": page["page_id"], "url": path})
    return manifest
