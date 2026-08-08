"""POST /system/support-bundle — one-click diagnostics from the web UI (#804).

Diagnosing #802 took several rounds of asking a user to paste shell output, and
two conclusions drawn from those pastes were wrong: once because the output was
ambiguous, once because `docker exec <c> ls /dev/sg*` silently describes the
host. The endpoint exists so the user clicks a button instead.

It shells out to the same ``scripts/mkv-support-bundle.sh`` a user would run by
hand — one implementation of the diagnosis rather than a shell one and a Python
one that drift apart — so what is worth testing here is the wiring around it:
the drive-lock guard, and that failures surface instead of returning a corrupt
download.
"""
from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import models as db_models
from api.database import get_db
from api.main import app
from api.routers import system

ENDPOINT = "/system/support-bundle"


@pytest.fixture
def client(test_db):
    def override_get_db():
        session = test_db()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def fake_script(tmp_path, monkeypatch):
    """A stand-in for the real script that records the flags it was given.

    Deliberately not the real thing: this test is about the endpoint's wiring,
    and the script's own behaviour is covered by having been exercised against
    a live container. Writing a tarball keeps the response path honest.
    """
    argfile = tmp_path / "args.txt"
    script = tmp_path / "fake-bundle.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > {argfile}\n'
        'outdir=""\n'
        'while [ $# -gt 0 ]; do\n'
        '  case "$1" in --bundle) outdir="$2"; shift ;; esac\n'
        '  shift\n'
        'done\n'
        'mkdir -p "$outdir/payload"\n'
        'echo verdict > "$outdir/payload/verdict.txt"\n'
        'tar -czf "$outdir/mkv-auto-support-20260101-000000.tar.gz" '
        '-C "$outdir" payload\n'
    )
    script.chmod(0o755)
    monkeypatch.setattr(system, "SUPPORT_BUNDLE_SCRIPT", script)
    return argfile


