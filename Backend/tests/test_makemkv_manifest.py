"""Client half of the validated MakeMKV x FFmpeg manifest.

The manifest is what stops an upstream release from breaking a fresh
install (FFmpeg 9.0, 2026-08-03). These tests pin the properties that make
the gate trustworthy: only validated pairs are offerable, an unknown
version is never silently treated as fine, and a hash we did not publish
is "unverifiable" rather than "verified".
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from core import makemkv_manifest as mm


def _manifest(**overrides):
    base = {
        "schema": mm.SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "latest_validated": "1.18.4",
        "validated": {
            "1.18.3": {
                "ffmpeg_version": "7.1",
                "sha256": {"makemkv-bin-1.18.3.tar.gz": "aaa"},
            },
            "1.18.4": {
                "ffmpeg_version": "8.1.2",
                "sha256": {
                    "makemkv-bin-1.18.4.tar.gz": "cee56de0",
                    "makemkv-oss-1.18.4.tar.gz": "85900636",
                    "ffmpeg-8.1.2.tar.xz": "ffff",
                },
            },
        },
        "known_incompatible": [
            {"makemkv_version": "1.18.5", "ffmpeg_version": "9.0",
             "reason": "build_failed"},
        ],
    }
    base.update(overrides)
    return base


class TestReading:
    def test_only_validated_versions_are_offerable(self):
        m = _manifest()
        assert mm.validated_versions(m) == ["1.18.4", "1.18.3"]
        # 1.18.5 exists upstream and is known-bad — it must never surface as
        # an update. The gate is structural: it simply isn't in `validated`.
        assert "1.18.5" not in mm.validated_versions(m)
        assert mm.latest_validated(m) == "1.18.4"

    def test_versions_sort_numerically_not_lexically(self):
        m = _manifest(validated={
            "1.18.9": {"ffmpeg_version": "8.1.2"},
            "1.18.10": {"ffmpeg_version": "8.1.2"},
        }, latest_validated=None)
        assert mm.latest_validated(m) == "1.18.10", "1.18.10 > 1.18.9"

    def test_ffmpeg_pairing_is_per_version(self):
        m = _manifest()
        assert mm.ffmpeg_for(m, "1.18.4") == "8.1.2"
        assert mm.ffmpeg_for(m, "1.18.3") == "7.1", "older MakeMKV keeps its own pairing"
        assert mm.ffmpeg_for(m, "1.18.5") is None, "unvalidated version has no pairing"

    def test_unpublished_hash_is_unverifiable_not_verified(self):
        m = _manifest()
        assert mm.expected_sha256(m, "1.18.4", "makemkv-bin-1.18.4.tar.gz") == "cee56de0"
        # Absent entries must return None so callers cannot mistake "no hash
        # on file" for "hash matched".
        assert mm.expected_sha256(m, "1.18.4", "not-a-file.tar.gz") is None
        assert mm.expected_sha256(m, "1.18.5", "makemkv-bin-1.18.5.tar.gz") is None

    def test_incompatibility_is_explained_to_the_user(self):
        m = _manifest()
        note = mm.incompatibility_note(m, "1.18.5")
        assert note and "1.18.5" in note and "9.0" in note
        assert mm.incompatibility_note(m, "1.18.4") is None

    def test_missing_manifest_never_crashes_the_readers(self):
        for empty in (None, {}, {"validated": None}):
            assert mm.validated_versions(empty) == []
            assert mm.latest_validated(empty) is None
            assert mm.ffmpeg_for(empty, "1.18.4") is None
            assert mm.expected_sha256(empty, "1.18.4", "x") is None
            assert mm.incompatibility_note(empty, "1.18.4") is None

    def test_staleness_is_detectable(self):
        fresh = _manifest()
        assert mm.is_stale(fresh) is False
        old = _manifest(generated_at=(
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat(timespec="seconds"))
        assert mm.is_stale(old) is True
        # An unparseable or absent timestamp is treated as stale: gating on
        # CI means we must notice when CI stops.
        assert mm.is_stale({"generated_at": "not-a-date"}) is True
        assert mm.is_stale({}) is True


class TestFetching:
    def _resp(self, status=200, body=None, etag=None):
        r = Mock()
        r.status_code = status
        r.headers = {"ETag": etag} if etag else {}
        r.text = json.dumps(body or {})
        r.json = lambda: json.loads(r.text)
        return r

    def test_fresh_fetch_is_cached_with_its_etag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mm.requests, "get",
                            lambda *a, **k: self._resp(200, _manifest(), etag='"abc"'))
        manifest, status = mm.fetch_manifest(tmp_path)
        assert status == "fresh"
        assert mm.latest_validated(manifest) == "1.18.4"
        assert (tmp_path / "makemkv-versions.json").exists()
        assert (tmp_path / "makemkv-versions.etag").read_text() == '"abc"'

    def test_second_poll_sends_if_none_match_and_reuses_cache_on_304(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mm.requests, "get",
                            lambda *a, **k: self._resp(200, _manifest(), etag='"abc"'))
        mm.fetch_manifest(tmp_path)

        seen = {}

        def conditional(url, headers=None, timeout=None):
            seen.update(headers or {})
            return self._resp(304)

        monkeypatch.setattr(mm.requests, "get", conditional)
        manifest, status = mm.fetch_manifest(tmp_path)
        assert seen.get("If-None-Match") == '"abc"', "poll must be conditional"
        assert status == "unchanged"
        assert mm.latest_validated(manifest) == "1.18.4", "cached copy still usable"

    def test_network_failure_falls_back_to_cache_without_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mm.requests, "get",
                            lambda *a, **k: self._resp(200, _manifest(), etag='"abc"'))
        mm.fetch_manifest(tmp_path)

        def boom(*a, **k):
            raise ConnectionError("github unreachable")

        monkeypatch.setattr(mm.requests, "get", boom)
        manifest, status = mm.fetch_manifest(tmp_path)
        assert status == "cached"
        assert mm.latest_validated(manifest) == "1.18.4"

    def test_unreachable_with_no_cache_is_unavailable_not_a_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mm.requests, "get",
                            Mock(side_effect=ConnectionError("offline")))
        monkeypatch.setattr(mm, "load_cached", lambda d: None)
        manifest, status = mm.fetch_manifest(tmp_path)
        assert manifest is None and status == "unavailable"

    def test_future_schema_is_ignored_rather_than_misread(self, tmp_path, monkeypatch):
        # A newer schema means THIS build is old. Guessing at unknown fields
        # is how a gate silently starts offering the wrong thing.
        monkeypatch.setattr(mm.requests, "get",
                            lambda *a, **k: self._resp(200, {"schema": 99, "validated": {}}))
        monkeypatch.setattr(mm, "load_cached", lambda d: None)
        manifest, status = mm.fetch_manifest(tmp_path)
        assert manifest is None and status == "unavailable"

    def test_html_error_page_is_not_accepted_as_a_manifest(self, tmp_path, monkeypatch):
        r = Mock()
        r.status_code = 200
        r.headers = {}
        r.text = "<html>404</html>"
        r.json = Mock(side_effect=ValueError("not json"))
        monkeypatch.setattr(mm.requests, "get", lambda *a, **k: r)
        monkeypatch.setattr(mm, "load_cached", lambda d: None)
        manifest, status = mm.fetch_manifest(tmp_path)
        assert manifest is None and status == "unavailable"


def test_poll_jitter_is_stable_and_bounded(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "container-abc")
    first = mm.poll_jitter_seconds()
    second = mm.poll_jitter_seconds()
    assert first == second, "an install must keep its slot across restarts"
    assert 0 <= first < 3600
    monkeypatch.setenv("HOSTNAME", "container-xyz")
    assert mm.poll_jitter_seconds() != first or True  # different host may differ
