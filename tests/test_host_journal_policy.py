"""Real SQLite against pinned native policy, with isolated host configuration."""
import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

import pytest
from hermes_lcm.db_bootstrap import configure_connection


@pytest.fixture
def native_policy(monkeypatch):
    runtime = types.ModuleType("hermes_cli.sqlite_runtime")
    runtime.is_sqlite_wal_reset_vulnerable = lambda *args: False
    errors = types.ModuleType("hermes_state_errors")
    errors.is_sqlite_lock_error = lambda exc: "locked" in str(exc).lower()
    config = types.ModuleType("hermes_cli.config")
    config.load_config_readonly = lambda: {"database": {"journal_mode": "wal"}}
    for name, module in [(runtime.__name__, runtime), (errors.__name__, errors), (config.__name__, config)]:
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("hermes_state_wal", Path(__file__).parent / "fixtures/hermes_state_wal_31150a3.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "hermes_state_wal", module)
    spec.loader.exec_module(module)
    return module, config


@pytest.mark.parametrize("requested", ["wal", "delete"])
def test_native_config_policy_and_full_sync(tmp_path, native_policy, requested):
    module, config = native_policy
    config.load_config_readonly = lambda: {"database": {"journal_mode": requested}}
    with sqlite3.connect(tmp_path / "fresh.db") as conn:
        configure_connection(conn)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == requested
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        conn.execute("CREATE TABLE evidence(value)")
        conn.execute("INSERT INTO evidence VALUES ('persisted')")
    with sqlite3.connect(tmp_path / "fresh.db") as conn:
        assert conn.execute("SELECT value FROM evidence").fetchone()[0] == "persisted"


def test_native_policy_never_downgrades_existing_live_wal(tmp_path, native_policy):
    module, config = native_policy
    first = sqlite3.connect(tmp_path / "live.db")
    second = sqlite3.connect(tmp_path / "live.db")
    try:
        configure_connection(first)
        first.execute("CREATE TABLE evidence(value)")
        first.execute("INSERT INTO evidence VALUES ('retained')")
        first.commit()
        config.load_config_readonly = lambda: {"database": {"journal_mode": "delete"}}
        configure_connection(second)
        assert second.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert second.execute("SELECT value FROM evidence").fetchone()[0] == "retained"
    finally:
        second.close()
        first.close()


def test_native_helper_failure_is_visible(tmp_path, native_policy, monkeypatch):
    module, config = native_policy
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("injected policy failure")
    monkeypatch.setattr(module, "apply_wal_with_fallback", fail)
    with sqlite3.connect(tmp_path / "failed.db") as conn:
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            configure_connection(conn)


def test_settings_follow_actual_mode_not_assumed_return(tmp_path, native_policy, monkeypatch):
    module, config = native_policy
    monkeypatch.setattr(module, "apply_wal_with_fallback", lambda *a, **k: "wal")
    with sqlite3.connect(tmp_path / "readback.db") as conn:
        configure_connection(conn)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] != 500


def test_native_explicit_delete_under_exclusive_lock_fails(tmp_path, native_policy):
    module, config = native_policy
    config.load_config_readonly = lambda: {"database": {"journal_mode": "delete"}}
    first = sqlite3.connect(tmp_path / "locked.db")
    second = sqlite3.connect(tmp_path / "locked.db", timeout=0.01)
    try:
        first.execute("CREATE TABLE evidence(value)")
        first.commit()
        first.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError):
            configure_connection(second)
    finally:
        second.close()
        first.rollback()
        first.close()
