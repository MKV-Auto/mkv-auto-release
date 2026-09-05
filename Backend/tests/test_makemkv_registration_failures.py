"""MakeMKV registration failures must not masquerade as disc errors (#845 era
prod incident, 2026-09-03).

Three defects, one incident: (1) an expired evaluation aborts with BOTH
MSG:5055 and "Failed to open disc", and the rip path classified by the latter
— telling the user to reseat a healthy disc; (2) the registered key lived only
in container-layer settings.conf, so every image upgrade silently unregistered
MakeMKV; (3) the status probe ran a bare ``makemkvcon`` whose usage text can
never contain the expiry strings, so "expired" was undetectable.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from api import models
from core.utils import is_disc_read_error, is_registration_error

# Verbatim (abridged) robot output from the failed prod job — carries BOTH the
# shareware markers and the disc-read marker.
PROD_EVAL_EXPIRED_OUTPUT = """makemkvcon exited with code 11
Full output:
MSG:1005,0,1,"MakeMKV v1.18.4 linux(x64-release) started","%1 started","MakeMKV v1.18.4 linux(x64-release)"
MSG:3007,0,0,"Using direct disc access mode","Using direct disc access mode"
MSG:5055,0,0,"Evaluation period has expired, shareware functionality unavailable.","Evaluation period has expired, shareware functionality unavailable."
MSG:5052,516,0,"Evaluation period has expired. Please purchase an activation key if you've found this application useful. You may still use all free functionality without any restrictions.","Evaluation period has expired. Please purchase an activation key if you've found this application useful. You may still use all free functionality without any restrictions."
MSG:5010,0,0,"Failed to open disc","Failed to open disc"
"""


def test_prod_output_matches_both_classifiers_and_registration_wins():
    """The incident output triggers BOTH matchers — order is the fix."""
    assert is_registration_error(PROD_EVAL_EXPIRED_OUTPUT)
    assert is_disc_read_error(PROD_EVAL_EXPIRED_OUTPUT)
    # Worker-side precedence (mirrors workers/tasks.py):
    err_type = (
        "registration" if is_registration_error(PROD_EVAL_EXPIRED_OUTPUT)
        else "disc_read" if is_disc_read_error(PROD_EVAL_EXPIRED_OUTPUT)
        else None
    )
    assert err_type == "registration"


def test_registration_matcher_is_strict():
    """Safe on arbitrary rip output: only makemkvcon's explicit markers."""
    assert not is_registration_error("MSG:2003 read error at sector 253")
    assert not is_registration_error("Failed to open disc")
    assert not is_registration_error("")
    assert is_registration_error('MSG:5020,0,0,"stored activation key is invalid"')
    assert is_registration_error("MSG:5021 too old")


def test_rip_complete_callback_reclassifies_and_sets_config_failure_kind(test_db):
    """An untyped (or disc_read-typed) eval-expired failure lands as
    error_type=registration with failure_kind=config — the 'Fix settings'
    card — instead of the reseat-the-disc guidance."""
    from api.routers.jobs import RipCompleteRequest, rip_complete_callback

    session = test_db()
    try:
        disc = models.Disc(
            id=str(uuid.uuid4()), content_hash=f"hash-{uuid.uuid4().hex[:12]}",
        )
        job = models.Job(
            id=str(uuid.uuid4()), disc_id=disc.id, disc_num="1",
            mount_point="/dev/sr0", job_status="running", rip_state="running",
        )
        session.add_all([disc, job])
        session.commit()

        body = RipCompleteRequest(
            success=False,
            error_reason=PROD_EVAL_EXPIRED_OUTPUT,
            error_type="disc_read",  # what an old/mistaken worker would send
        )
        rip_complete_callback(job.id, body, session)

        session.refresh(job)
        assert job.job_status == "failed"
        assert job.error_type == "registration"
        assert job.failure_kind == "config"
    finally:
        session.close()


def test_reapply_stored_registration_key(tmp_path, monkeypatch):
    """Boot re-applies the UI-registered key from settings.json into
    settings.conf (which the container upgrade destroyed)."""
    from core import makemkv_updater

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    valid_key = "M-" + "a1B2c3D4e5F6g7H8i9J0" * 3  # 60 chars, matches key regex
    stored = {"makemkv_registration_key": valid_key}
    monkeypatch.setattr(
        "core.settings.load_settings", lambda: dict(stored)
    )

    conf = tmp_path / ".MakeMKV" / "settings.conf"
    assert not conf.exists()
    assert makemkv_updater.reapply_stored_registration_key() is True
    assert f'app_Key = "{valid_key}"' in conf.read_text()
    # Idempotent: already applied → no rewrite.
    assert makemkv_updater.reapply_stored_registration_key() is False
    # Other settings survive a re-apply after the key changes.
    conf.write_text('app_SomeSetting = "1"\napp_Key = "M-old"\n')
    assert makemkv_updater.reapply_stored_registration_key() is True
    text = conf.read_text()
    assert 'app_SomeSetting = "1"' in text
    assert f'app_Key = "{valid_key}"' in text
    assert 'app_Key = "M-old"' not in text
    # No stored key / malformed key → untouched.
    stored["makemkv_registration_key"] = None
    conf.unlink()
    assert makemkv_updater.reapply_stored_registration_key() is False
    stored["makemkv_registration_key"] = "T-MKVAUTO-DEVMODE-TEST-KEY-BYPASS"
    assert makemkv_updater.reapply_stored_registration_key() is False
    assert not conf.exists()


def test_registration_status_probe_uses_info_command(monkeypatch):
    """A bare makemkvcon run prints only usage text — the expiry strings can
    never appear, so 'expired' was undetectable. The probe must use the same
    '-r info disc:9999' invocation as the key probe (exits after the startup
    MSG stream, no drive needed)."""
    from core import makemkv_updater

    seen = {}

    class _R:
        stdout = 'MSG:5055,0,0,"Evaluation period has expired, shareware functionality unavailable.","..."\n'
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _R()

    monkeypatch.setattr(makemkv_updater, "get_makemkvcon_path", lambda: "/usr/bin/makemkvcon")
    monkeypatch.setattr(makemkv_updater.subprocess, "run", fake_run)
    expired, _msg, _key = makemkv_updater.get_registration_status(force_refresh=True)
    assert seen["cmd"][1:] == ["-r", "info", "disc:9999"]
    assert expired is True