def _make_ripping_job(test_db, rip_state="running"):
    """A job in ``rip_state``. disc_id / disc_num / mount_point are NOT NULL."""
    import uuid

    with test_db() as s:
        disc_id = str(uuid.uuid4())
        s.add(db_models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        s.add(
            db_models.Job(
                id=f"job-{rip_state}",
                disc_id=disc_id,
                disc_num="0",
                mount_point="/dev/sr0",
                rip_state=rip_state,
            )
        )
        s.commit()


# ──────────────────────────────────────────────────────────────────────
# Happy path
# ──────────────────────────────────────────────────────────────────────

def test_returns_a_real_gzip_archive(client, fake_script, tmp_path):
    r = client.post(ENDPOINT)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    assert "mkv-auto-support-" in r.headers["content-disposition"]

    # Actually open it — a 200 carrying a truncated or empty body would pass a
    # status-code-only assertion and be useless to whoever receives it.
    out = tmp_path / "got.tar.gz"
    out.write_bytes(r.content)
    with tarfile.open(out, "r:gz") as tf:
        assert any(m.name.endswith("verdict.txt") for m in tf.getmembers())


def test_scope_header_says_container(client, fake_script):
    """The bundle cannot see the Docker engine's own config from in here, and
    the header records that so a reader knows what is missing."""
    assert client.post(ENDPOINT).headers["x-support-bundle-scope"] == "container"


# ──────────────────────────────────────────────────────────────────────
# The drive-lock guard — the reason this endpoint is not a plain exec
# ──────────────────────────────────────────────────────────────────────

def test_collects_when_no_rip_is_active(client, fake_script):
    assert client.post(ENDPOINT).status_code == 200


@pytest.mark.parametrize("rip_state", ["running", "pending"])
def test_refused_while_a_rip_holds_the_drive(client, fake_script, test_db, rip_state):
    """`makemkvcon info disc:9999` takes MakeMKV's drive lock.

    Running it mid-rip makes the rip block on the drive (#545, #547). An
    earlier version skipped just that probe and returned the rest, but a
    bundle silently missing the drive enumeration — the thing the user is
    usually trying to diagnose — is worse than asking them to come back.
    """
    _make_ripping_job(test_db, rip_state)
    r = client.post(ENDPOINT)
    assert r.status_code == 409
    assert "rip" in r.json()["detail"].lower()
    # And nothing was collected, so the rip was never touched.
    assert not fake_script.exists()


def test_finished_jobs_do_not_block_collection(client, fake_script, test_db):
    """Only pending/running hold a drive; a completed job must not lock the
    user out of diagnostics forever."""
    _make_ripping_job(test_db, "completed")
    assert client.post(ENDPOINT).status_code == 200


# ──────────────────────────────────────────────────────────────────────
# Availability — so the button can disable itself instead of erroring
# ──────────────────────────────────────────────────────────────────────

AVAIL = "/system/support-bundle/availability"


def test_available_when_idle(client, fake_script):
    body = client.get(AVAIL).json()
    assert body["available"] is True
    assert body["reason"] is None


def test_unavailable_during_a_rip_with_a_reason(client, fake_script, test_db):
    _make_ripping_job(test_db, "running")
    body = client.get(AVAIL).json()
    assert body["available"] is False
    assert body["reason"] == "rip_in_progress"
    assert "rip" in body["message"].lower()


def test_unavailable_when_the_script_is_missing(client, monkeypatch, tmp_path):
    monkeypatch.setattr(system, "SUPPORT_BUNDLE_SCRIPT", tmp_path / "nope.sh")
    body = client.get(AVAIL).json()
    assert body["available"] is False
    assert body["reason"] == "script_missing"
    assert "host" in body["message"].lower()


# ──────────────────────────────────────────────────────────────────────
# Failure modes surface rather than returning a broken file
# ──────────────────────────────────────────────────────────────────────

def test_missing_script_is_503_and_points_at_the_host(client, monkeypatch, tmp_path):
    monkeypatch.setattr(system, "SUPPORT_BUNDLE_SCRIPT", tmp_path / "nope.sh")
    r = client.post(ENDPOINT)
    assert r.status_code == 503
    assert "host" in r.json()["detail"].lower()


def test_script_failure_is_500_with_its_output(client, monkeypatch, tmp_path):
    script = tmp_path / "boom.sh"
    script.write_text("#!/usr/bin/env bash\necho 'collection exploded' >&2\nexit 3\n")
    script.chmod(0o755)
    monkeypatch.setattr(system, "SUPPORT_BUNDLE_SCRIPT", script)

    r = client.post(ENDPOINT)
    assert r.status_code == 500
    # The operator needs the reason, not just the code.
    assert "collection exploded" in r.json()["detail"]


def test_success_exit_without_an_archive_is_still_an_error(client, monkeypatch, tmp_path):
    """Exit 0 is not proof a bundle exists — returning 200 with nothing
    attached would be the worst outcome."""
    script = tmp_path / "empty.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setattr(system, "SUPPORT_BUNDLE_SCRIPT", script)
    assert client.post(ENDPOINT).status_code == 500


def test_timeout_is_504_and_kills_the_process(client, monkeypatch, tmp_path):
    script = tmp_path / "slow.sh"
    script.write_text("#!/usr/bin/env bash\nsleep 30\n")
    script.chmod(0o755)
    monkeypatch.setattr(system, "SUPPORT_BUNDLE_SCRIPT", script)
    monkeypatch.setattr(system, "SUPPORT_BUNDLE_TIMEOUT_SECONDS", 1)

    r = client.post(ENDPOINT)
    assert r.status_code == 504
    assert "timed out" in r.json()["detail"].lower()


def test_failures_do_not_leave_temp_directories_behind(client, monkeypatch, tmp_path):
    """Every error path unlinks its workdir; a support feature that fills the
    disk each time it fails is worse than not having it."""
    import tempfile

    script = tmp_path / "boom.sh"
    script.write_text("#!/usr/bin/env bash\nexit 1\n")
    script.chmod(0o755)
    monkeypatch.setattr(system, "SUPPORT_BUNDLE_SCRIPT", script)

    before = set(Path(tempfile.gettempdir()).glob("support-bundle-*"))
    client.post(ENDPOINT)
    assert set(Path(tempfile.gettempdir()).glob("support-bundle-*")) == before
