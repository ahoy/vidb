"""Text format for rows: rendering, parsing, escaping and type coercion.

Each row is one line:  <number> TAB <field> TAB <field> ...
Inside a field, backslash, tab, newline and CR are escaped (\\\\, \\t, \\n, \\r)
and SQL NULL is written as \\N.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

NULL = r"\N"


class ParseError(ValueError):
    def __init__(self, message: str, line: Optional[int] = None):
        self.line = line
        super().__init__(f"line {line}: {message}" if line else message)


# --------------------------------------------------------------- escaping

_ESC = {"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"}
_UNESC = {"\\": "\\", "t": "\t", "n": "\n", "r": "\r"}


def escape(text: str) -> str:
    return "".join(_ESC.get(c, c) for c in text)


def unescape(text: str) -> Optional[str]:
    """Inverse of escape(); returns None for the NULL marker."""
    if text == NULL:
        return None
    if "\\" not in text:
        return text

    def sub(m: "re.Match[str]") -> str:
        ch = m.group(1)
        if ch not in _UNESC:
            raise ValueError(f"unknown escape sequence \\{ch}")
        return _UNESC[ch]

    if text.endswith("\\") and (len(text) - len(text.rstrip("\\"))) % 2 == 1:
        raise ValueError("dangling backslash")
    return re.sub(r"\\(.)", sub, text, flags=re.S)


# ------------------------------------------------------- value <-> text

def render(value: Any) -> Optional[str]:
    """Convert a database value to its (unescaped) text form; None is NULL."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    return str(value)


_TRUE = {"true", "t", "yes", "y", "1", "on"}
_FALSE = {"false", "f", "no", "n", "0", "off"}


def _to_bool(s: str) -> bool:
    low = s.strip().lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise ValueError(f"not a boolean: {s!r}")


_COERCERS = {
    bool: _to_bool,
    int: lambda s: int(s.strip()),
    float: lambda s: float(s.strip()),
    decimal.Decimal: lambda s: decimal.Decimal(s.strip()),
    dt.datetime: lambda s: dt.datetime.fromisoformat(s.strip()),
    dt.date: lambda s: dt.date.fromisoformat(s.strip()),
    dt.time: lambda s: dt.time.fromisoformat(s.strip()),
    uuid.UUID: lambda s: uuid.UUID(s.strip()),
    bytes: lambda s: bytes.fromhex(s.strip()),
    dict: json.loads,
    list: json.loads,
}


def coerce(text: Optional[str], sample_type: Optional[type]) -> Any:
    """Convert edited text back to a Python value of the column's type."""
    if text is None:
        return None
    fn = _COERCERS.get(sample_type) if sample_type else None
    if fn is None:
        for base, f in _COERCERS.items():  # subclasses, e.g. memoryview-ish
            if sample_type and issubclass(sample_type, base):
                fn = f
                break
    if fn is None:
        return text
    try:
        return fn(text)
    except (ValueError, decimal.InvalidOperation) as e:
        raise ValueError(f"cannot convert {text!r} to {sample_type.__name__}: {e}")


def column_types(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> list:
    """Per column, the type of the first non-NULL value (None if unknown)."""
    types: list = [None] * len(columns)
    for row in rows:
        for i, v in enumerate(row):
            if types[i] is None and v is not None:
                if isinstance(v, memoryview):
                    v = bytes(v)
                types[i] = type(v)
    return types


# ------------------------------------------------------------- the file

def number_width(count: int) -> int:
    return max(4, len(str(count)))


def format_document(columns: Sequence[str], rows: Sequence[Sequence[Any]],
                    header: Sequence[str] = ()) -> str:
    out = [f"# {h}" for h in header]
    out.append("# columns: " + "\t".join(escape(c) for c in columns))
    width = number_width(len(rows))
    for n, row in enumerate(rows, 1):
        fields = []
        for v in row:
            r = render(v)
            fields.append(NULL if r is None else escape(r))
        out.append("\t".join([str(n).zfill(width), *fields]))
    return "\n".join(out) + "\n"


@dataclass
class ParsedDocument:
    # row number -> list of field texts (None = NULL)
    existing: dict = field(default_factory=dict)
    # list of (line number, field texts) for rows to insert
    new: list = field(default_factory=list)


def parse_document(text: str, ncols: int,
                   originals: Optional[dict] = None) -> ParsedDocument:
    """Parse the edited file.

    `originals` maps row number -> rendered field texts of the rows as loaded.
    It is used to repair a single, very common damage: editors that strip
    trailing whitespace turn a line whose last column was the empty string
    (rendered as a trailing TAB) into a line that is one field short.  Only that
    exact case is repaired; anything else stays a loud error.
    """
    doc = ParsedDocument()
    for lineno, line in enumerate(text.split("\n"), 1):
        if line.endswith("\r"):
            line = line[:-1]
        if not line.strip(" ") or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == ncols and originals is not None and parts[0].strip().isdigit():
            orig = originals.get(int(parts[0]))
            if orig and orig[-1] == "":
                parts.append("")      # trailing empty field was stripped on save
        if len(parts) != ncols + 1:
            hint = ""
            if len(parts) == 1:
                hint = "; did the editor turn tabs into spaces?"
            elif len(parts) == ncols:
                hint = ("; a field is missing - editors that strip trailing "
                        "whitespace remove a TAB when the last column is empty")
            raise ParseError(
                f"expected {ncols + 1} tab-separated fields "
                f"(number + {ncols} columns), found {len(parts)}" + hint,
                lineno)
        try:
            values = [unescape(p) for p in parts[1:]]
        except ValueError as e:
            raise ParseError(str(e), lineno)
        num = parts[0].strip()
        if num == "":
            doc.new.append((lineno, values))
        elif num.isdigit():
            n = int(num)
            if n in doc.existing:
                raise ParseError(
                    f"row number {n} appears twice (clear the number to insert a new row)",
                    lineno)
            doc.existing[n] = (lineno, values)
        else:
            raise ParseError(f"bad row number {num!r}", lineno)
    return doc
