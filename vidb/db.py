"""Database access: connecting, loading rows, planning and applying changes."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import json

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.engine import URL, make_url

from .codec import ParsedDocument, ParseError, coerce, column_types, render


class VidbError(Exception):
    pass


# ------------------------------------------------------------ connecting

def normalize_uri(uri: str) -> str:
    """Accept a bare sqlite file path as well as a real SQLAlchemy URL."""
    if "://" in uri:
        return uri
    if uri == ":memory:":
        return "sqlite://"
    if os.path.exists(uri) or uri.endswith((".db", ".sqlite", ".sqlite3")):
        return "sqlite:///" + uri
    raise VidbError(
        f"{uri!r} is not a database URI (expected e.g. postgresql://user@host/db "
        "or a path to an SQLite file)")


def make_url_for(uri: str, user: Optional[str] = None,
                 password: Optional[str] = None) -> URL:
    """Turn the user's URI plus -u/-W/$VIDB_PASSWORD into a SQLAlchemy URL."""
    try:
        url: URL = make_url(normalize_uri(uri))
    except sa.exc.ArgumentError as e:
        raise VidbError(f"bad database URI: {e}")
    # "postgres://" is common but not accepted by SQLAlchemy 2.
    if url.drivername == "postgres":
        url = url.set(drivername="postgresql")
    # SQLite has no credentials, and SQLAlchemy raises if a URL for it carries
    # any - so a globally exported $VIDB_PASSWORD must not break sqlite use.
    if not url.drivername.startswith("sqlite"):
        if user is not None:
            url = url.set(username=user)
        if password is not None:
            url = url.set(password=password)
    return url


def make_engine(uri: str, user: Optional[str] = None,
                password: Optional[str] = None) -> sa.Engine:
    url = make_url_for(uri, user, password)
    try:
        return sa.create_engine(url)
    except (ImportError, sa.exc.NoSuchModuleError) as e:
        raise VidbError(
            f"cannot load a driver for {url.drivername!r}: {e}\n"
            "Install one, e.g.  pip install 'vidb[postgres]'  or  'vidb[mysql]'")


# --------------------------------------------------------------- loading

@dataclass
class Table:
    name: str
    schema: Optional[str] = None

    def clause(self, columns: Sequence[str], types: Optional[dict] = None):
        types = types or {}
        return sa.table(self.name, *[sa.column(c, types.get(c)) for c in columns],
                        schema=self.schema)

    def __str__(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


_IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)'
_QNAME = rf"{_IDENT}(?:\s*\.\s*{_IDENT})?"
_BARE_NAME = re.compile(rf"^\s*({_QNAME})\s*;?\s*$")
def _unquote(ident: str) -> str:
    ident = ident.strip()
    if ident[0] in '"`[':
        return ident[1:-1]
    return ident


def parse_table_name(qname: str) -> Table:
    """Parse "table" or "schema.table" (as accepted by --table)."""
    parts = [_unquote(p) for p in re.findall(_IDENT, qname)]
    if len(parts) == 1:
        return Table(parts[0])
    if len(parts) == 2:
        return Table(parts[1], parts[0])
    raise VidbError(
        f"{qname!r} has more than two parts; use \"table\" or \"schema.table\"")


def expand_query(query: str) -> str:
    """A bare table name means SELECT * FROM that table."""
    if re.match(r"^\s*select\b", query, re.I) is None and _BARE_NAME.match(query):
        return f"SELECT * FROM {_BARE_NAME.match(query).group(1)}"
    return query


def strip_sql_noise(query: str) -> str:
    """Blank out string literals and comments, best-effort, for keyword scanning."""
    q = re.sub(r"'(?:[^']|'')*'", "''", query)
    return re.sub(r"--[^\n]*|/\*.*?\*/", " ", q, flags=re.S)


