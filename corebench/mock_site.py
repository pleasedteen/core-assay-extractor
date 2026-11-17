"""Simulated drill-archive site used by demos and the whole test suite.

Nothing here talks to a real external host.  The site serves deliberately dirty
HTML: unclosed ``td``/``tr`` tags, ``rowspan``/``colspan`` merged cells,
two-layer headers, units written only in headers, mixed metres/centimetres and
mg/kg / ug/kg, detection-limit values (``<0.05``), measured zeros and missing
cells.  A failure switch simulates an aborted page fetch mid-crawl.

Two layouts are provided so the second rule version can demonstrate purely
declarative adaptation (renamed labels, different classes/units) without any
Python change.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

# --------------------------------------------------------------------- data
# Each measurement tuple: (sample_id, analyte, method, value_text, unit)
DATASET: Dict[str, List[Dict[str, Any]]] = {
    "ZK01": [
        {
            "page_id": "zk01-p1",
            "title": "钻孔档案 ZK01 (1/3)",
            "intervals": [
                {"interval_id": "I-1", "from": "12.0", "to": "14.5", "rock": "花岗岩"},
            ],
            "samples": [
                {"sample_id": "S101", "interval_id": "I-1", "depth": "12.5", "lithology": "中粒花岗岩"},
                {"sample_id": "S102", "interval_id": "I-1", "depth": "1300", "lithology": "细粒花岗岩"},
            ],
            # Wide table: two-layer header, unit in header, one missing cell.
            "assays": [
                {"sample_id": "S101", "rows": [
                    ("Au", "火试金 FA", "<0.05", "mg/kg"),
                    ("Cu", "ICP-AES", "42.1", "mg/kg"),
                    ("Zn", "ICP-AES", "", "mg/kg"),
                ]},
                {"sample_id": "S102", "rows": [
                    ("Au", "火试金 FA", "0.008", "mg/kg"),
                    ("Cu", "ICP-AES", "35000", "ug/kg"),
                    ("Zn", "ICP-AES", "0", "mg/kg"),
                ]},
            ],
        },
        {
            "page_id": "zk01-p2",
            "title": "钻孔档案 ZK01 (2/3)",
            "intervals": [
                {"interval_id": "I-1", "from": "12.0", "to": "14.5", "rock": "花岗岩"},
            ],
            # S101 repeated across pages; only a complementary measurement here.
            "samples": [
                {"sample_id": "S101", "interval_id": "I-1", "depth": "12.5", "lithology": "中粒花岗岩"},
            ],
            "assays": [
                {"sample_id": "S101", "rows": [
                    ("Pb", "ICP-MS", "18.2", "mg/kg"),
                ]},
                # Deliberate conflict: same sample/analyte/method, different value.
                {"sample_id": "S102", "rows": [
                    ("Au", "火试金 FA", "0.012", "mg/kg"),
                    ("Zn", "ICP-AES", "0", "mg/kg"),
                ]},
            ],
        },
        {
            "page_id": "zk01-p3",
            "title": "钻孔档案 ZK01 (3/3)",
            "intervals": [
                {"interval_id": "I-2", "from": "14.5", "to": "18.0", "rock": "闪长岩"},
            ],
            "samples": [
                {"sample_id": "S103", "interval_id": "I-2", "depth": "1500", "lithology": "闪长岩"},
            ],
            # Tall table: one measurement per row, merged header cells, censored
            # value expressed in ug/kg and a missing reading.
            "assays_long": [
                ("S103", "Au", "石墨炉 GFAA", "<2", "ug/kg"),
                ("S103", "Ag", "ICP-MS", "0.31", "mg/kg"),
                ("S103", "Mo", "ICP-MS", "N/A", "mg/kg"),
            ],
        },
    ],
    "ZK02": [
        {
            "page_id": "zk02-p1",
            "title": "钻孔档案 ZK02 (1/1)",
            "intervals": [
                {"interval_id": "J-1", "from": "5.0", "to": "7.2", "rock": "玄武岩"},
            ],
            "samples": [
                {"sample_id": "T201", "interval_id": "J-1", "depth": "610", "lithology": "气孔玄武岩"},
            ],
            "assays": [
                {"sample_id": "T201", "rows": [
                    ("Au", "火试金 FA", "0.21", "mg/kg"),
                    ("Cu", "ICP-AES", "77.0", "mg/kg"),
                ]},
            ],
        },
    ],
}

# Layout B: renamed labels/classes, equivalent unit spellings, cm depths.
LAYOUT_B = {
    "borehole_label": "Hole ID",
    "interval_table_class": "interval-grid",
    "sample_table_class": "sample-grid",
    "assay_table_class": "assay-grid",
    "labels": {
        "interval_id": "Interval",
        "from": "From",
        "to": "To",
        "rock": "Rock type",
        "sample_id": "Sample",
        "depth": "Depth (cm)",
        "lithology": "Lithology",
    },
    "unit_style": {
        "mg/kg": "mg kg-1",
        "ug/kg": "μg kg-1",
    },
    "analyte_names": {  # display name -> canonical code
        "Gold": "Au",
        "Copper": "Cu",
        "Zinc": "Zn",
        "Lead": "Pb",
        "Silver": "Ag",
        "Molybdenum": "Mo",
    },
}


# ----------------------------------------------------------------- rendering
def _dirty_open(tag: str, attrs: str = "") -> str:
    # Intentionally missing some closing tags later; renderer keeps some <td>
    # unclosed to exercise the tolerant parser.
    return f"<{tag}{(' ' + attrs) if attrs else ''}>"


def render_interval_table(page: Dict[str, Any], layout_b: bool) -> str:
    if layout_b:
        lb = LAYOUT_B["labels"]
        head = (f"<thead><tr><th>{lb['interval_id']}</th><th>{lb['from']}</th>"
                f"<th>{lb['to']}</th><th>{lb['rock']}</th></tr></thead>")
        cls = LAYOUT_B["interval_table_class"]
    else:
        head = ("<thead><tr><th>区间编号</th><th>孔深起 (m)</th><th>孔深止 (m)</th>"
                "<th>岩性</th></tr></thead>")
        cls = "interval-table"
    lines = [f'<table class="{cls}">', head, "<tbody>"]
    for interval in page["intervals"]:
        # Missing </td> on the last cell: dirty markup on purpose.
        lines.append(
            "<tr>"
            f"<td>{interval['interval_id']}</td>"
            f"<td>{interval['from']}</td>"
            f"<td>{interval['to']}</td>"
            f"<td>{interval['rock']}"
            "</tr>"
        )
    lines.append("</tbody></table>")
    return "".join(lines)


def render_sample_table(page: Dict[str, Any], layout_b: bool) -> str:
    if layout_b:
        lb = LAYOUT_B["labels"]
        head = (f"<thead><tr><th>{lb['sample_id']}</th><th>{lb['interval_id']}</th>"
                f"<th>{lb['depth']}</th><th>{lb['lithology']}</th></tr></thead>")
        cls = LAYOUT_B["sample_table_class"]
    else:
        head = ("<thead><tr><th>样品编号</th><th>所属区间</th>"
                "<th>采样深度</th><th>岩性描述</th></tr></thead>")
        cls = "sample-table"
    lines = [f'<table class="{cls}">', head, "<tbody>"]
    for sample in page["samples"]:
        depth = sample["depth"]
        if layout_b:
            # Layout B declares centimetres in the header, so values are in cm.
            depth_html = str(round(float(depth) * 100, 1)) if float(depth) < 100 else depth
        elif float(depth) > 100:
            # Layout A stores the deep samples in centimetres with a cm unit hint.
            depth_html = f"{depth}<small>(cm)</small>"
        else:
            depth_html = depth
        lines.append(
            "<tr>"
            f"<td>{sample['sample_id']}</td>"
            f"<td>{sample['interval_id']}</td>"
            f"<td>{depth_html}</td>"
            f"<td>{sample['lithology']}</td>"
            "</tr>"
        )
    lines.append("</tbody></table>")
    return "".join(lines)


def _display_analyte(code: str, layout_b: bool) -> str:
    if not layout_b:
        return code
    for name, canonical in LAYOUT_B["analyte_names"].items():
        if canonical == code:
            return name
    return code


def _unit_text(unit: str, layout_b: bool) -> str:
    return LAYOUT_B["unit_style"].get(unit, unit) if layout_b else unit


def render_assay_table(page: Dict[str, Any], layout_b: bool) -> str:
    lines: List[str] = []
    if "assays" in page:
        lines.append(_render_wide_assay(page, layout_b))
    if "assays_long" in page:
        lines.append(_render_long_assay(page, layout_b))
    return "".join(lines)


def _render_wide_assay(page: Dict[str, Any], layout_b: bool) -> str:
    # Collect analyte/method columns in stable order; two-layer header with
    # colspan grouping, unit line embedded in the method row.
    columns = []
    for block in page["assays"]:
        for analyte, method, _value, unit in block["rows"]:
            label = _display_analyte(analyte, layout_b)
            signature = (label, method, unit)
            if signature not in columns:
                columns.append(signature)
    sample_label = LAYOUT_B["labels"]["sample_id"] if layout_b else "样品编号"
    cls = LAYOUT_B["assay_table_class"] if layout_b else "assay-table"
    out = [f'<table class="{cls}">', "<thead>"]
    # Header layer 1: analyte grouping with colspan.
    out.append("<tr>")
    out.append(f'<th rowspan="2">{sample_label}</th>')
    last_label = None
    span = 0
    for label, method, unit in columns:
        if label != last_label:
            if span:
                out.append(_dirty_open("th", f'colspan="{span}"') + last_label + "</th>")
            last_label, span = label, 1
        else:
            span += 1
    if span:
        out.append(_dirty_open("th", f'colspan="{span}"') + last_label + "</th>")
    out.append("</tr>")
    # Header layer 2: method + unit (unit is per column, not per analyte group).
    out.append("<tr>")
    for label, method, unit in columns:
        out.append(f"<th>{method} ({_unit_text(unit, layout_b)})</th>")
    out.append("</tr></thead><tbody>")
    for block in page["assays"]:
        value_map = {
            (a, m, u): v for a, m, v, u in block["rows"]
        }
        out.append("<tr>")
        out.append(f"<td>{block['sample_id']}</td>")
        for analyte, method, unit in columns:
            code = analyte
            if layout_b:
                code = LAYOUT_B["analyte_names"].get(analyte, analyte)
            value_text = value_map.get((code, method, unit), "")
            if value_text == "":
                out.append("<td></td>")  # explicit missing
            else:
                out.append(f"<td>{value_text}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _render_long_assay(page: Dict[str, Any], layout_b: bool) -> str:
    sample_label = LAYOUT_B["labels"]["sample_id"] if layout_b else "样品编号"
    cls = LAYOUT_B["assay_table_class"] if layout_b else "assay-table"
    # Merged header: "分析结果" spans analyte/method/value columns.
    out = [
        f'<table class="{cls}">',
        "<thead>",
        f'<tr><th rowspan="2">{sample_label}</th>'
        '<th colspan="3">分析结果</th></tr>',
        "<tr><th>元素</th><th>方法</th><th>结果</th></tr>",
        "</thead><tbody>",
    ]
    for sample_id, analyte, method, value, unit in page["assays_long"]:
        label = _display_analyte(analyte, layout_b)
        unit_text = _unit_text(unit, layout_b)
        out.append(
            "<tr>"
            f"<td>{sample_id}</td>"
            f"<td>{label}</td>"
            f"<td>{method}</td>"
            f"<td>{value}<small> ({unit_text})</small></td>"
            "</tr>"
        )
    out.append("</tbody></table>")
    return "".join(out)


def render_page(borehole: str, page: Dict[str, Any], layout_b: bool = False) -> str:
    if layout_b:
        title = f"{LAYOUT_B['borehole_label']}: {borehole} - {page['page_id']}"
        heading = f'<h2 class="hole-heading">{title}</h2>'
    else:
        heading = f"<h1>钻孔编号: {borehole}</h1>"
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<title>{page['title']}</title></head><body>",
        heading,
        f"<h3>{page['title']}</h3>",
        render_interval_table(page, layout_b),
        render_sample_table(page, layout_b),
        render_assay_table(page, layout_b),
        # Unclosed paragraph: more dirty markup.
        "<p class='footer-note'>模拟档案页面，仅用于离线验证",
        "</body></html>",
    ]
    return "".join(parts)


def render_index() -> str:
    items = []
    for borehole, pages in DATASET.items():
        for page in pages:
            items.append(f"<li><a href='/site/{borehole}/{page['page_id']}'>"
                         f"{borehole} / {page['page_id']}</a></li>")
    return ("<html><body><h1>模拟钻孔档案站点</h1><ul>" + "".join(items) + "</ul>"
            "<p>Layout B: <a href='/site-b/ZK01/zk01-p1'>/site-b/...</a></p></body></html>")


# ------------------------------------------------------------------ server
class _MockState:
    def __init__(self) -> None:
        self.fail_pages: set = set()
        self.lock = threading.Lock()

    def fail(self, page_ids: List[str]) -> None:
        with self.lock:
            self.fail_pages.update(page_ids)

    def reset(self) -> None:
        with self.lock:
            self.fail_pages.clear()

    def is_failing(self, page_id: str) -> bool:
        with self.lock:
            return page_id in self.fail_pages


STATE = _MockState()


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence test server logging
        pass

    def do_GET(self):  # noqa: N802 (stdlib API)
        path = self.path
        if path in ("/", "/index.html"):
            self._reply(200, render_index())
            return
        layout_b = path.startswith("/site-b/")
        prefix = "/site-b/" if layout_b else "/site/"
        if path.startswith(prefix):
            parts = path[len(prefix):].strip("/").split("/")
            if len(parts) != 2:
                self._reply(404, "not found")
                return
            borehole, page_id = parts
            if STATE.is_failing(page_id):
                self._reply(500, "simulated server failure")
                return
            for page in DATASET.get(borehole, []):
                if page["page_id"] == page_id:
                    self._reply(200, render_page(borehole, page, layout_b=layout_b))
                    return
            self._reply(404, "not found")
            return
        self._reply(404, "not found")

    def do_POST(self):  # noqa: N802
        if self.path == "/_control/fail":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            STATE.fail(payload.get("page_ids", []))
            self._reply(200, json.dumps({"ok": True}))
            return
        if self.path == "/_control/reset":
            STATE.reset()
            self._reply(200, json.dumps({"ok": True}))
            return
        self._reply(404, "not found")

    def _reply(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def start_mock_server(host: str = "127.0.0.1", port: int = 0):
    server = ThreadingHTTPServer((host, port), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://{host}:{server.server_address[1]}"


def write_static_site(directory: str, layout_b: bool = False) -> List[Dict[str, str]]:
    """Write the same pages to disk, returning a fetch manifest."""
    import os
    os.makedirs(directory, exist_ok=True)
    manifest = []
    for borehole, pages in DATASET.items():
        for page in pages:
            sub = os.path.join(directory, borehole)
            os.makedirs(sub, exist_ok=True)
            path = os.path.join(sub, f"{page['page_id']}.html")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(render_page(borehole, page, layout_b=layout_b))
            manifest.append({"page_id": page["page_id"], "url": path})
    return manifest
