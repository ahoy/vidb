# vidb

Edit database rows in your text editor, like [`vidir`](https://joeyh.name/code/moreutils/)
does for file names. `vidb` runs a `SELECT`, opens the rows in `$VISUAL`/`$EDITOR`,
and when you exit applies the differences to the database in **one transaction**.

```
$ vidb app.db 'select * from users where active = 0'
```
```
# vidb: table=users key=id rows=3
# columns: id	name	email
0001	1	ann	ann@example.com
0002	2	bob	\N
0003	3	cy	cy@example.com
```

| In the editor                                          | Result                                  |
|--------------------------------------------------------|-----------------------------------------|
| edit a line                                            | `UPDATE` of just the changed columns    |
| delete a line                                          | `DELETE` of that row                    |
| add a line with an **empty** row number (leading TAB)  | `INSERT` (`\N` columns are omitted, so defaults / auto-increment apply) |
| reorder lines, edit `#` comments or blank lines        | nothing                                 |

Line format: `<row number> TAB <col> TAB <col> ...`. Keep the row numbers of rows
you keep – they identify the rows. In a column, `\\`, `\t`, `\n`, `\r` mean
backslash, tab, newline, carriage return; `\N` means `NULL`.
Make sure your editor does not expand tabs to spaces for this file.

## Install

```
pip install .                    # SQLite works out of the box
pip install '.[postgres]'        # psycopg 3
pip install '.[mysql]'           # PyMySQL
```

## Platform

Linux and macOS. vidb runs an external editor and quotes its command with POSIX
rules, and keeps its working files under a `TMPDIR` with POSIX permissions;
Windows is not supported, and says so rather than half-working.

## Usage

```
vidb [options] [URI] [QUERY]
```

* **URI** – any SQLAlchemy URL (`postgresql://host/db`, `mysql+pymysql://host/db`,
  `sqlite:///file.db`, …), or a plain SQLite file path. Omit it to use `$VIDB_URI`.
* **QUERY** – a `SELECT`, or just a table name (= `SELECT * FROM name`). Getting a
  long query through the shell is awkward, so it can also come from a file or stdin:
  `-c/--query SQL`, `-f/--from-file FILE` (`-` = stdin). A file can hold a
  multi-line query with `--` comments, and can be reviewed and version-controlled.
* `-d/--database URI` – the connection URI as a flag instead of a positional. If
  the query comes from `-c` or `-f`, the URI is the only positional; if both are
  positional, the URI comes first and the query second.
* **Authentication** – put it in the URI, or use `-u USER`, `-W` (prompt for the
  password), or `$VIDB_PASSWORD`. There is deliberately no `--password` flag: a
  password given on the command line is visible in `ps` to other users, and the
  other three routes cover every case. Driver mechanisms such as
  `~/.pgpass`/`PGPASSWORD` keep working.
* `-t TABLE` – table to modify. Auto-detected for simple single-table selects;
  needed for joins, views, etc.
* `-k COL[,COL]` – columns identifying a row. Defaults to the table's primary key.
  They must be present in the query's output. If the table has no primary key, all
  selected columns are matched (and a warning is printed). The chosen key must be
  unique in the result – vidb checks this before opening the editor.
* `-n/--dry-run` print the changes only, `-i/--confirm` ask before applying,
  `--force` overwrite rows changed since the query ran (see Safety), `-y/--yes` skip
  the "delete all rows?" guard, `-e/--editor CMD` override.
* `--max-rows N` refuse to open the editor when the query returns more than `N`
  rows (default 10000, `0` disables the cap). Editing is for sets that fit in an
  editor, not for whole tables.
* `--clean` delete the edit files kept from earlier failed runs, then exit.

## Safety

* Everything is applied in a single transaction. Each `UPDATE`/`DELETE` must touch
  exactly one row, otherwise everything is rolled back.
* **No lost updates.** An `UPDATE` also matches the original values of the columns
  you changed (`... WHERE id = 1 AND name = 'ann'`). If someone else changed the row
  while your editor was open, no row matches, the transaction is rolled back and you
  get an error instead of silently overwriting their work. `--force` turns this
  check off. A column whose type the server cannot compare with `=` (PostgreSQL
  `json`, as opposed to `jsonb`) is simply left out of that check.
* **Deletes match on the key only**, so they are not protected against a concurrent
  change of that row's contents.
* A `LIMIT` without an `ORDER BY` means "some rows", not "the first rows": vidb warns
  about it, since the set you see may differ from run to run.
* A key that is not unique is refused *before* the editor opens, since updates and
  deletes would be ambiguous; select a unique column and use `--key`.
* A key must be a real column of that table, because the `WHERE` clause compares
  columns: an expression alias, and PostgreSQL's `ctid`, cannot be used. A table
  with no unique column at all cannot be edited with vidb.
* A new row that gives a value for no column at all relies on the database
  supporting `DEFAULT VALUES`. Where it does not, vidb says so and changes
  nothing, instead of passing on a syntax error from the server.
* A malformed file (wrong field count, bad number/date, unknown row number…) is
  reported before touching the database; interactive sessions can re-edit.
* **Edited rows on disk.** The rows are written to a temporary file. On any
  failure that file is kept (and its path printed) rather than losing your edits;
  on success it is deleted. Kept files live in `$TMPDIR/vidb-edits`, a directory
  only you can enter, and `vidb --clean` removes them.
* Editors that strip trailing whitespace on save delete the TAB that represents an
  empty **last** column. That single case is repaired automatically; any other
  missing field is an error. If your editor expands tabs to spaces, tell it not to
  for `*.vidb` files.
* Deleting every row requires confirmation (or `-y`).
* Only columns whose text changed are written. Edited text is converted back
  using the Python type of the value that was loaded, or of the column itself when
  every loaded value is `NULL` (int, float, Decimal, bool, date/time, UUID, JSON,
  arrays, bytes as hex). Things are sent as text only when neither source gives a
  type, which is the case for a computed column in the query.

## Tests

```
pip install -e '.[test]' && pytest
```

SQLite is covered with no setup. The PostgreSQL, MySQL and MariaDB suites are
skipped unless you point them at a scratch database; each test creates and drops
its own uniquely named table, so the database only needs to be one you can throw
away. `compose.yaml` starts all three on loopback:

```
docker compose up -d --wait
export VIDB_TEST_PG_URI='postgresql+psycopg://vidb:throwaway@127.0.0.1:55432/scratch'
export VIDB_TEST_MYSQL_URI='mysql+pymysql://vidb:throwaway@127.0.0.1:33061/scratch'
export VIDB_TEST_MARIADB_URI='mysql+pymysql://vidb:throwaway@127.0.0.1:33062/scratch'
pytest
docker compose down -v
```

Leaving a variable unset just skips that suite, so `VIDB_TEST_MYSQL_URI=... pytest`
runs SQLite plus MySQL alone. MariaDB is not redundant with MySQL: it implements
`json` as `longtext`, so the same column takes a different shape in each.

CI (`.github/workflows/tests.yml`) runs SQLite plus all three servers on Python
3.11 and 3.14, the floor and the newest release claimed in `pyproject.toml`.

## Contributing

`make lint` and `make test` must pass with the virtualenv active (or pass it
explicitly, `make PYTHON=.venv/bin/python test`); `make services-down` cleans up. Commit
messages follow [Conventional Commits](https://www.conventionalcommits.org/)
(`fix(scope): ...`, `feat(scope): ...`).