def guess_table(query: str) -> Optional[Table]:
    """Find the table of a simple single-table SELECT; None if not obvious."""
    q = strip_sql_noise(query)
    if not re.match(r"\s*select\b", q, re.I):
        return None
    if re.search(r"\b(join|union|intersect|except|with)\b|\(\s*select\b", q, re.I):
        return None
    found = re.findall(rf"\bfrom\s+({_QNAME})(\s*,)?", q, re.I)
    if len(found) != 1 or found[0][1]:
        return None
    return parse_table_name(found[0][0])


def limit_without_order(query: str) -> bool:
    """True if the query limits rows without saying which rows (arbitrary subset).

    Best-effort: an ORDER BY inside a subquery counts, so this can under-warn,
    but it never warns about a query that has no LIMIT at all.
    """
    q = strip_sql_noise(query)
    limited = re.search(r"\blimit\b|\bfetch\s+(?:first|next)\b", q, re.I)
    ordered = re.search(r"\border\s+by\b", q, re.I)
    return bool(limited) and not ordered


def _take(cursor, limit: Optional[int]) -> list:
    """Read at most limit+1 rows (one extra to detect overflow), or all of them."""
    cap = None if limit is None else limit + 1
    out: list = []
    while cap is None or len(out) < cap:
        chunk = cursor.fetchmany(10000 if cap is None else cap - len(out))
        if not chunk:
            break
        out.extend(tuple(r) for r in chunk)
    return out


def fetch(conn: sa.Connection, query: str, max_rows: Optional[int] = None):
    """Run the caller's SELECT and read the rows.

    This goes through the DBAPI cursor rather than SQLAlchemy's statement layer
    because the query is raw text: some drivers treat a literal % (as in
    LIKE 'x%') as a placeholder marker when it passes through them, and the SQL
    must reach the server exactly as written.
    """
    cursor = conn.connection.cursor()
    try:
        cursor.execute(query)
        if cursor.description is None:
            raise VidbError("the query returned no result set; it must be a SELECT")
        columns = [d[0] for d in cursor.description]
        if len(set(columns)) != len(columns):
            raise VidbError("the query returns duplicate column names; alias them with AS")
        rows = _take(cursor, max_rows)
    finally:
        cursor.close()
    if max_rows is not None and len(rows) > max_rows:
        raise VidbError(
            f"the query returned more than {max_rows} rows; vidb is meant for small "
            "sets that fit in an editor. Narrow the query with WHERE, or raise the "
            "cap with --max-rows N (0 disables it)")
    return columns, rows


