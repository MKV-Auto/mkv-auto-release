from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
import os

# Prefer env config; fall back to a local dev default if unset.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:ripper_pass@localhost:5432/discs",
)


def _int_env(name: str, default: int) -> int:
    try:
        val = os.getenv(name)
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


# Recycle connections after this many seconds so they don't sit in the pool
# indefinitely (avoids stale connections after Postgres server idle timeout).
POOL_RECYCLE_SEC = _int_env("SQLALCHEMY_POOL_RECYCLE", 1800)  # 30 min default


def _engine_connect_args(url: str) -> dict:
    """
    psycopg2: force UTF-8 client encoding so Unicode in labels (e.g. è) commits cleanly.

    Without this, some environments (e.g. C locale / ascii client_encoding) raise
    UnicodeEncodeError on flush/commit for PATCH /discs/{id}/titles and similar paths.
    """
    u = (url or "").strip().lower()
    if u.startswith("sqlite"):
        # SQLite enforces a single owning thread per connection by default;
        # FastAPI hands sessions across the threadpool/worker boundary, so we
        # disable that check. SQLite is the E2E backend's DB (see
        # run_e2e_backend.py); production runs on Postgres.
        return {"check_same_thread": False}
    if "mysql" in u or "mssql" in u or "oracle" in u:
        return {}
    if "postgresql" in u or u.startswith("postgres:"):
        return {"client_encoding": "utf8"}
    return {}


_engine_kwargs = dict(
    echo=os.getenv("SQL_ECHO", "0") == "1",
    future=True,
    pool_size=_int_env("SQLALCHEMY_POOL_SIZE", 10),
    max_overflow=_int_env("SQLALCHEMY_MAX_OVERFLOW", 20),
    pool_pre_ping=True,
    pool_recycle=POOL_RECYCLE_SEC,
)
_ca = _engine_connect_args(DATABASE_URL)
if _ca:
    _engine_kwargs["connect_args"] = _ca

# SQLite: use NullPool so each Session gets a fresh connection that closes
# on session.close() instead of returning to a pool. Pooled SQLite
# connections retain transactional state that triggers "database is locked"
# under FastAPI's get_db() dependency + concurrent eager Celery tasks (E2E).
# Postgres keeps the default QueuePool.
if (DATABASE_URL or "").strip().lower().startswith("sqlite"):
    from sqlalchemy.pool import NullPool
    for _k in ("pool_size", "max_overflow", "pool_recycle"):
        _engine_kwargs.pop(_k, None)
    _engine_kwargs["poolclass"] = NullPool

engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
