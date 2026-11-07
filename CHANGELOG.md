# Changelog

Notable changes, in the spirit of [Keep a Changelog](https://keepachangelog.com/).
Versions follow [semantic versioning](https://semver.org/).

## 1.0.0

First release. vidb opens the rows returned by a SELECT in your editor and applies
the differences on exit, the way `vidir` does for file names.

### What it does

- Edit a line to update the columns whose text changed; delete a line to delete
  that row; add a line with an empty row number to insert, where `\N` leaves a
  column to its default.
- Row identity comes from the table's primary key, or `--key`. Keys that are not
  unique in the result are refused before the editor opens.
- `UPDATE` also matches the loaded values of the columns you changed, so a row
  edited by someone else in the meantime is refused rather than overwritten
  (`--force` opts out).
- Everything is applied in one transaction, and every `UPDATE`/`DELETE` must match
  exactly one row or the whole change is rolled back.
- The query can be a positional argument, `--query`, a file (`--from-file`,
  `-` for stdin), or a bare table name.
- A malformed edit file is reported before the database is touched; on any
  failure the edited file is kept and its path printed.
- `--dry-run`, `--confirm`, a delete-everything confirmation, and `--max-rows`
  (default 10000) to keep the editor usable.

### Verified against

SQLite, PostgreSQL 17, MySQL 8.4 and MariaDB 11, on Python 3.9 through 3.13.

### Known limits

- POSIX only: Linux and macOS. Windows is not supported.
- Deleting a row matches on the key alone, so it is not protected against a
  concurrent change of that row's contents.
- JSON columns are only pinned by the concurrency check where the server compares
  documents (PostgreSQL `jsonb`); elsewhere the update still applies.
- A database with no way to express "insert a row using only defaults" reports
  that plainly instead of storing something surprising.
- MySQL-family servers treat `TINYINT(1)` as `INTEGER`, so booleans show as `0`
  and `1` there.
