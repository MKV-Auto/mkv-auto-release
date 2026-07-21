"""Migration data-safety gate (#709).

Two guards against a migration that would lose or mangle user data on the
unattended startup `alembic upgrade head`:

1. **Reversibility coverage** (static, no DB): every migration must have a real
   `downgrade()` — or be a merge migration, or carry an explicit
   `# irreversible:` justification. A migration with no way back is a data-loss
   trap waiting to happen.

2. **Row-preservation round-trip** (real Postgres): seed a Disc→Job chain at
   head, then reverse and re-apply the newest migration (`downgrade -1` →
   `upgrade head`). The seeded rows must still exist. This catches a newest
   migration whose down/up path deletes or recreates existing rows.

Test 2 needs a PostgreSQL server (Alembic here uses PG-only DDL). It creates and
drops its own throwaway database and skips cleanly when no server is reachable.
"""
import os
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

BACKEND_DIR = Path(__file__).resolve().parents[1]
VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"


# ── 1. Reversibility coverage (static) ──────────────────────────────────────

def _downgrade_body(src: str) -> str:
    """Return the code body of downgrade(), sans comments/docstring/pass."""
    m = re.search(r"def downgrade\([^)]*\)[^:]*:\n(.*?)(?=\ndef |\Z)", src, re.DOTALL)
    if not m:
        return ""
    body = m.group(1)
    body = re.sub(r'"""[\s\S]*?"""', "", body)   # docstrings
    body = re.sub(r"'''[\s\S]*?'''", "", body)
    lines = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or s == "pass":
            continue
        lines.append(s)
    return "\n".join(lines)


def _is_merge(src: str) -> bool:
    # Merge migrations legitimately have an empty downgrade; they carry a tuple
    # down_revision (or say so in the message).
    return bool(re.search(r"down_revision\s*=\s*\(", src)) or "merge" in src.lower()[:600]


@pytest.mark.parametrize(
    "path",
    sorted(VERSIONS_DIR.glob("*.py")),
    ids=lambda p: p.name,
)
def test_every_migration_is_reversible_or_declared_irreversible(path: Path):
    src = path.read_text(encoding="utf-8")
    if "def downgrade" not in src:
        pytest.fail(f"{path.name}: no downgrade() at all")
    if _is_merge(src) or "# irreversible:" in src:
        return  # allowed: merge, or explicitly justified as irreversible
    assert _downgrade_body(src), (
        f"{path.name}: downgrade() is empty (just pass/comments). Implement a real "
        f"reversal, or if it genuinely cannot be reversed add a comment "
        f"'# irreversible: <reason>' so the choice is deliberate and reviewed."
    )


# ── 2. Row-preservation round-trip (real Postgres) ──────────────────────────

def _base_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    from api import database  # app default when DATABASE_URL is unset
    return database.DATABASE_URL


def _alembic_cfg():
    from alembic.config import Config
    return Config(str(BACKEND_DIR / "alembic.ini"))


@pytest.fixture()
def throwaway_db():
    """Create an isolated database for the migration cycle; drop it after.

    Skips if no PostgreSQL server is reachable (e.g. a unit-only environment)."""
    base = make_url(_base_url())
    if not base.get_backend_name().startswith("postgresql"):
        pytest.skip("migration round-trip requires PostgreSQL")
    admin_url = base.set(database="postgres")
    dbname = f"mig_safety_{uuid.uuid4().hex[:12]}"
    try:
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        conn = admin.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no PostgreSQL server reachable: {exc}")
    try:
        conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        conn.close()
    # render_as_string(hide_password=False): str(URL) masks the password as
    # "***" in SQLAlchemy 2.0, which would reach alembic/psycopg2 verbatim.
    temp_url = base.set(database=dbname).render_as_string(hide_password=False)
    try:
        yield temp_url
    finally:
        with create_engine(admin_url, isolation_level="AUTOCOMMIT").connect() as c:
            # Terminate stragglers, then drop.
            c.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :d AND pid <> pg_backend_pid()"
            ), {"d": dbname})
            c.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))


def test_newest_migration_preserves_existing_rows(throwaway_db, monkeypatch):
    from alembic import command

    temp_url = throwaway_db
    # alembic/env.py reads DATABASE_URL first — point the whole cycle at the temp DB.
    monkeypatch.setenv("DATABASE_URL", temp_url)
    cfg = _alembic_cfg()

    command.upgrade(cfg, "head")

    # Seed a realistic Disc -> Job chain via the ORM (applies model defaults).
    from api import models
    from sqlalchemy.orm import sessionmaker

    eng = create_engine(temp_url)
    Session = sessionmaker(bind=eng)
    s = Session()
    try:
        disc = models.Disc(content_hash=f"safety-{uuid.uuid4().hex}")
        s.add(disc)
        s.flush()
        job = models.Job(disc_id=disc.id, disc_num="1", mount_point="/dev/sr-test")
        s.add(job)
        s.commit()
        disc_id, job_id = disc.id, job.id
    finally:
        s.close()

    # Reverse and re-apply the NEWEST migration — the one that changes between
    # releases and would run on a populated user DB.
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")

    # Rows must survive (assert by id via raw SQL — robust to column changes).
    with eng.connect() as c:
        discs = c.execute(text("SELECT count(*) FROM discs WHERE id = :i"), {"i": disc_id}).scalar()
        jobs = c.execute(text("SELECT count(*) FROM jobs WHERE id = :i"), {"i": job_id}).scalar()
    assert discs == 1, "newest migration's down/up dropped the seeded disc row"
    assert jobs == 1, "newest migration's down/up dropped the seeded job row"
    eng.dispose()
