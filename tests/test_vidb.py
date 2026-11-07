import contextlib
import sqlite3
import sys
import textwrap

import pytest

from vidb import codec
from vidb.cli import main
from vidb.db import guess_table, expand_query


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    c = sqlite3.connect(path)
    c.executescript("""
        create table users(id integer primary key, name text, age integer, note text);
        insert into users values (1,'ann',30,'hi'),(2,'bob',25,NULL),(3,'cy',41,'a\tb');
        create table nopk(a text, b text);
        insert into nopk values ('x','1'),('y','2');
    """)
    c.commit()
    c.close()
    return str(path)


def editor(tmp_path, body):
    """Create an 'editor' script: python code that edits the file `path`."""
    script = tmp_path / "ed.py"
    script.write_text("import sys\npath = sys.argv[1]\n"
                      "lines = open(path).read().split('\\n')\n"
                      + textwrap.dedent(body) +
                      "\nopen(path, 'w').write('\\n'.join(lines))\n")
    return f"{sys.executable} {script}"


def rows(db, sql="select * from users order by id"):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def test_update_delete_insert(db, tmp_path):
    ed = editor(tmp_path, """
        lines = [l for l in lines if not l.startswith('0002')]       # delete bob
        lines = [l.replace('\\tann\\t', '\\tANN\\t') for l in lines]   # update
        lines = [l.replace('\\t41\\t', '\\t\\\\N\\t') for l in lines]   # age -> NULL
        lines.insert(-1, '\\t\\\\N\\tdan\\t7\\tnew')                            # insert
    """)
    assert main(["-e", ed, db, "select * from users"]) == 0
    assert rows(db) == [(1, "ANN", 30, "hi"), (3, "cy", None, "a\tb"), (4, "dan", 7, "new")]


def test_no_change(db, tmp_path):
    ed = editor(tmp_path, "pass")
    assert main(["-e", ed, db, "users"]) == 0
    assert len(rows(db)) == 3


def test_type_coercion_and_unchanged_columns(db, tmp_path):
    ed = editor(tmp_path, "lines = [l.replace('\\t25', '\\t26') for l in lines]")
    main(["-e", ed, db, "select id, name, age from users"])
    assert rows(db, "select age, note from users where id=2") == [(26, None)]


def test_dry_run_changes_nothing(db, tmp_path, capsys):
    ed = editor(tmp_path, "lines = [l for l in lines if not l.startswith('0001')]")
    assert main(["-n", "-e", ed, db, "users"]) == 0
    assert len(rows(db)) == 3
    assert "DELETE row 1" in capsys.readouterr().out


def test_delete_all_requires_yes(db, tmp_path):
    ed = editor(tmp_path, "lines = [l for l in lines if l.startswith('#')]")
    with pytest.raises(SystemExit):
        main(["-e", ed, db, "users"])
    assert len(rows(db)) == 3
    assert main(["-y", "-e", ed, db, "users"]) == 0
    assert rows(db) == []


def test_bad_field_count_aborts(db, tmp_path):
    ed = editor(tmp_path, "lines = [l.replace('\\tann', '') for l in lines]")
    with pytest.raises(SystemExit):
        main(["-e", ed, db, "users"])
    assert rows(db)[0] == (1, "ann", 30, "hi")


def test_bad_integer_aborts_without_partial_apply(db, tmp_path):
    ed = editor(tmp_path, """
        lines = [l.replace('\\tann\\t', '\\tANN\\t').replace('\\t25\\t', '\\tabc\\t') for l in lines]
    """)
    with pytest.raises(SystemExit):
        main(["-e", ed, db, "users"])
    assert rows(db)[0][1] == "ann"


def test_no_primary_key_matches_whole_row(db, tmp_path):
    ed = editor(tmp_path, "lines = [l.replace('\\tx\\t', '\\tz\\t') for l in lines]")
    main(["-e", ed, db, "nopk"])
    assert rows(db, "select * from nopk order by b") == [("z", "1"), ("y", "2")]


def test_key_must_be_selected(db, tmp_path):
    with pytest.raises(SystemExit):
        main(["-e", "true", db, "select name from users"])


def test_key_override_and_table_flag(db, tmp_path):
    ed = editor(tmp_path, "lines = [l.replace('\\tann', '\\tANN') for l in lines]")
    q = "select u.name as nm, u.id from users u join users v on v.id = u.id"
    assert guess_table(q) is None
    main(["-t", "users", "-k", "id", "-e", ed, db, "select id, name from users"])
    assert rows(db, "select name from users where id=1") == [("ANN",)]


def test_editor_failure_changes_nothing(db):
    with pytest.raises(SystemExit):
        main(["-e", "false", db, "users"])


# ---- codec / helpers