@dataclass
class Schema:
    """What the database knows about the table being written to."""

    pk: list = field(default_factory=list)
    types: dict = field(default_factory=dict)     # column name -> SQLAlchemy type

    @classmethod
    def reflect(cls, engine: sa.Engine, table: Table) -> "Schema":
        try:
            insp = sa.inspect(engine)
            cols = insp.get_columns(table.name, schema=table.schema)
            pk = insp.get_pk_constraint(table.name, schema=table.schema)
        except sa.exc.NoSuchTableError:
            raise VidbError(f"table {table} does not exist")
        return cls(list(pk.get("constrained_columns") or []),
                   {c["name"]: c["type"] for c in cols})

    def python_types(self, columns: Sequence[str]) -> list:
        """Python class per column, to convert edited text back to a value.

        This is what makes a column that is NULL in every loaded row usable:
        without it the only type information would be the (absent) loaded value.
        """
        out = []
        for name in columns:
            t = self.types.get(name)
            try:
                out.append(t.python_type if t is not None else None)
            except (NotImplementedError, AttributeError):
                out.append(None)
        return out

    def is_json(self, name: str) -> bool:
        return isinstance(self.types.get(name), sa.JSON)

    def normalize(self, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> list:
        """Make driver-specific value shapes uniform.

        psycopg hands back dicts for json/jsonb, while pymysql hands back the raw
        text.  Without this the text would be bound through the column's JSON type
        and stored as a JSON *string* rather than the document, so decode it here
        and let the type do the encoding on the way out.
        """
        jidx = [i for i, c in enumerate(columns) if self.is_json(c)]
        if not jidx:
            return list(rows)
        out = []
        for row in rows:
            row = list(row)
            for i in jidx:
                value = row[i]
                if isinstance(value, (str, bytes, bytearray)):
                    try:
                        row[i] = json.loads(value)
                    except (ValueError, TypeError):
                        pass          # not JSON after all; leave the text alone
            out.append(tuple(row))
        return out

    def comparable(self, name: str) -> bool:
        """Whether `column = value` can match a document that was loaded from it.

        JSON columns mostly cannot be pinned. PostgreSQL's json type has no
        equality operator at all, and MySQL compares a JSON column with a string
        operand by JSON *type*, so the document arrives as a string and never
        matches. PostgreSQL's jsonb does compare properly, so it stays pinned.
        """
        t = self.types.get(name)
        if isinstance(t, sa.JSON):
            return isinstance(t, pg.JSONB)
        return True


def check_key_columns(key: Sequence[str], schema: "Schema", dialect: str) -> None:
    """A key must be a column of the table: the WHERE clause compares columns,
    so a computed or aliased value from the query cannot identify a row."""
    pseudo = {"rowid", "oid", "_rowid_"} if dialect == "sqlite" else set()
    missing = [k for k in key if k not in schema.types and k not in pseudo]
    if missing:
        raise VidbError(
            f"key column(s) {', '.join(missing)} are not columns of the table, so "
            "they cannot be compared in the WHERE clause; a computed value or an "
            "alias cannot identify a row - key on a real column instead")


def check_key_unique(columns: Sequence[str], rows: Sequence[Sequence[Any]],
                     key: Sequence[str]) -> None:
    """Refuse keys that do not identify rows, before the user starts editing."""
    kidx = [columns.index(k) for k in key]
    seen: dict = {}
    for n, row in enumerate(rows, 1):
        kv = []
        for i in kidx:
            try:
                hash(row[i])
                kv.append(row[i])
            except TypeError:          # e.g. a list from a JSON column
                kv.append(repr(row[i]))
        kv = tuple(kv)
        if kv in seen:
            raise VidbError(
                f"the key columns ({', '.join(key)}) are not unique: rows "
                f"{seen[kv]} and {n} have the same values, so updates and deletes "
                "would be ambiguous. Add a unique column to the query"
                " choose a unique column of the table with --key")
        seen[kv] = n


# -------------------------------------------------------------- planning

@dataclass
class Change:
    kind: str                       # "update" | "delete" | "insert"
    number: Optional[int]           # row number in the file (None for inserts)
    where: dict = field(default_factory=dict)    # key column -> original value
    lock: dict = field(default_factory=dict)     # changed column -> original value
    values: dict = field(default_factory=dict)   # column -> new value
    before: dict = field(default_factory=dict)   # for display only


def plan_changes(columns: Sequence[str], rows: Sequence[Sequence[Any]],
                 doc: ParsedDocument, key: Sequence[str],
                 types: Optional[Sequence[Any]] = None,
                 no_lock: Sequence[str] = ()) -> list:
    """Compare the edited document with the loaded rows."""
    sample = column_types(columns, rows)
    types = [s or t for s, t in zip(sample, types or [None] * len(columns))]
    kidx = [columns.index(k) for k in key]
    changes = []

    for n, row in enumerate(rows, 1):
        where = {columns[i]: row[i] for i in kidx}
        if n not in doc.existing:
            changes.append(Change("delete", n, where=where,
                                  before=dict(zip(columns, map(render, row)))))
            continue
        lineno, texts = doc.existing[n]
        values, before = {}, {}
        for i, col in enumerate(columns):
            old = render(row[i])
            if texts[i] != old:
                try:
                    values[col] = coerce(texts[i], types[i])
                except ValueError as e:
                    raise ParseError(f"column {col!r}: {e}", lineno)
                before[col] = old
        if values:
            lock = {c: row[columns.index(c)] for c in values
                    if c not in where and c not in no_lock}
            changes.append(Change("update", n, where=where, lock=lock,
                                  values=values, before=before))

    for n in doc.existing:
        if n < 1 or n > len(rows):
            raise ParseError(f"row number {n} does not exist", doc.existing[n][0])

    for lineno, texts in doc.new:
        values = {}
        for i, col in enumerate(columns):
            if texts[i] is None:   # NULL -> omit so the column default applies
                continue
            try:
                values[col] = coerce(texts[i], types[i])
            except ValueError as e:
                raise ParseError(f"column {col!r}: {e}", lineno)
        changes.append(Change("insert", None, values=values))

    order = {"delete": 0, "update": 1, "insert": 2}
    changes.sort(key=lambda c: (order[c.kind], c.number or 0))
    return changes


def _where(tbl, where: dict):
    conds = [(tbl.c[k].is_(None) if v is None else tbl.c[k] == v) for k, v in where.items()]
    if not conds:
        raise VidbError("refusing to run a statement without a row filter")
    return sa.and_(*conds)


def apply_changes(engine: sa.Engine, table: Table, columns: Sequence[str],
                  changes: Sequence[Change], lock: bool = True,
                  types: Optional[dict] = None) -> None:
    """Apply everything in one transaction; any surprise rolls it all back."""
    tbl = table.clause(columns, types)
    with engine.begin() as conn:
        for ch in changes:
            # Changed columns are pinned to their loaded value (key wins on
            # collision), so a concurrent writer makes rowcount 0 instead of
            # silently losing their change.
            where = {**ch.lock, **ch.where} if ch.kind == "update" and lock else ch.where
            if ch.kind == "delete":
                stmt = sa.delete(tbl).where(_where(tbl, ch.where))
            elif ch.kind == "update":
                stmt = sa.update(tbl).where(_where(tbl, where)).values(ch.values)
            else:
                stmt = sa.insert(tbl).values(ch.values) if ch.values else sa.insert(tbl)
            try:
                res = conn.execute(stmt)
            except (sa.exc.DBAPIError, sa.exc.CompileError) as e:
                if ch.kind == "insert" and not ch.values:
                    # Not every database can say "insert a row, use every default".
                    # Rather than guess from capability flags, explain it when the
                    # statement is actually rejected, and never pre-emptively refuse.
                    raise VidbError(
                        f"{table} cannot insert a row that leaves every column to its "
                        "default: this database has no way to express that. Give at "
                        "least one column a value in the new row.\n"
                        f"({getattr(e, 'orig', None) or e})")
                raise
            if ch.kind != "insert" and res.rowcount != 1:
                raise VidbError(_mismatch(ch, res.rowcount, lock))


def _mismatch(ch: Change, count: int, lock: bool) -> str:
    if count > 1:
        return (f"{ch.kind} of row {ch.number} matched {count} rows: the --key "
                "columns are not unique; nothing was applied")
    if ch.kind == "update" and lock:
        return (f"row {ch.number} could not be updated: it was changed or deleted "
                "since the query ran (use --force to overwrite); nothing was applied")
    return (f"{ch.kind} of row {ch.number} matched no rows: it was changed or "
            "deleted since the query ran; nothing was applied")


def describe(ch: Change) -> str:
    def q(v):
        return "NULL" if v is None else repr(v)
    if ch.kind == "delete":
        return f"DELETE row {ch.number}: " + ", ".join(f"{k}={q(v)}" for k, v in ch.where.items())
    if ch.kind == "insert":
        if not ch.values:
            return "INSERT: no columns given, so server defaults apply to every column"
        return "INSERT: " + ", ".join(f"{k}={q(v)}" for k, v in ch.values.items())
    parts = ", ".join(f"{c}: {q(ch.before[c])} -> {q(render(v))}" for c, v in ch.values.items())
    return (f"UPDATE row {ch.number} "
            f"({', '.join(f'{k}={q(v)}' for k, v in ch.where.items())}): {parts}")
