"""The migration-failure guard (#709).

When the guarded startup migration (Docker/scripts/db-migrate.sh) fails, it
writes a sentinel file. While that file exists the backend must report NOT ready
— even when the database itself is reachable — so the readiness_gate middleware
503s every mutating route and users can't operate on a half-migrated schema.
"""
import asyncio

import pytest

from api import main as api_main


@pytest.fixture(autouse=True)
def _reset_readiness_state(monkeypatch, tmp_path):
    monkeypatch.setattr(
        api_main, "_readiness_state",
        {"checked_at": 0.0, "ready": False, "error": None},
    )
    monkeypatch.setattr(api_main, "_readiness_lock", None)
    # Point the sentinel at a temp path and make the DB ping always succeed, so
    # the ONLY thing that can flip readiness in these tests is the sentinel.
    monkeypatch.setattr(api_main, "_MIGRATION_FAILED_SENTINEL", str(tmp_path / "mig-failed"))
    monkeypatch.setattr(api_main, "_ping_db_blocking", lambda: None)
    yield


def _write_sentinel(text="alembic upgrade head FAILED (rc=1)"):
    with open(api_main._MIGRATION_FAILED_SENTINEL, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_reason_none_when_sentinel_absent():
    assert api_main._migration_failure_reason() is None


def test_reason_reports_detail_when_present():
    _write_sentinel("boom detail")
    assert api_main._migration_failure_reason() == "boom detail"


def test_sync_readiness_is_false_while_sentinel_present():
    _write_sentinel()
    ready, err = api_main._check_db_ready()
    assert ready is False
    assert err is not None and err.startswith("migration_failed")


def test_async_readiness_is_false_while_sentinel_present():
    _write_sentinel()
    ready, err = asyncio.run(api_main._check_db_ready_async())
    assert ready is False
    assert "migration_failed" in err


def test_sentinel_overrides_a_warm_ready_cache():
    # Warm the cache to ready=True (DB ping stubbed to succeed)...
    ready, _ = api_main._check_db_ready()
    assert ready is True
    # ...then a migration failure lands. Readiness must flip to False on the
    # next check despite the warm cache (a slow migration can fail after the
    # API has already started serving).
    _write_sentinel()
    ready, err = api_main._check_db_ready()
    assert ready is False
    assert err.startswith("migration_failed")


def test_recovers_once_sentinel_cleared():
    _write_sentinel()
    assert api_main._check_db_ready()[0] is False
    import os
    os.remove(api_main._MIGRATION_FAILED_SENTINEL)
    assert api_main._check_db_ready()[0] is True
