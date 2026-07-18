"""Unit tests for core.mkv_identity — read-only Matroska Segment UID helper.

These tests do not exercise the real ``mkvmerge`` binary; the read path is
covered via ``subprocess.run`` patches so the suite runs without the
``mkvtoolnix`` package installed on the host. The Dockerfile installs
``mkvtoolnix`` and the manual smoke-test in #448's PR doc verifies the live
path end-to-end.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core import mkv_identity


# ────────────────────────────────────────────────────────────────
# read_segment_uid
# ────────────────────────────────────────────────────────────────

SAMPLE_UID = "0123456789abcdef0123456789abcdef"


def _mkvmerge_ok(uid: str = SAMPLE_UID) -> SimpleNamespace:
    payload = {"container": {"properties": {"segment_uid": uid}}}
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def test_read_segment_uid_happy_path(monkeypatch):
    monkeypatch.setattr(mkv_identity.shutil, "which", lambda _: "/usr/bin/mkvmerge")
    monkeypatch.setattr(mkv_identity.subprocess, "run", lambda *a, **k: _mkvmerge_ok())
    assert mkv_identity.read_segment_uid("/tmp/whatever.mkv") == SAMPLE_UID


def test_read_segment_uid_missing_binary(monkeypatch, caplog):
    monkeypatch.setattr(mkv_identity.shutil, "which", lambda _: None)
    with caplog.at_level("WARNING"):
        assert mkv_identity.read_segment_uid("/tmp/whatever.mkv") is None
    assert any("mkvmerge binary not on PATH" in r.message for r in caplog.records)


def test_read_segment_uid_nonzero_exit(monkeypatch, caplog):
    monkeypatch.setattr(mkv_identity.shutil, "which", lambda _: "/usr/bin/mkvmerge")
    fake = SimpleNamespace(returncode=2, stdout="", stderr="cannot open file")
    monkeypatch.setattr(mkv_identity.subprocess, "run", lambda *a, **k: fake)
    with caplog.at_level("WARNING"):
        assert mkv_identity.read_segment_uid("/tmp/missing.mkv") is None
    assert any("exit=2" in r.message for r in caplog.records)


def test_read_segment_uid_malformed_json(monkeypatch):
    monkeypatch.setattr(mkv_identity.shutil, "which", lambda _: "/usr/bin/mkvmerge")
    fake = SimpleNamespace(returncode=0, stdout="not json at all", stderr="")
    monkeypatch.setattr(mkv_identity.subprocess, "run", lambda *a, **k: fake)
    assert mkv_identity.read_segment_uid("/tmp/whatever.mkv") is None


def test_read_segment_uid_field_missing(monkeypatch):
    monkeypatch.setattr(mkv_identity.shutil, "which", lambda _: "/usr/bin/mkvmerge")
    payload = {"container": {"properties": {}}}  # no segment_uid
    fake = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(mkv_identity.subprocess, "run", lambda *a, **k: fake)
    assert mkv_identity.read_segment_uid("/tmp/whatever.mkv") is None


def test_read_segment_uid_subprocess_raises(monkeypatch):
    monkeypatch.setattr(mkv_identity.shutil, "which", lambda _: "/usr/bin/mkvmerge")

    def boom(*a, **k):
        raise OSError("EACCES")

    monkeypatch.setattr(mkv_identity.subprocess, "run", boom)
    assert mkv_identity.read_segment_uid("/tmp/whatever.mkv") is None


# ────────────────────────────────────────────────────────────────
# capture_segment_uids_for_titles
# ────────────────────────────────────────────────────────────────


def test_capture_segment_uids_writes_column(test_db, monkeypatch):
    """End-to-end: given a disc with two titles and a post_paths dict, each
    DiscTitle.segment_uid is populated from the per-file read."""
    from api import models

    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        tid_a, tid_b = str(uuid.uuid4()), str(uuid.uuid4())
        session.add(models.DiscTitle(id=tid_a, disc_id=disc_id, title="A", source_file="00001.mpls"))
        session.add(models.DiscTitle(id=tid_b, disc_id=disc_id, title="B", source_file="00002.mpls"))
        session.commit()
    finally:
        session.close()

    uids = {
        "/jobs/J/transient/A.mkv": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "/jobs/J/transient/B.mkv": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    }
    monkeypatch.setattr(mkv_identity, "read_segment_uid", lambda p: uids.get(p))

    session = test_db()
    try:
        updated = mkv_identity.capture_segment_uids_for_titles(
            session,
            disc_id,
            {tid_a: "A.mkv", tid_b: "B.mkv"},
            "/jobs/J/transient",
        )
        session.commit()
    finally:
        session.close()

    assert updated == 2
    session = test_db()
    try:
        a = session.query(models.DiscTitle).filter_by(id=tid_a).first()
        b = session.query(models.DiscTitle).filter_by(id=tid_b).first()
        assert a.segment_uid == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        assert b.segment_uid == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    finally:
        session.close()


def test_capture_segment_uids_skips_none_reads(test_db, monkeypatch):
    """When read_segment_uid returns None for a file (unreadable, missing
    UID, no binary), the row is left with segment_uid=NULL — capture is
    additive, never destructive."""
    from api import models

    session = test_db()
    try:
        disc_id = str(uuid.uuid4())
        session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
        tid = str(uuid.uuid4())
        session.add(models.DiscTitle(id=tid, disc_id=disc_id, title="A", source_file="00001.mpls"))
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(mkv_identity, "read_segment_uid", lambda p: None)

    session = test_db()
    try:
        updated = mkv_identity.capture_segment_uids_for_titles(
            session, disc_id, {tid: "A.mkv"}, "/jobs/J/transient",
        )
        session.commit()
    finally:
        session.close()

    assert updated == 0
    session = test_db()
    try:
        row = session.query(models.DiscTitle).filter_by(id=tid).first()
        assert row.segment_uid is None
    finally:
        session.close()
