"""Shared test setup: no test may touch the real data/shots.db."""

import shutil

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point SHOTS_DB at a per-test path (not created unless a test uses it)."""
    monkeypatch.setenv("SHOTS_DB", str(tmp_path / "isolated_shots.db"))
    monkeypatch.delenv("SHOTS_EXPORT_DIR", raising=False)  # no test writes an export unless it asks for one


@pytest.fixture(scope="session")
def _migrated_template(tmp_path_factory):
    from shots.db import migrate

    path = tmp_path_factory.mktemp("dbtemplate") / "template.db"
    migrate.upgrade(path)  # disposes its engine: the WAL is checkpointed into the file on close
    return path


@pytest.fixture
def migrated_db(tmp_path, _migrated_template):
    """A freshly migrated empty DB file (copied from a session template: migrating once is enough)."""
    path = tmp_path / "shots.db"
    shutil.copy(_migrated_template, path)
    return path
