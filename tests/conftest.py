import pytest

import vidb.cli as cli


@pytest.fixture(autouse=True)
def isolated_edit_dir(tmp_path, monkeypatch):
    """Keep kept-edit-files out of the real temp directory during tests."""
    monkeypatch.setattr(cli, "EDIT_DIR", str(tmp_path / "vidb-edits"))
