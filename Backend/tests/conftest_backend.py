import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import models
from tests.fixtures.mock_drive import MockDrive
from tests.fixtures.mock_mkv import MockMKV


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """
    MockDatabase: canonical test DB. In-memory SQLite on tmp_path; no real Postgres.

    Use in any test that needs a DB: `def test_foo(test_db):` then
    `with test_db() as session:` (or `session = test_db(); ...; session.close()`).
    Patches both `api.database.SessionLocal` and `workers.tasks.database.SessionLocal`.
    """
    engine = create_engine(f"sqlite:///{tmp_path}/test.db", future=True)
    @event.listens_for(engine, "connect")
    def _conn(dbapi_conn, connection_record):
        dbapi_conn.create_function("now", 0, lambda: datetime.datetime.now(datetime.UTC).isoformat())
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    models.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr("api.database.SessionLocal", SessionLocal)
    monkeypatch.setattr("workers.tasks.database.SessionLocal", SessionLocal)
    yield SessionLocal


@pytest.fixture
def test_db_shared_conn(tmp_path, monkeypatch):
    """
    Variant of ``test_db`` that pins every ``SessionLocal()`` to the same
    SQLite connection (``StaticPool`` + ``check_same_thread=False``).

    Use this for tests where the production code path opens a second
    session while an outer session is still mid-transaction — e.g.
    ``resume_postprocess`` opens a ``db_session`` and from within that
    block calls ``_post_postprocess_complete_callback`` which opens its
    own ``database.SessionLocal()`` and tries to commit. On Postgres
    those two sessions run as separate MVCC transactions; on SQLite with
    the default pool they live on distinct connections and the inner
    write hits ``database is locked`` against the outer's
    uncommitted write — silently rolling back the very transition the
    test is asserting on.

    The default ``test_db`` fixture stays as it was so threading-based
    tests (e.g. ``test_concurrent_rip_requests``) keep distinct
    per-thread connections.
    """
    engine = create_engine(
        f"sqlite:///{tmp_path}/test.db",
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    @event.listens_for(engine, "connect")
    def _conn(dbapi_conn, connection_record):
        dbapi_conn.create_function("now", 0, lambda: datetime.datetime.now(datetime.UTC).isoformat())
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    models.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr("api.database.SessionLocal", SessionLocal)
    monkeypatch.setattr("workers.tasks.database.SessionLocal", SessionLocal)
    yield SessionLocal


@pytest.fixture(autouse=True)
def _env_isolation(monkeypatch, tmp_path):
    # Ensure no real data directories are used during tests
    monkeypatch.setenv("MKVAUTO_ROOT", str(tmp_path / "mkvauto_root"))
    monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path / "mkvauto_data"))
    # Point DB to a throwaway SQLite for any accidental engine use
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    # Reload database module so global engine picks up the test database.
    import importlib
    from api import database
    importlib.reload(database)
    # Avoid external locks during tests
    class DummyLock:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("workers.tasks.FileLock", DummyLock)
    # Avoid real makemkv and hashing during tests. run_makemkv returns (str, int|None).
    monkeypatch.setattr("core.utils.run_makemkv", lambda *a, **k: ("Mock makemkv output", None))
    monkeypatch.setattr("core.utils.hash_media_disc", lambda *a, **k: "FAKEHASH")
    monkeypatch.setattr("core._drive_operations.run_makemkv", lambda *a, **k: ("Mock makemkv output", None))
    monkeypatch.setattr("core._drive_operations.hash_media_disc", lambda *a, **k: "FAKEHASH")
    yield


