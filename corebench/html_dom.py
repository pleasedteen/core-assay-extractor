"""Tolerant HTML parser used on dirty drill-archive pages.

The archive pages are not guaranteed to be well formed: closing tags may be
missing entirely.  This parser builds a small DOM while implicitly closing
open elements according to HTML-ish nesting rules (``tr`` ends when the next
``tr`` starts, ``td``/``th`` end at the next cell, block elements close each
other, and so on).  Script/style contents are kept verbatim.
"""
from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "col", "area", "base"}
RAW_TAGS = {"script", "style"}
# Opening one of these implicitly closes an open sibling of the same group.
CLOSE_GROUPS = {
    "p": {"p"},
    "li": {"li"},
    "tr": {"tr", "td", "th"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "thead": {"thead", "tbody", "tfoot"},
    "tbody": {"thead", "tbody", "tfoot"},
    "tfoot": {"thead", "tbody", "tfoot"},
    "option": {"option"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
}
# Starting an element with one of these tags forces ancestors in the value set
# to close (table section rules).
ANCESTOR_CLOSE = {
    "tr": {"td", "th"},
    "table": {"table", "thead", "tbody", "tfoot", "tr", "td", "th"},
    "thead": {"tr", "td", "th"},
    "tbody": {"tr", "td", "th"},
}

TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^<>]*?)?)\s*(/?)>", re.S)
ATTR_RE = re.compile(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*(=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'<>`]+))?")


@dataclass
class Node:
    tag: str
    attrs: Dict[str, str] = field(default_factory=dict)
    children: List["Node"] = field(default_factory=list)
    text: str = ""
    # No back-reference to the parent: the tree is acyclic, so a finished page
    # DOM is freed by plain reference counting the moment extraction ends.
    # Cyclic parent links would leave every page waiting for a generational GC
    # pass, making peak memory grow with the number of crawled pages.

    # -- navigation helpers -------------------------------------------------
    def iter_descendants(self, tag: Optional[str] = None):
        for child in self.children:
            if tag is None or child.tag == tag:
                yield child
            yield from child.iter_descendants(tag)

    def find_all(self, tag: str) -> List["Node"]:
        return list(self.iter_descendants(tag))

    def find(self, tag: str) -> Optional["Node"]:
        for found in self.iter_descendants(tag):
            return found
        return None

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.attrs.get(name, default)

    def text_content(self) -> str:
        parts = []
        if self.text:
            parts.append(self.text)
        for child in self.children:
            if child.tag in RAW_TAGS:
                continue
            parts.append(child.text_content())
        return "".join(parts)

    def normalize_text(self) -> str:
        return re.sub(r"\s+", " ", self.text_content()).strip()

    def outer_html(self) -> str:
        if self.tag == "#root":
            return "".join(child.outer_html() for child in self.children)
        attrs = "".join(f' {k}="{html_mod.escape(v, quote=True)}"' for k, v in self.attrs.items())
        if self.tag in VOID_TAGS:
            return f"<{self.tag}{attrs}>"
        inner = html_mod.escape(self.text, quote=False) if self.text else ""
        inner += "".join(child.outer_html() for child in self.children)
        return f"<{self.tag}{attrs}>{inner}</{self.tag}>"


def _parse_attrs(raw: str) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for match in ATTR_RE.finditer(raw):
        name = match.group(1).lower()
        value = match.group(3)
        if value is None:
            attrs[name] = ""
        else:
            if value[:1] in ("'", '"'):
                value = value[1:-1]
            attrs[name] = html_mod.unescape(value)
    return attrs


def parse_html(markup: str) -> Node:
    """Parse possibly-broken HTML into a small DOM tree."""
    root = Node(tag="#root")
    stack: List[Node] = [root]
    pos = 0

    def close_until(tag: str) -> None:
        # Pop implicit-close ancestors dictated by table nesting rules.
        forced = ANCESTOR_CLOSE.get(tag, set())
        while len(stack) > 1:
            top = stack[-1].tag
            if top in forced:
                stack.pop()
                continue
            break
        # Pop implicit sibling groups (tr/tr, td/td, p/p ...).
        group = CLOSE_GROUPS.get(tag, set())
        while len(stack) > 1 and stack[-1].tag in group:
            stack.pop()

    for match in TAG_RE.finditer(markup):
        if match.start() > pos:
            text = markup[pos:match.start()]
            if text:
                stack[-1].children.append(Node(tag="#text", text=html_mod.unescape(text)))
        is_close = match.group(1) == "/"
        tag = match.group(2).lower()
        attr_text = match.group(3) or ""
        self_closing = match.group(4) == "/"

        if is_close:
            # Find nearest matching open tag; tolerate missing/mismatched closes.
            for depth in range(len(stack) - 1, 0, -1):
                if stack[depth].tag == tag:
                    del stack[depth:]
                    break
            continue

        if tag in RAW_TAGS:
            end = re.search(rf"</\s*{tag}\s*>", markup[match.end():], re.IGNORECASE)
            raw = markup[match.end():match.end() + end.start()] if end else markup[match.end():]
            node = Node(tag=tag, attrs=_parse_attrs(attr_text), text=raw)
            stack[-1].children.append(node)
            if not end:
                # Unclosed raw element: consume the rest of the document.
                pos = len(markup)
                break
            pos = match.end() + end.end()
            continue

        close_until(tag)
        node = Node(tag=tag, attrs=_parse_attrs(attr_text))
        stack[-1].children.append(node)
        if tag not in VOID_TAGS and not self_closing:
            stack.append(node)
        pos = match.end()

    if pos < len(markup):
        root.children.append(Node(tag="#text", text=html_mod.unescape(markup[pos:])))
    return root
