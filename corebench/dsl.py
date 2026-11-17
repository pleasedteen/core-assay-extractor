"""Minimal declarative-rule text format (an indentation based YAML subset).

Supported syntax
----------------
* ``#`` line comments (outside quotes)
* ``key: value`` mappings; an empty value introduces a nested block
* ``- item`` lists; items may be scalars, ``- key: value`` mappings or
  nested blocks
* scalars: bare strings, quoted strings, true/false/null, ints, floats
* flow lists: ``[a, "b", 3]``

Only the Python standard library is used.  The format is intentionally small
so that rule authors can read and edit it without learning Python.
"""
from __future__ import annotations

import ast
from typing import Any, List, Tuple


class DslError(ValueError):
    pass


def _strip_comment(line: str) -> str:
    in_single = in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or line[i - 1] in " \t":
                return line[:i]
    return line


def _parse_flow_list(token: str) -> List[Any]:
    inner = token.strip()[1:-1]
    items: List[Any] = []
    buf = ""
    quote = None
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf += ch
        elif ch == ",":
            if buf.strip():
                items.append(_coerce(buf.strip()))
            buf = ""
        else:
            buf += ch
    if buf.strip():
        items.append(_coerce(buf.strip()))
    return items


def _coerce(token: str) -> Any:
    token = token.strip()
    if token == "":
        return None
    low = token.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "none", "~"):
        return None
    if token.startswith("["):
        if not token.endswith("]"):
            raise DslError(f"unterminated flow list: {token!r}")
        if token.strip() == "[]":
            return []
        return _parse_flow_list(token)
    if token == "{}":
        return {}
    if token[0] in ("'", '"'):
        try:
            return ast.literal_eval(token)
        except (ValueError, SyntaxError) as exc:
            raise DslError(f"cannot parse literal {token!r}: {exc}") from exc
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


def _tokenize(text: str) -> List[Tuple[int, str, int]]:
    rows: List[Tuple[int, str, int]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if "\t" in line[:indent]:
            raise DslError(f"line {lineno}: tabs are not allowed for indentation")
        rows.append((indent, line.strip(), lineno))
    return rows


def parse(text: str) -> Any:
    """Parse DSL text into Python dict/list/scalar structures."""
    rows = _tokenize(text)
    pos = 0

    def parse_block(min_indent: int) -> Any:
        nonlocal pos
        if pos >= len(rows):
            return None
        indent, content, lineno = rows[pos]
        if indent < min_indent:
            return None
        block_indent = indent
        if content.startswith("- "):
            result_list: List[Any] = []
            while pos < len(rows):
                indent, content, lineno = rows[pos]
                if indent < block_indent:
                    break
                if indent != block_indent or not content.startswith("- "):
                    break
                rest = content[2:].strip()
                if rest == "":
                    pos += 1
                    result_list.append(parse_block(block_indent + 1))
                    continue
                if ":" in rest and not rest.startswith(("[", "'", '"')):
                    key, _, val = rest.partition(":")
                    item: Any = {}
                    val = val.strip()
                    if val:
                        item[key.strip()] = _coerce(val)
                    pos += 1
                    child = parse_block(block_indent + 1)
                    if isinstance(child, dict):
                        item.update(child)
                    elif child is not None and val == "":
                        item[key.strip()] = child
                    result_list.append(item)
                else:
                    result_list.append(_coerce(rest))
                    pos += 1
            return result_list
        result_map: dict = {}
        while pos < len(rows):
            indent, content, lineno = rows[pos]
            if indent < block_indent:
                break
            if indent != block_indent or content.startswith("- "):
                break
            if ":" not in content:
                raise DslError(f"line {lineno}: expected 'key:' but got {content!r}")
            key, _, val = content.partition(":")
            key = key.strip()
            val = val.strip()
            pos += 1
            if val == "":
                child = parse_block(block_indent + 1)
                result_map[key] = child if child is not None else {}
            else:
                result_map[key] = _coerce(val)
        return result_map

    if not rows:
        return {}
    base = rows[0][0]
    return parse_block(base)


def _looks_numeric(text: str) -> bool:
    try:
        int(text)
        return True
    except ValueError:
        pass
    try:
        float(text)
        return True
    except ValueError:
        return False


def _dump_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or any(ch in text for ch in ":#[]'\"") or text.strip() != text:
        return repr(text)
    if text.lower() in ("true", "false", "null", "yes", "no", "none", "~"):
        return repr(text)
    if _looks_numeric(text):
        return repr(text)
    return text


def dump(data: Any, indent: int = 0) -> str:
    """Serialise Python structures back to DSL text."""
    pad = " " * indent
    lines: List[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                if value:
                    lines.append(f"{pad}{key}:")
                    lines.append(dump(value, indent + 2))
                else:
                    lines.append(f"{pad}{key}: " + "{}")
            elif isinstance(value, list):
                if value:
                    lines.append(f"{pad}{key}:")
                    lines.append(dump(value, indent + 2))
                else:
                    lines.append(f"{pad}{key}: " + "[]")
            else:
                lines.append(f"{pad}{key}: {_dump_scalar(value)}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                first = True
                for key, value in item.items():
                    prefix = f"{pad}- " if first else f"{pad}  "
                    first = False
                    if isinstance(value, (dict, list)):
                        lines.append(f"{prefix}{key}:")
                        lines.append(dump(value, indent + 4))
                    else:
                        lines.append(f"{prefix}{key}: {_dump_scalar(value)}")
            elif isinstance(item, list):
                lines.append(f"{pad}-")
                lines.append(dump(item, indent + 2))
            else:
                lines.append(f"{pad}- {_dump_scalar(item)}")
    else:
        lines.append(f"{pad}{_dump_scalar(data)}")
    return "\n".join(lines)
