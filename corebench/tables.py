"""Table grid reconstruction for dirty tables with merged / multi-row headers.

The extractor does not assume one row == one header.  ``build_table`` expands
``rowspan``/``colspan`` onto a rectangular grid and ``Table.header_columns``
joins the header layers above each column into a single label path, preserving
the hierarchy ("化学分析 / Au (mg/kg)").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .html_dom import Node


@dataclass
class Cell:
    text: str
    node: Node
    row: int
    col: int
    rowspan: int
    colspan: int
    is_header: bool

    @property
    def clean(self) -> str:
        return re.sub(r"\s+", " ", self.text).strip()


@dataclass
class Table:
    node: Node
    rows: List[List[Optional[Cell]]] = field(default_factory=list)
    header_layers: int = 1

    @property
    def ncols(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    def header_columns(self) -> List[List[str]]:
        """One label path per column, bottom layer last."""
        cols: List[List[str]] = [[] for _ in range(self.ncols)]
        for r in range(min(self.header_layers, len(self.rows))):
            for c in range(self.ncols):
                cell = self.rows[r][c] if c < len(self.rows[r]) else None
                if cell is not None and cell.clean:
                    cols[c].append(cell.clean)
        return cols

    def body_rows(self) -> List[List[Optional[Cell]]]:
        return self.rows[self.header_layers:]


def _row_cells(tr: Node, row_index: int) -> List[Cell]:
    cells: List[Cell] = []
    col = 0
    for child in tr.children:
        if child.tag not in ("td", "th"):
            continue
        rowspan = _safe_int(child.get("rowspan"), 1)
        colspan = _safe_int(child.get("colspan"), 1)
        text = child.normalize_text()
        cells.append(
            Cell(
                text=text,
                node=child,
                row=row_index,
                col=col,
                rowspan=max(rowspan, 1),
                colspan=max(colspan, 1),
                is_header=child.tag == "th",
            )
        )
        col += colspan
    return cells


def _safe_int(value: Optional[str], default: int) -> int:
    if value is None:
        return default
    match = re.search(r"\d+", value)
    return int(match.group()) if match else default


def build_table(table_node: Node, header_layers: int = 1) -> Table:
    """Expand merged cells onto a rectangular grid."""
    trs = table_node.find_all("tr")
    # occupied[(r, c)] remembers cells that span into position (r, c).
    occupied: dict = {}
    grid: List[List[Optional[Cell]]] = []
    for r, tr in enumerate(trs):
        row: List[Optional[Cell]] = []
        source_cells = _row_cells(tr, r)
        c = 0
        for cell in source_cells:
            while (r, c) in occupied:
                row.append(occupied[(r, c)])
                c += 1
            cell.col = c
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    occupied[(r + dr, c + dc)] = cell
            row.append(cell)
            for _ in range(cell.colspan - 1):
                c += 1
                row.append(cell)
            c += 1
        # Trailing cells that only exist because of rowspans above.
        while (r, c) in occupied:
            row.append(occupied[(r, c)])
            c += 1
        grid.append(row)
    table = Table(node=table_node, rows=grid)
    table.header_layers = min(header_layers, max(1, len(grid)))
    return table


def detect_header_layers(table_node: Node) -> int:
    """Count leading rows made up entirely (or mostly) of ``th`` cells."""
    trs = table_node.find_all("tr")
    layers = 0
    for tr in trs:
        cells = [c for c in tr.children if c.tag in ("td", "th")]
        if not cells:
            break
        if all(c.tag == "th" for c in cells):
            layers += 1
        else:
            break
    return max(1, layers)