@pytest.mark.parametrize("s", ["", "plain", "a\tb", "a\nb\r", "back\\slash", "\\N", "x\\", "\\\\"])
def test_escape_roundtrip(s):
    assert codec.unescape(codec.escape(s)) == s


def test_null_marker():
    assert codec.unescape("\\N") is None


@pytest.mark.parametrize("q,expect", [
    ("select * from users", "users"),
    ("SELECT a FROM public.users WHERE x = 'from y' ORDER BY a", "users"),
    ('select * from "My Table" where 1', "My Table"),
    ("select * from a join b on 1", None),
    ("select * from a, b", None),
    ("select * from (select 1) s", None),
    ("select a from t union select a from u", None),
])
def test_guess_table(q, expect):
    t = guess_table(q)
    assert (t.name if t else None) == expect


def test_expand_query():
    assert expand_query("users") == "SELECT * FROM users"
    assert expand_query("public.users;") == "SELECT * FROM public.users"
    assert expand_query("select 1") == "select 1"


# ---- review-driven regressions

def test_lost_update_is_refused(db, tmp_path, capsys):
    """Another writer touches the row while the editor is open."""
    ed = editor(tmp_path, f"""
        import sqlite3
        c = sqlite3.connect({db!r})
        c.execute("update users set name='other-user' where id=1")
        c.commit(); c.close()
        lines = [l.replace('\\tann', '\\tMINE') for l in lines]
    """)
    with pytest.raises(SystemExit):
        main(["-e", ed, db, "select * from users"])
    assert rows(db, "select name from users where id=1") == [("other-user",)]
    assert "changed or deleted" in capsys.readouterr().err


def test_force_overwrites_concurrent_change(db, tmp_path):
    ed = editor(tmp_path, f"""
        import sqlite3
        c = sqlite3.connect({db!r})
        c.execute("update users set name='other-user' where id=1")
        c.commit(); c.close()
        lines = [l.replace('\\tann', '\\tMINE') for l in lines]
    """)
    assert main(["--force", "-e", ed, db, "select * from users"]) == 0
    assert rows(db, "select name from users where id=1") == [("MINE",)]


def test_nonunique_key_refused_before_editor_runs(db, tmp_path, capsys):
    import sqlite3
    c = sqlite3.connect(db)
    c.execute("update users set note='dup' where id in (1,3)")
    c.commit()
    c.close()
    marker = tmp_path / "ran"
    ed = editor(tmp_path, f"open({str(marker)!r}, 'w').write('x')\n")
    with pytest.raises(SystemExit):
        main(["-k", "note", "-e", ed, db, "select * from users where note = 'dup'"])
    assert not marker.exists()          # refused before the user did any work
    assert "not unique" in capsys.readouterr().err


def test_three_part_table_name_rejected(db):
    with pytest.raises(SystemExit):
        main(["-t", "a.b.c", "-e", "true", db, "users"])


def test_stripped_trailing_tab_is_repaired(db, tmp_path):
    """An editor that trims trailing whitespace must not corrupt empty last fields."""
    ed = editor(tmp_path, """
        lines = [l.rstrip('\\t ') for l in lines]              # what many editors do
        lines = [l.replace('\\tann', '\\tANN') for l in lines]  # a real edit
    """)
    assert main(["-e", ed, db, "select id, name, note from users where id in (1,2)"]) == 0
    assert rows(db) == [(1, "ANN", 30, "hi"), (2, "bob", 25, None), (3, "cy", 41, "a\tb")]


def test_genuinely_short_line_still_errors(db, tmp_path):
    ed = editor(tmp_path, "lines = [l.replace('\\tann', '') for l in lines]")
    with pytest.raises(SystemExit):
        main(["-e", ed, db, "select id, name, note from users"])
    assert rows(db)[0] == (1, "ann", 30, "hi")


def test_uri_and_password_from_environment(db, tmp_path, monkeypatch):
    monkeypatch.setenv("VIDB_URI", db)
    monkeypatch.setenv("VIDB_PASSWORD", "unused-for-sqlite")
    ed = editor(tmp_path, "lines = [l.replace('\\tann', '\\tANN') for l in lines]")
    assert main(["-e", ed, "users"]) == 0
    assert rows(db)[0][1] == "ANN"


def test_missing_uri_is_reported(monkeypatch):
    monkeypatch.delenv("VIDB_URI", raising=False)
    with pytest.raises(SystemExit):
        main(["users"])


