"""
Tests for the sandbox-probe key validation pipeline (#688).

``makemkvcon reg`` rejects valid beta keys that MakeMKV's runtime accepts from
settings.conf, so set_registration_key evaluates candidates in an isolated
$HOME and classifies by makemkvcon's own startup verdict. MSG fixtures below
were captured from real makemkvcon 1.18.4 runs (2026-07-19):

- clean start → key accepted (works even mid-trial)
- MSG:5020   → "stored activation key is invalid" (definitive rejection)
- MSG:5021   → "application version is too old" (binary expired)
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import makemkv_updater
from core.makemkv_updater import MakeMKVUpdateError

BETA_KEY = "T-" + "a" * 66
PURCHASED_KEY = "M-" + "b" * 66

MSG_CLEAN = (
    'MSG:1005,0,1,"MakeMKV v1.18.4 linux(x64-release) started","%1 started","MakeMKV v1.18.4 linux(x64-release)"\n'
    'MSG:5074,0,0,"Automatic checking for updates is enabled...","..."\n'
)
MSG_INVALID = (
    'MSG:1005,0,1,"MakeMKV v1.18.4 linux(x64-release) started","%1 started","MakeMKV v1.18.4 linux(x64-release)"\n'
    'MSG:5020,516,0,"The stored activation key is invalid. I guess someone tampered with settings...","..."\n'
    'MSG:5021,131332,1,"This application version is too old.  Please download the latest version...","..."\n'
)
MSG_TOO_OLD = (
    'MSG:1005,0,1,"MakeMKV v1.18.3 linux(x64-release) started","%1 started","MakeMKV v1.18.3 linux(x64-release)"\n'
    'MSG:5021,131332,1,"This application version is too old.  Please download the latest version...","..."\n'
)


@pytest.fixture(autouse=True)
def _home_and_binary(tmp_path, monkeypatch):
    """Isolate $HOME and pretend makemkvcon exists."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(makemkv_updater, "get_makemkvcon_path", lambda: "/fake/makemkvcon")
    yield home


def _mock_run(monkeypatch, outputs):
    """subprocess.run mock returning queued outputs; records call envs + planted keys."""
    calls = []

    def fake_run(cmd, capture_output, text, check, timeout, env=None, **kw):
        home = (env or {}).get("HOME", "")
        planted = None
        conf = Path(home) / ".MakeMKV" / "settings.conf"
        if conf.exists():
            m = re.search(r'app_Key\s*=\s*"([^"]*)"', conf.read_text())
            planted = m.group(1) if m else None
        calls.append(SimpleNamespace(cmd=cmd, home=home, planted=planted))
        out = outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(stdout=out, stderr="", returncode=0)

    monkeypatch.setattr(makemkv_updater.subprocess, "run", fake_run)
    return calls


def test_valid_beta_key_commits_and_preserves_settings(monkeypatch, _home_and_binary):
    home = _home_and_binary
    real_conf = home / ".MakeMKV" / "settings.conf"
    real_conf.parent.mkdir(parents=True)
    real_conf.write_text('app_DefaultSelectionString = "+sel:all"\napp_Key = "T-oldkey"\n')

    calls = _mock_run(monkeypatch, [MSG_CLEAN, MSG_CLEAN])  # sandbox probe + post-commit verify
    ok, msg = makemkv_updater.set_registration_key(BETA_KEY)

    assert ok and "beta" in msg
    # Sandbox probe ran in an ISOLATED home with the candidate planted — never the real one.
    assert calls[0].home != str(home)
    assert calls[0].planted == BETA_KEY
    # Commit preserved unrelated settings and replaced app_Key.
    text = real_conf.read_text()
    assert 'app_DefaultSelectionString = "+sel:all"' in text
    assert f'app_Key = "{BETA_KEY}"' in text and "T-oldkey" not in text
    # Cache invalidated.
    assert makemkv_updater._reg_status_cache["ts"] == 0.0


def test_purchased_key_message_labels_type(monkeypatch, _home_and_binary):
    _mock_run(monkeypatch, [MSG_CLEAN, MSG_CLEAN])
    ok, msg = makemkv_updater.set_registration_key(PURCHASED_KEY)
    assert ok and "purchased" in msg


def test_invalid_key_raises_and_never_touches_real_settings(monkeypatch, _home_and_binary):
    home = _home_and_binary
    calls = _mock_run(monkeypatch, [MSG_INVALID])
    with pytest.raises(MakeMKVUpdateError, match="rejected this key"):
        makemkv_updater.set_registration_key(BETA_KEY)
    assert len(calls) == 1  # no commit, no post-verify
    assert not (home / ".MakeMKV" / "settings.conf").exists()


def test_invalid_beta_key_message_mentions_rotation(monkeypatch, _home_and_binary):
    _mock_run(monkeypatch, [MSG_INVALID])
    with pytest.raises(MakeMKVUpdateError, match="beta keys rotate"):
        makemkv_updater.set_registration_key(BETA_KEY)


def test_binary_expired_raises_update_guidance(monkeypatch, _home_and_binary):
    _mock_run(monkeypatch, [MSG_TOO_OLD])
    with pytest.raises(MakeMKVUpdateError, match="too old"):
        makemkv_updater.set_registration_key(BETA_KEY)


def test_malformed_key_rejected_without_running_makemkv(monkeypatch, _home_and_binary):
    calls = _mock_run(monkeypatch, [])
    for bad in ("", "   ", "notakey", "T-short", "X-" + "a" * 66, 'T-"quote' + "a" * 60):
        with pytest.raises(MakeMKVUpdateError):
            makemkv_updater.set_registration_key(bad)
    assert calls == []


def test_post_commit_regression_restores_prior_settings(monkeypatch, _home_and_binary):
    home = _home_and_binary
    real_conf = home / ".MakeMKV" / "settings.conf"
    real_conf.parent.mkdir(parents=True)
    prior = 'app_Key = "T-oldkey"\n'
    real_conf.write_text(prior)

    _mock_run(monkeypatch, [MSG_CLEAN, MSG_INVALID])  # sandbox ok, post-commit regresses
    with pytest.raises(MakeMKVUpdateError, match="restored"):
        makemkv_updater.set_registration_key(BETA_KEY)
    assert real_conf.read_text() == prior


def test_flaky_post_verify_does_not_fail_commit(monkeypatch, _home_and_binary):
    home = _home_and_binary
    _mock_run(monkeypatch, [MSG_CLEAN, subprocess.TimeoutExpired(cmd="x", timeout=90)])
    ok, _ = makemkv_updater.set_registration_key(BETA_KEY)
    assert ok
    assert BETA_KEY in (home / ".MakeMKV" / "settings.conf").read_text()


def test_probe_shared_library_error_reports_install_problem(monkeypatch, _home_and_binary):
    _mock_run(monkeypatch, ["error while loading shared libraries: libmakemkv.so"])
    with pytest.raises(MakeMKVUpdateError, match="not properly installed"):
        makemkv_updater.set_registration_key(BETA_KEY)
