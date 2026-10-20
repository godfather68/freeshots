"""Shared test setup: no test may touch the real data/shots.db."""

import shutil

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point SHOTS_DB at a per-test path (not created unless a test uses it)."""
    monkeypatch.setenv("SHOTS_DB", str(tmp_path / "isolated_shots.db"))
    monkeypatch.delenv("SHOTS_EXPORT_DIR", raising=False)  # no test writes an export unless it asks for one


@pytest.fixture(autouse=True)
def _production_switches(monkeypatch):
    """Since 2026-10-20 production runs fin_c on the no-odds route with the curve median
    (src/shots/config.py). Unit tests use stub/synthetic models written for the original routing,
    so they pin the original switches; tests about fin_c or the median set the variables themselves."""
    monkeypatch.setenv("SHOTS_NOODDS_MODEL", "negbin_pit_comp")
    monkeypatch.setenv("SHOTS_MEDIAN_FROM_CURVE", "0")


@pytest.fixture(autouse=True)
def _isolated_manual_results(tmp_path_factory, monkeypatch):
    """The committed results_manual/ holds real hand-entered results that need the live DB's fixtures;
    tests use an empty directory unless they pass one explicitly. Tests that check the committed files
    themselves read shots.ingest.manual_results.COMMITTED_DIR (the unpatched path)."""
    from shots.ingest import manual_results as mr

    empty = tmp_path_factory.mktemp("no_manual_results")  # outside the test's own tmp_path
    monkeypatch.setattr(mr, "MANUAL_DIR", empty)


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
