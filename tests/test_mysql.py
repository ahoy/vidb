"""Integration tests against a real MySQL-compatible server (MySQL or MariaDB).

Run with either or both of:
  VIDB_TEST_MYSQL_URI=mysql+pymysql://user:pass@host:port/db pytest
  VIDB_TEST_MARIADB_URI=mysql+pymysql://user:pass@host:port/db pytest

Each test gets its own uniquely named table, dropped afterwards, so the target
database can be a scratch one. Every test runs once per configured server; with
neither variable set the whole module skips.

Both are worth running: MariaDB implements `json` as `longtext`, so the same
column takes a different shape in each server.
"""

import os
import sys
import textwrap
import uuid

import pytest
import sqlalchemy as sa

from vidb.cli import main

FLAVOURS = ["mysql", "mariadb"]
URIS = {f: os.environ.get(f"VIDB_TEST_{f.upper()}_URI") for f in FLAVOURS}

# field index in the file = 1 + column position, since field 0 is the row number
ID, NAME, EMAIL, ACTIVE, SCORE, CREATED, AVATAR, PAYLOAD, NOTE = range(1, 10)


def editor(tmp_path, body, flavour):
    """An edit script; it picks its connection URI out of the environment by
    flavour, so no credentials are written to a temporary file."""
    script = tmp_path / f"edit_{flavour}.py"
    script.write_text(
        "import os, sys\n"
        "flavour = sys.argv[1].upper()\n"
        "uri = os.environ[f'VIDB_TEST_{flavour}_URI']\n"
        "path = sys.argv[2]\n"
        "lines = open(path).read().split('\\n')\n"
        + textwrap.dedent(body)
        + "\nopen(path, 'w').write('\\n'.join(lines))\n")
    return f"{sys.executable} {script} {flavour}"


def set_field(index, value, row="0001"):
    cond = "True" if row is None else f"fields[0] == {row!r}"
    return (f"for i, line in enumerate(lines):\n"
            f"    fields = line.split('\\t')\n"
            f"    if len(fields) > {index} and {cond}:\n"
            f"        fields[{index}] = {value!r}\n"
            f"        lines[i] = '\\t'.join(fields)\n")


class Db:
    def __init__(self, table, uri, flavour):
        self.table = table
        self.uri = uri
        self.flavour = flavour
        self.engine = sa.create_engine(uri)

    def query(self, sql, **params):
        with self.engine.connect() as c:
            return c.execute(sa.text(sql), params).fetchall()

    def one(self, sql, **params):
        return self.query(sql, **params)[0][0]

    def execute(self, sql, **params):
        with self.engine.begin() as c:
            c.execute(sa.text(sql), params)


@pytest.fixture(params=FLAVOURS)
def my(request):
    flavour = request.param
    uri = URIS[flavour]
    if not uri:
        pytest.skip(f"VIDB_TEST_{flavour.upper()}_URI is not set")
    name = "vidb_it_" + uuid.uuid4().hex[:12]
    engine = sa.create_engine(uri)
    with engine.begin() as c:
        c.exec_driver_sql(f"""
            create table {name}(
                id int auto_increment primary key, name varchar(50) not null,
                email varchar(100) null, active tinyint(1) default 1,
                score decimal(10,2), created datetime(6) default current_timestamp(6),
                avatar blob, payload json, note varchar(50) default 'default-note')""")
        c.exec_driver_sql(f"""
            insert into {name}(name,email,active,score,created,avatar,payload,note)
            values ('ann','ann@example.com',1,12.50,'2024-01-02 03:04:05.678',
                    0xdeadbeef,'{{"k": 1}}','line1'),
                   ('bob',NULL,0,NULL,NULL,NULL,NULL,NULL)""")
    yield Db(name, uri, flavour)
    with sa.create_engine(uri).begin() as c:
        c.exec_driver_sql(f"drop table if exists {name}")


def test_typed_values_round_trip(my, tmp_path):
    ed = editor(tmp_path,
                set_field(SCORE, "99.99")
                + set_field(AVATAR, "00ff")
                + set_field(CREATED, "2025-06-01T00:00:00"), my.flavour)
    assert main(["-e", ed, "-d", my.uri, "-c", f"select * from {my.table}"]) == 0
    score, created, avatar = my.query(
        f"select score, created, avatar from {my.table} where id = 1")[0]
    assert str(score) == "99.99"
    assert str(created).startswith("2025-06-01 00:00:00")
    assert bytes(avatar) == b"\x00\xff"


def test_json_survives_update_and_insert(my, tmp_path):
    row = ['', '\\N', 'new'] + ['\\N'] * 5 + ['{"n": 7}', '\\N']
    ed = editor(tmp_path,
                set_field(PAYLOAD, '{"k": 2}')
                + f"lines.append({chr(9).join(row)!r})\n", my.flavour)
    assert main(["-e", ed, "-d", my.uri, "-c", f"select * from {my.table}"]) == 0
    assert my.one(f"select payload from {my.table} where id = 1") == '{"k": 2}'
    assert my.one(f"select payload from {my.table} where name = 'new'") == '{"n": 7}'


def test_insert_without_values_uses_server_defaults(my, tmp_path):
    row = ['', '\\N', 'dave'] + ['\\N'] * 7
    ed = editor(tmp_path, f"lines.append({chr(9).join(row)!r})\n", my.flavour)
    assert main(["-e", ed, "-d", my.uri, "-c", f"select * from {my.table}"]) == 0
    active, note, created = my.query(
        f"select active, note, created from {my.table} where name = 'dave'")[0]
    assert active == 1 and note == "default-note" and created is not None


def test_null_in_every_row_still_uses_the_column_type(my, tmp_path):
    ed = editor(tmp_path, set_field(3, "7.25"), my.flavour)
    assert main(["-e", ed, "-d", my.uri, "-c",
                 f"select id, name, score, created from {my.table} where name = 'bob'"]) == 0
    assert str(my.one(f"select score from {my.table} where name = 'bob'")) == "7.25"


def test_literal_percent_in_the_query(my, tmp_path):
    ed = editor(tmp_path, "pass\n", my.flavour)
    assert main(["-e", ed, "-d", my.uri, "-c",
                 f"select * from {my.table} where name like 'an%'"]) == 0


def test_concurrent_change_is_refused(my, tmp_path):
    ed = editor(tmp_path, textwrap.dedent(f"""
        import sqlalchemy as sa
        with sa.create_engine(uri).begin() as c:
            c.execute(sa.text("update {my.table} set name = 'THEIRS' where id = 1"))
    """) + set_field(NAME, "MINE"), my.flavour)
    with pytest.raises(SystemExit):
        main(["-e", ed, "-d", my.uri, "-c", f"select * from {my.table} where id = 1"])
    assert my.one(f"select name from {my.table} where id = 1") == "THEIRS"


def test_equivalent_value_is_not_a_mismatch(my, tmp_path):
    """These servers report changed rows unless FOUND_ROWS is set: 12.50 -> 12.5
    must still be treated as having matched the row."""
    ed = editor(tmp_path, set_field(SCORE, "12.5"), my.flavour)
    assert main(["-e", ed, "-d", my.uri, "-c",
                 f"select * from {my.table} where id = 1"]) == 0
    assert str(my.one(f"select score from {my.table} where id = 1")) == "12.50"


def test_max_rows_cap(my, tmp_path):
    my.execute(f"insert into {my.table}(name) values ('x'), ('y')")
    with pytest.raises(SystemExit):
        main(["--max-rows", "2", "-e", "true", "-d", my.uri,
              "-c", f"select * from {my.table}"])