def test_ask_password_prompts(db, tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr("vidb.cli.getpass.getpass", lambda p="": asked.append(p) or "pw")
    ed = editor(tmp_path, "pass")
    assert main(["-W", "-e", ed, db, "users"]) == 0
    assert asked == ["Database password: "]


@pytest.mark.parametrize("answer,applied", [(False, False), (True, True)])
def test_confirm_flag(db, tmp_path, monkeypatch, answer, applied):
    monkeypatch.setattr("vidb.cli.ask", lambda *a, **k: answer)
    ed = editor(tmp_path, "lines = [l.replace('\\tann', '\\tANN') for l in lines]")
    expected = contextlib.nullcontext() if applied else pytest.raises(SystemExit)
    with expected:
        main(["-i", "-e", ed, db, "users"])
    assert (rows(db)[0][1] == "ANN") is applied


def test_url_and_uri_normalization(tmp_path):
    from vidb.db import make_url_for, normalize_uri

    f = tmp_path / "x.db"
    f.write_text("")
    assert normalize_uri(str(f)) == f"sqlite:///{f}"
    assert normalize_uri(":memory:") == "sqlite://"
    assert normalize_uri("postgresql://h/d") == "postgresql://h/d"
    with pytest.raises(Exception):
        normalize_uri("not-a-db")

    u = make_url_for("postgres://h/d")
    assert u.drivername == "postgresql"
    u = make_url_for("postgresql://alice@h/d", user="bob", password="s3cr3t")
    assert (u.username, u.password) == ("bob", "s3cr3t")
    # credentials must not leak into SQLite URLs (a global $VIDB_PASSWORD is common)
    assert make_url_for(str(f), user="bob", password="x").password is None


# ---- query input forms

def test_query_flag_with_positional_uri(db, tmp_path):
    ed = editor(tmp_path, "lines = [l.replace('\\tann', '\\tANN') for l in lines]")
    assert main(["-e", ed, "-c", "select * from users", db]) == 0
    assert rows(db)[0][1] == "ANN"


def test_database_flag_with_positional_query(db, tmp_path):
    ed = editor(tmp_path, "lines = [l.replace('\\tann', '\\tANN') for l in lines]")
    assert main(["-e", ed, "-d", db, "select * from users"]) == 0
    assert rows(db)[0][1] == "ANN"


def test_both_inputs_as_flags(db, tmp_path, monkeypatch):
    monkeypatch.delenv("VIDB_URI", raising=False)
    ed = editor(tmp_path, "lines = [l.replace('\\tann', '\\tANN') for l in lines]")
    assert main(["-e", ed, "-d", db, "-c", "select * from users"]) == 0
    assert rows(db)[0][1] == "ANN"


def test_from_file_multiline_query(db, tmp_path):
    q = tmp_path / "q.sql"
    q.write_text("-- emails we must fix\nselect id, name, note\n"
                 "from users\nwhere id = 1\n")
    ed = editor(tmp_path, "lines = [l.replace('\\thi', '\\tHI') for l in lines]")
    assert main(["-e", ed, "-f", str(q), db]) == 0
    assert rows(db)[0][3] == "HI"


def test_from_file_stdin(db, tmp_path, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("select * from users\n"))
    ed = editor(tmp_path, "lines = [l.replace('\\tann', '\\tANN') for l in lines]")
    assert main(["-e", ed, "-f", "-", db]) == 0
    assert rows(db)[0][1] == "ANN"


def test_from_file_bare_table_name_is_expanded(db, tmp_path):
    q = tmp_path / "q.sql"
    q.write_text("users\n")
    ed = editor(tmp_path, "lines = [l.replace('\\tann', '\\tANN') for l in lines]")
    assert main(["-e", ed, "-f", str(q), db]) == 0
    assert rows(db)[0][1] == "ANN"


def test_empty_query_file_is_rejected(db, tmp_path):
    q = tmp_path / "q.sql"
    q.write_text("  \n\n")
    with pytest.raises(SystemExit):
        main(["-f", str(q), "-e", "true", db])


def test_query_and_file_are_mutually_exclusive(db, tmp_path):
    q = tmp_path / "q.sql"
    q.write_text("select 1")
    with pytest.raises(SystemExit):
        main(["-c", "select 1", "-f", str(q), "-e", "true", db])


def test_uri_given_twice_is_rejected(db, tmp_path):
    with pytest.raises(SystemExit):
        main(["-d", db, "-e", "true", db, "select * from users"])
    with pytest.raises(SystemExit):
        main(["-c", "select * from users", "-d", db, "-e", "true", db])


def test_no_query_given(db):
    with pytest.raises(SystemExit):
        main(["-d", db, "-e", "true"])


@pytest.mark.parametrize("query,warns", [
    ("select * from users limit 10", True),
    ("select * from users order by id limit 10", False),
    ("select * from users order by id", False),
    ("select * from users", False),
    ("select * from users fetch first 5 rows only", True),
    ("select * from users offset 5 rows fetch next 5 rows only", True),
    ("select * from users where note = 'limit 3'", False),   # literal, not a limit
    ("select * from users limit 3 -- order by id", True),    # commented out
    ("select * from users /* order by id */ limit 3", True),
])
def test_limit_without_order_detection(query, warns):
    from vidb.db import limit_without_order
    assert limit_without_order(query) is warns


def test_limit_without_order_warns_on_stderr(db, tmp_path, capsys):
    ed = editor(tmp_path, "pass")
    assert main(["-e", ed, db, "select * from users limit 2"]) == 0
    assert "LIMIT without ORDER BY" in capsys.readouterr().err


def test_ordered_limit_does_not_warn(db, tmp_path, capsys):
    ed = editor(tmp_path, "pass")
    assert main(["-e", ed, db, "select * from users order by id limit 2"]) == 0
    assert "LIMIT without ORDER BY" not in capsys.readouterr().err


# ---- row cap, edit-file hygiene, key validation

def test_max_rows_refuses_large_result(db):
    with pytest.raises(SystemExit):
        main(["--max-rows", "2", "-e", "true", db, "select * from users"])


def test_max_rows_zero_disables_the_cap(db, tmp_path):
    ed = editor(tmp_path, "lines = [l.replace('\\tann', '\\tANN') for l in lines]")
    assert main(["--max-rows", "0", "-e", ed, db, "select * from users"]) == 0
    assert rows(db)[0][1] == "ANN"


def test_edit_files_are_private_and_cleaned_up(db, tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    ed = editor(tmp_path, "lines = [l.replace('\\tann', '\\tANN') for l in lines]")
    assert main(["-e", ed, db, "users"]) == 0
    d = tmp_path / "vidb-edits"
    assert d.is_dir() and (d.stat().st_mode & 0o777) == 0o700
    assert list(d.iterdir()) == []            # nothing kept after success
    # a failure keeps the file
    bad = editor(tmp_path, "lines = [l.replace('0001', '0x01') for l in lines]")
    with pytest.raises(SystemExit):
        main(["-e", bad, db, "select id, name, note from users"])
    kept = list(d.iterdir())
    assert len(kept) == 1 and (kept[0].stat().st_mode & 0o777) == 0o600
    assert main(["--clean"]) == 0
    assert list(d.iterdir()) == []


def test_key_must_be_a_table_column(db, tmp_path):
    with pytest.raises(SystemExit):
        main(["-k", "nickname", "-e", "true", db,
              "select name as nickname, id from users"])


def test_sqlite_rowid_is_a_usable_key(db, tmp_path):
    # "users" has an INTEGER PRIMARY KEY, where rowid is just an alias of id;
    # nopk has no key at all, so rowid is the only handle on a row.
    ed = editor(tmp_path, "lines = [l.replace('\\t1\\tx\\t', '\\t1\\tX\\t') for l in lines]")
    assert main(["-k", "rowid", "-e", ed, db, "select rowid, a, b from nopk"]) == 0
    assert rows(db, "select * from nopk order by b") == [("X", "1"), ("y", "2")]


# ---- graceful handling of engines that cannot express an all-defaults insert

def test_all_default_insert_rejected_is_explained(db, tmp_path, monkeypatch, capsys):
    import vidb.cli as cli

    real = cli.make_engine

    def without_default_support(uri, user=None, password=None):
        engine = real(uri, user, password)
        engine.dialect.supports_default_values = False
        engine.dialect.supports_empty_insert = False
        return engine

    monkeypatch.setattr(cli, "make_engine", without_default_support)
    row = [''] + ['\\N'] * 4
    ed = editor(tmp_path, f"lines.append({chr(9).join(row)!r})\n")
    with pytest.raises(SystemExit):
        main(["-e", ed, db, "select * from users"])
    err = capsys.readouterr().err
    assert "leaves every column to its default" in err
    assert "at least one column a value" in err
    assert len(rows(db)) == 3            # rolled back


def test_column_names_in_the_header_are_escaped():
    from vidb.codec import format_document
    doc = format_document(["a\tb", "c\nd"], [[1, 2]], ["header"])
    assert "# columns: a\\tb\tc\\nd" in doc


# ---- 1.0 decisions

def test_password_flag_is_not_accepted(db):
    """A password on the command line is visible in ps; -W and $VIDB_PASSWORD cover it."""
    with pytest.raises(SystemExit):
        main(["-p", "secret", "-e", "true", db, "select * from users"])


def test_platform_check():
    from vidb.cli import check_platform
    from vidb.db import VidbError

    check_platform("posix")
    with pytest.raises(VidbError, match="not supported"):
        check_platform("nt")


def test_version_is_single_sourced():
    import importlib.metadata as md

    import vidb

    assert md.version("vidb") == vidb.__version__
    assert vidb.__version__.count(".") == 2
