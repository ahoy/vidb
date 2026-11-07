"""Command line interface for vidb."""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
import subprocess
import sys
import tempfile
from typing import Optional, Sequence

from . import __version__
from .codec import ParseError, format_document, parse_document, render
from .db import (Schema, Table, VidbError, apply_changes, check_key_columns,
                 check_key_unique, describe,
                 expand_query, fetch, guess_table, limit_without_order, make_engine,
                 parse_table_name, plan_changes)

DESCRIPTION = """\
Edit the rows returned by a SELECT query in your text editor ($VISUAL / $EDITOR),
like vidir does for file names. When the editor exits, vidb applies the
differences in a single transaction:

  * change a line          -> UPDATE of the changed columns of that row
  * delete a line          -> DELETE of that row
  * add a line with an empty row number (a leading TAB) -> INSERT
    (columns written as \\N are omitted, so their defaults apply)

Each line is: <row number> TAB <column> TAB <column> ...
Inside a column, \\\\ \\t \\n \\r stand for backslash, tab, newline and carriage
return, and \\N stands for NULL. Lines starting with # and blank lines are ignored.
Don't change the row numbers of lines you want to keep.
"""

EPILOG = """\
examples:
  vidb app.db 'select * from users where active = 0'
  vidb postgresql://alice@db.example.com/shop 'select id, name, price from items' -W
  vidb mysql+pymysql://db/shop items          # bare table name = SELECT *
  vidb -d app.db -f fix_bad_emails.sql        # keep the query in a file
  vidb -d app.db -f - < query.sql             # or on standard input
  VIDB_PASSWORD=secret vidb -u bob postgresql://db/shop 'select * from t' -k id,rev
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vidb", description=DESCRIPTION, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("args", nargs="*", metavar="[URI] [QUERY]",
                   help="database URI (SQLAlchemy style, e.g. postgresql://host/db, "
                        "mysql+pymysql://host/db, sqlite:///file.db, or a plain SQLite "
                        "file path) and the SELECT query (or a table name). Either may "
                        "be omitted when given with -d, -c/-f or $VIDB_URI.")
    p.add_argument("-d", "--database", metavar="URI",
                   help="connection URI (instead of the first positional argument; "
                        "default: $VIDB_URI)")
    p.add_argument("-c", "--query", metavar="SQL",
                   help="the SELECT query (or table name), instead of a positional "
                        "argument")
    p.add_argument("-f", "--from-file", metavar="FILE",
                   help="read the query from FILE ('-' for standard input)")
    p.add_argument("--max-rows", metavar="N", type=int, default=10000,
                   help="refuse to open the editor when the query returns more than "
                        "N rows (default: 10000; 0 disables the cap)")
    p.add_argument("--clean", action="store_true",
                   help=f"delete the edit files kept from earlier failed runs in "
                        f"{tempfile.gettempdir()}/vidb-edits and exit")
    p.add_argument("-u", "--user", help="database user (overrides the one in the URI)")
    p.add_argument("-W", "--ask-password", action="store_true",
                   help="prompt for the database password (or put it in the URI, "
                        "or set $VIDB_PASSWORD)")
    p.add_argument("-t", "--table",
                   help="table to modify (default: detected from a simple SELECT)")
    p.add_argument("-k", "--key",
                   help="comma-separated columns identifying a row "
                        "(default: the table's primary key)")
    p.add_argument("-e", "--editor", help="editor command (default: $VISUAL, $EDITOR, vi)")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="show what would change, but change nothing")
    p.add_argument("-i", "--confirm", action="store_true",
                   help="show the changes and ask before applying them")
    p.add_argument("--force", action="store_true",
                   help="overwrite rows even if they changed since the query ran "
                        "(by default such updates are refused)")
    p.add_argument("-y", "--yes", action="store_true",
                   help="don't ask before deleting every row")
    p.add_argument("-V", "--version", action="version", version=f"vidb {__version__}")
    return p


def die(msg: str, code: int = 1):
    print(f"vidb: {msg}", file=sys.stderr)
    raise SystemExit(code)


def ask(question: str, default: bool = False) -> bool:
    if not sys.stdin.isatty():
        return False
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(question + suffix).strip().lower()
    except EOFError:
        return False
    return default if not ans else ans.startswith("y")


def run_editor(path: str, editor: Optional[str]) -> None:
    cmd = editor or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    try:
        rc = subprocess.call(shlex.split(cmd) + [path])
    except FileNotFoundError:
        raise VidbError(f"editor not found: {cmd}")
    if rc != 0:
        raise VidbError(f"editor exited with status {rc}; nothing was changed")


def check_platform(os_name: str = os.name) -> None:
    """Editing works anywhere, but launching an editor and quoting its command
    are POSIX concerns; say so instead of failing in some odd way."""
    if os_name != "posix":
        raise VidbError("vidb runs on POSIX systems (Linux, macOS); this platform "
                        "is not supported")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ns = build_parser().parse_args(argv)
    if ns.clean:
        print(f"vidb: removed {clean_edit_files()} kept edit file(s)", file=sys.stderr)
        return 0
    try:
        check_platform()
        return run(ns)
    except VidbError as e:
        die(str(e))
    except KeyboardInterrupt:
        die("interrupted", 130)


EDIT_DIR = os.path.join(tempfile.gettempdir(), "vidb-edits")


def write_edit_file(text: str) -> str:
    """Write the rows to a new file only the current user can read.

    The file is kept when a run fails, so it must not be world-readable, and it
    lives in one private directory so that it can be found (and removed) later.
    """
    os.makedirs(EDIT_DIR, mode=0o700, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="vidb-", suffix=".vidb", dir=EDIT_DIR)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def clean_edit_files() -> int:
    """Remove edit files kept by earlier failed runs."""
    if not os.path.isdir(EDIT_DIR):
        return 0
    removed = 0
    for name in os.listdir(EDIT_DIR):
        path = os.path.join(EDIT_DIR, name)
        if name.startswith("vidb-") and name.endswith(".vidb") and os.path.isfile(path):
            os.unlink(path)
            removed += 1
    return removed


def keep_message(path: str, why: str) -> str:
    return (f"{why}\nyour edits are kept in {path} "
            f"(delete it when you are done, or run vidb --clean)")


def read_query(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise VidbError(f"cannot read query file: {e}")


def resolve_inputs(ns: argparse.Namespace) -> tuple[str, str]:
    """Work out which URI and which query to use (flags win over positionals)."""
    pos = list(ns.args)
    if ns.query is not None and ns.from_file is not None:
        raise VidbError("-c/--query and -f/--from-file are mutually exclusive")

    uri = ns.database or os.environ.get("VIDB_URI")

    if ns.query is not None:
        query = ns.query
    elif ns.from_file is not None:
        query = read_query(ns.from_file).strip()
        if not query:
            raise VidbError(f"the query file {ns.from_file!r} is empty")
    else:
        query = None

    if query is None:                       # no -c/-f: read the positionals
        if len(pos) == 2:
            if ns.database is not None:
                raise VidbError("the URI was given both with -d and positionally")
            uri, query = pos
        elif len(pos) == 1:
            query = pos[0]
        elif not pos:
            raise VidbError("no query given: pass it as an argument, or use "
                            "-c/--query or -f/--from-file")
        else:
            raise VidbError("too many arguments; see vidb --help")
    else:
        if len(pos) > 1:
            raise VidbError("only the URI may be given positionally when the query "
                            "comes from -c/-f")
        if pos:
            if uri is not None:
                raise VidbError("the URI given positionally conflicts with -d/--database "
                                "or $VIDB_URI")
            uri = pos[0]

    if not uri:
        raise VidbError("no database URI given (use -d, a positional URI, or $VIDB_URI)")
    return uri, query


def run(ns: argparse.Namespace) -> int:
    uri, query = resolve_inputs(ns)

    password = None
    if ns.ask_password:
        password = getpass.getpass("Database password: ")
    else:
        password = os.environ.get("VIDB_PASSWORD")

    query = expand_query(query)
    if limit_without_order(query):
        print("vidb: warning: the query uses LIMIT without ORDER BY, so the server "
              "may return a different set of rows each time; add ORDER BY if you "
              "expect a stable set", file=sys.stderr)
    engine = make_engine(uri, ns.user, password)
    max_rows = ns.max_rows or None

    try:
        with engine.connect() as conn:
            columns, rows = fetch(conn, query, max_rows)
        table: Optional[Table] = parse_table_name(ns.table) if ns.table else guess_table(query)
        if table is None:
            raise VidbError("cannot tell which table to modify; use --table")
        schema = Schema.reflect(engine, table)
        rows = schema.normalize(columns, rows)
        key = [k.strip() for k in ns.key.split(",")] if ns.key else schema.pk
    except VidbError:
        raise
    except Exception as e:  # driver / SQL errors
        raise VidbError(str(getattr(e, "orig", None) or e))

    if not key:
        print(f"vidb: warning: {table} has no primary key; rows are matched on all "
              "selected columns (use --key to choose)", file=sys.stderr)
        key = list(columns)
    missing = [k for k in key if k not in columns]
    if missing:
        raise VidbError(
            f"key column(s) {', '.join(missing)} are not in the query's output; "
            "select them (or choose another --key)")
    check_key_columns(key, schema, engine.dialect.name)
    check_key_unique(columns, rows, key)

    header = [f"vidb: table={table} key={','.join(key)} rows={len(rows)}",
              "row = <number> TAB columns...; delete a line to delete the row; "
              "empty number = new row; \\N = NULL"]
    text = format_document(columns, rows, header)

    path = write_edit_file(text)

    try:
        changes = edit_until_valid(path, text, columns, rows, key, ns.editor,
                                   schema.python_types(columns),
                                   [c for c in columns if not schema.comparable(c)])
        if not changes:
            print("vidb: no changes", file=sys.stderr)
            os.unlink(path)
            return 0

        for ch in changes:
            print(describe(ch))
        counts = {k: sum(c.kind == k for c in changes) for k in ("update", "delete", "insert")}
        summary = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in counts.items() if n)
        if ns.dry_run:
            print(f"vidb: dry run, would apply: {summary}", file=sys.stderr)
            os.unlink(path)
            return 0
        if rows and counts["delete"] == len(rows) and not ns.yes \
                and not ask(f"This deletes ALL {len(rows)} rows. Continue?"):
            raise VidbError(keep_message(path, "aborted: that would delete every row"))
        if ns.confirm and not ask(f"Apply {summary}?", default=True):
            raise VidbError(keep_message(path, "aborted at the confirmation prompt"))

        try:
            apply_changes(engine, table, columns, changes, lock=not ns.force,
                          types=schema.types)
        except VidbError as e:
            raise VidbError(keep_message(path, str(e)))
        except Exception as e:
            raise VidbError(keep_message(path, 
                f"{getattr(e, 'orig', None) or e}\nnothing was applied"))
        os.unlink(path)
        print(f"vidb: applied {summary}", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print(f"vidb: interrupted; your edits are kept in {path}", file=sys.stderr)
        raise
    except ParseError as e:
        raise VidbError(keep_message(path, str(e)))


def edit_until_valid(path, original, columns, rows, key, editor,
                     types=None, no_lock=()):
    """Run the editor; on a malformed file offer to re-edit it."""
    originals = {n: [render(v) for v in row] for n, row in enumerate(rows, 1)}
    while True:
        run_editor(path, editor)
        with open(path, encoding="utf-8", newline="") as f:
            edited = f.read()
        if edited == original:
            return []
        try:
            doc = parse_document(edited, len(columns), originals)
            return plan_changes(columns, rows, doc, key, types, no_lock)
        except ParseError as e:
            if not sys.stdin.isatty():
                raise
            print(f"vidb: {e}", file=sys.stderr)
            if not ask("Edit again?", default=True):
                raise VidbError(keep_message(path, "aborted while fixing the edit file"))


if __name__ == "__main__":
    sys.exit(main())