@pytest.fixture
def mock_drive(monkeypatch):
    """MockDrive: replaces _drive_operations and jobs.validate_disc_info. Writes to real disc_cache."""
    mock = MockDrive(
        drives=[("1", "/mnt/dvd")],
        discinfo_payload={
            "disc_num": "1",
            "mount_point": "/mnt/dvd",
            "show_title": "Test Show",
            "show_image": None,
            "tracks": {"00001.mpls": {"season": "1", "episode": "1", "episode_name": "Pilot", "format": "MainFeature"}},
            "disc_hash": "FAKEHASH",
            "content_hash": "FAKEHASH",
            "info_log": "TINFO:0,0,0,\"Test Title\"\nSINFO:0,0,19,0,\"1920x1080\"\n",
            "raw_info_log": "TINFO:0,0,0,\"Test Title\"\nSINFO:0,0,19,0,\"1920x1080\"\n",
        },
    )
    monkeypatch.setattr("core._drive_operations.list_drives", mock.list_drives)
    monkeypatch.setattr("core._drive_operations.get_disc_info", mock.get_disc_info)
    monkeypatch.setattr("core._drive_operations.refresh_disc_info", mock.refresh_disc_info)
    monkeypatch.setattr("core._drive_operations.validate_disc_info", mock.validate_disc_info)
    monkeypatch.setattr("core._drive_operations.scan_disc_info", mock.scan_disc_info)
    monkeypatch.setattr("core._drive_operations.hash_disc", mock.hash_disc)
    monkeypatch.setattr("core._drive_operations.handle_disc_eject", mock.handle_disc_eject)
    monkeypatch.setattr("core._drive_operations.handle_disc_insert", mock.handle_disc_insert)
    monkeypatch.setattr("api.routers.jobs.validate_disc_info", mock.validate_disc_info)
    # disc_manager imports at load time; patch those references too
    monkeypatch.setattr("core.disc_manager._get_disc_info", mock.get_disc_info)
    monkeypatch.setattr("core.disc_manager._refresh_disc_info", mock.refresh_disc_info)
    # drives router imports list_drives at load time
    monkeypatch.setattr("api.routers.drives.list_drives", mock.list_drives)
    # Seed disc_cache for the default drive so get_disc_info(refresh=False) works
    if mock.drives:
        n, mp = mock.drives[0]
        mock.refresh_disc_info(n, mp)
    return mock


@pytest.fixture
def mock_mkv(monkeypatch):
    """MockMKV: replaces run_makemkv at core.utils, core.disc, api.crud. Returns (log_str, pid); log is parse_log-compatible."""
    mock = MockMKV(titles=[{"file": "00001.mpls"}], progress=True)
    monkeypatch.setattr("core.utils.run_makemkv", mock.run_makemkv)
    monkeypatch.setattr("core.disc.run_makemkv", mock.run_makemkv)
    monkeypatch.setattr("api.crud.run_makemkv", mock.run_makemkv)
    return mock


def _make_stage_callback_fake_requests():
    """Build a fake 'requests' module that intercepts worker callback POSTs.

    Thin wrapper around the shared
    ``tests.fixtures.stage_callback_intercept.make_stage_callback_fake_requests``
    helper so the E2E backend bootstrap and the integration tests apply the
    same callback interception.
    """
    from tests.fixtures.stage_callback_intercept import make_stage_callback_fake_requests
    return make_stage_callback_fake_requests()


@pytest.fixture
def stage_callback_mocks(request):
    """
    For tests marked @pytest.mark.integration: context manager that patches workers.tasks.requests
    so rip-complete and postprocess-complete POSTs apply state via StageState in the test DB.
    Use as::

        with stage_callback_mocks:
            tasks.rip_disc.run(...)
    """
    from contextlib import contextmanager, nullcontext
    try:
        has_integration = request.node.get_closest_marker("integration") is not None
    except Exception:
        has_integration = False
    if not has_integration:
        yield nullcontext()
        return

    from unittest.mock import patch

    @contextmanager
    def _patcher():
        fake_requests = _make_stage_callback_fake_requests()
        p = patch("workers.tasks.requests", fake_requests)
        p.start()
        try:
            yield
        finally:
            p.stop()

    yield _patcher()
