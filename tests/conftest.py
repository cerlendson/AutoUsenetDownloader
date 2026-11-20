import importlib

import pytest


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    from app import config, db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    return db
