"""Integration tests against a real PostgreSQL server.

Run with:  VIDB_TEST_PG_URI=postgresql+psycopg://user:pass@host/db pytest
Each test gets its own uniquely named table, dropped afterwards, so the target
database can be a scratch one. The module is skipped without the variable.

Editors here rewrite whole fields (never substrings) because row values such as
'{"k": 1}' occur in several columns.
"""

import os
import sys
import textwrap
import uuid

import pytest
import sqlalchemy as sa

from vidb.cli import main

URI = os.environ.get("VIDB_TEST_PG_URI")
pytestmark = pytest.mark.skipif(not URI, reason="VIDB_TEST_PG_URI is not set")

# field index in the file = 1 + column position, since field 0 is the row number
ID, NAME, EMAIL, ACTIVE, SCORE, CREATED, AVATAR, TAGS, META, RAW, NOTE = range(1, 12)


def editor(tmp_path, body):
    """An 'editor' script that edits the row file vidb gives it.

    The database URI is taken from the environment so it is never written to a
    temporary file.
    """
    script = tmp_path / "ed_pg.py"
    script.write_text(
        "import os, sys\n"
        "uri = os.environ['VIDB_TEST_PG_URI']\n"
        "path = sys.argv[1]\n"
        "lines = open(path).read().split('\\n')\n"
        + textwrap.dedent(body)
        + "\nopen(path, 'w').write('\\n'.join(lines))\n")
    return f"{sys.executable} {script}"


def set_field(index, value, row="0001"):
    """Body snippet: set field `index` of row `row`, or of every row if row is None."""
    cond = "True" if row is None else f"fields[0] == {row!r}"
    return (f"for i, line in enumerate(lines):\n"
            f"    fields = line.split('\\t')\n"
            f"    if len(fields) > {index} and {cond}:\n"
            f"        fields[{index}] = {value!r}\n"
            f"        lines[i] = '\\t'.join(fields)\n")


class Pg:
    def __init__(self, table, uri):
        self.table = table
        self.engine = sa.create_engine(uri)

    def query(self, sql, **params):
        with self.engine.connect() as c:
            return c.execute(sa.text(sql), params).fetchall()

    def one(self, sql, **params):
        return self.query(sql, **params)[0][0]

    def execute(self, sql, **params):
        with self.engine.begin() as c:
            c.execute(sa.text(sql), params)


@pytest.fixture
def pg():
    name = "vidb_it_" + uuid.uuid4().hex[:12]
    engine = sa.create_engine(URI)
    with engine.begin() as c:
        c.exec_driver_sql(f"""
            create table {name}(
                id serial primary key, name text not null, email text,
                active boolean default true, score numeric(10,2),
                created timestamptz default now(), avatar bytea, tags text[],
                meta jsonb, raw json, note text default 'default-note')""")
        c.exec_driver_sql(f"""
            insert into {name}(name,email,active,score,created,avatar,tags,meta,raw,note)
            values ('ann','ann@example.com',true,12.50,'2024-01-02 03:04:05.678+00',
                    '\\xdeadbeef','{{a,b}}','{{"k": 1}}','{{"k": 1}}','line1'),
                   ('bob',NULL,false,NULL,NULL,NULL,NULL,NULL,NULL,NULL)""")
    yield Pg(name, URI)
    with sa.create_engine(URI).begin() as c:
        c.exec_driver_sql(f"drop table if exists {name} cascade")


def vidb(pg, ed, *flags, query=None):
    return main([*flags, "-d", URI,
                 "-c", query or f"select * from {pg.table}", "-e", ed])


def test_typed_values_round_trip(pg, tmp_path):
    ed = editor(tmp_path,
                set_field(SCORE, "99.99")
                + set_field(AVATAR, "00ff")
                + set_field(TAGS, '["c"]'))
    assert vidb(pg, ed) == 0
    score, avatar, tags = pg.query(
        f"select score, avatar, tags from {pg.table} where id = 1")[0]
    assert str(score) == "99.99" and bytes(avatar) == b"\x00\xff" and tags == ["c"]


def test_jsonb_survives_update_and_insert(pg, tmp_path):
    row = ['', '\\N', 'new'] + ['\\N'] * 6 + ['{"n": 7}', '\\N', '\\N']
    ed = editor(tmp_path,
                set_field(META, '{"k": 2}')
                + f"lines.append({chr(9).join(row)!r})\n")
    assert vidb(pg, ed) == 0
    assert pg.query(f"select meta from {pg.table} where id = 1")[0][0] == {"k": 2}
    assert pg.query(
        f"select meta from {pg.table} where name = 'new'")[0][0] == {"n": 7}


def test_postgres_json_column_is_not_pinned(pg, tmp_path):
    """`json` has no = operator, so it is left out of the UPDATE's WHERE clause."""
    ed = editor(tmp_path, set_field(3, '{"k": 3}'))
    assert main(["-d", URI, "-c",
                 f"select id, name, raw, note, meta from {pg.table} where id = 1",
                 "-e", ed]) == 0
    assert pg.query(f"select raw from {pg.table} where id = 1")[0][0] == {"k": 3}


def test_insert_without_values_uses_server_defaults(pg, tmp_path):
    row = ['', '\\N', 'dave'] + ['\\N'] * 9
    ed = editor(tmp_path, f"lines.append({chr(9).join(row)!r})\n")
    assert vidb(pg, ed) == 0
    active, note, created = pg.query(
        f"select active, note, created from {pg.table} where name = 'dave'")[0]
    assert active is True and note == "default-note" and created is not None


def test_null_in_every_row_still_uses_the_column_type(pg, tmp_path):
    """Only the reflected column type can type a value that is NULL everywhere."""
    ed = editor(tmp_path, set_field(3, "7.25"))
    assert main(["-d", URI, "-c",
                 f"select id, name, score, created from {pg.table} where name = 'bob'",
                 "-e", ed]) == 0
    assert str(pg.one(f"select score from {pg.table} where name = 'bob'")) == "7.25"


def test_literal_percent_in_the_query(pg, tmp_path):
    ed = editor(tmp_path, "pass\n")
    assert main(["-d", URI, "-c",
                 f"select * from {pg.table} where name like 'an%'", "-e", ed]) == 0


def test_concurrent_change_is_refused(pg, tmp_path):
    ed = editor(tmp_path, textwrap.dedent(f"""
        import sqlalchemy as sa
        with sa.create_engine(uri).begin() as c:
            c.execute(sa.text("update {pg.table} set name = 'THEIRS' where id = 1"))
    """) + set_field(NAME, "MINE"))
    with pytest.raises(SystemExit):
        main(["-d", URI, "-c", f"select * from {pg.table} where id = 1", "-e", ed])
    assert pg.one(f"select name from {pg.table} where id = 1") == "THEIRS"


def test_max_rows_cap(pg, tmp_path):
    pg.execute(f"insert into {pg.table}(name) values ('x'), ('y')")
    with pytest.raises(SystemExit):
        main(["--max-rows", "2", "-d", URI, "-c", f"select * from {pg.table}",
              "-e", "true"])
