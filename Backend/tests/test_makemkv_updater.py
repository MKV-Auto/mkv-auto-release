import json
from datetime import datetime, timezone
import hashlib
import urllib.error
from unittest.mock import Mock

import pytest
import requests

from core import makemkv_updater


class _RunResult:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_strings(monkeypatch, output: str):
    def fake_run(*_args, **_kwargs):
        return _RunResult(stdout=output, stderr="", returncode=0)

    monkeypatch.setattr(makemkv_updater.subprocess, "run", fake_run)
    monkeypatch.setattr(
        makemkv_updater.shutil,
        "which",
        lambda name: "/usr/bin/strings" if name == "strings" else None,
    )


def test_get_installed_version_selects_most_frequent(monkeypatch, tmp_path):
    binary = tmp_path / "makemkvcon"
    binary.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(makemkv_updater, "get_makemkvcon_path", lambda: str(binary))
    _patch_strings(monkeypatch, "v1.18.2 v1.1.0 v1.18.2 v1.18.2")

    assert makemkv_updater.get_installed_version() == "1.18.2"


def test_get_installed_version_tie_breaks_by_semver(monkeypatch, tmp_path):
    binary = tmp_path / "makemkvcon"
    binary.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(makemkv_updater, "get_makemkvcon_path", lambda: str(binary))
    _patch_strings(monkeypatch, "v1.18.1 v1.18.2")

    assert makemkv_updater.get_installed_version() == "1.18.2"


def test_get_installed_version_prefers_frequency_over_semver(monkeypatch, tmp_path):
    binary = tmp_path / "makemkvcon"
    binary.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(makemkv_updater, "get_makemkvcon_path", lambda: str(binary))
    _patch_strings(monkeypatch, "v1.18.2 v9.99.3 v1.18.2 v1.1.0")

    assert makemkv_updater.get_installed_version() == "1.18.2"


def test_get_installed_version_ignores_unprefixed_versions(monkeypatch, tmp_path):
    binary = tmp_path / "makemkvcon"
    binary.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(makemkv_updater, "get_makemkvcon_path", lambda: str(binary))
    _patch_strings(monkeypatch, "1.18.2 9.99.3 9.99.3 v1.18.2")

    assert makemkv_updater.get_installed_version() == "1.18.2"


def test_get_installed_version_returns_none_when_no_versions(monkeypatch, tmp_path):
    binary = tmp_path / "makemkvcon"
    binary.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(makemkv_updater, "get_makemkvcon_path", lambda: str(binary))
    _patch_strings(monkeypatch, "no version here")

    assert makemkv_updater.get_installed_version() is None


def test_get_makemkvcon_metadata_includes_hash_and_mtime(monkeypatch, tmp_path):
    binary = tmp_path / "makemkvcon"
    content = b"makemkv-binary"
    binary.write_bytes(content)
    monkeypatch.setattr(makemkv_updater, "get_makemkvcon_path", lambda: str(binary))

    meta = makemkv_updater.get_makemkvcon_metadata()

    assert meta["binary_path"] == str(binary)
    assert meta["resolved_path"] == str(binary.resolve())
    assert meta["binary_sha256"] == hashlib.sha256(content).hexdigest()
    assert meta["binary_mtime"] is not None


def test_download_http_403_appends_to_logs_and_raises_makemkv_update_error(
    monkeypatch, tmp_path
):
    """When _download gets HTTP 403, logs get failure lines and MakeMKVUpdateError is raised."""
    url = "https://www.makemkv.com/download/makemkv-bin-1.18.2.tar.gz"
    dest = tmp_path / "out.tar.gz"
    logs = []

    def urlopen_raising_403(_request_url, timeout=None):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(makemkv_updater.urllib.request, "urlopen", urlopen_raising_403)

    with pytest.raises(makemkv_updater.MakeMKVUpdateError) as excinfo:
        makemkv_updater._download(url, dest, logs)

    assert "403" in excinfo.value.args[0]
    assert "Forbidden" in excinfo.value.args[0]
    assert url in excinfo.value.args[0]
    assert any("403" in line or "Forbidden" in line for line in logs)
    assert any(url in line for line in logs)


def test_run_failure_logs_to_logger(monkeypatch, caplog):
    """When _run gets non-zero exit, log.error is called with command and stderr."""
    from unittest.mock import Mock

    logs = []
    cmd = ["false"]
    stderr_text = "failure message"

    # _run uses subprocess.Popen, not subprocess.run; mock Popen to return a process
    # that has already "exited" with returncode 1 and stderr content
    mock_stdout = Mock()
    mock_stdout.read.return_value = ""
    mock_stderr = Mock()
    mock_stderr.read.return_value = stderr_text
    mock_process = Mock()
    mock_process.poll.return_value = 1
    mock_process.stdout = mock_stdout
    mock_process.stderr = mock_stderr
    mock_process.returncode = 1

    def fake_popen(*args, **kwargs):
        return mock_process

    monkeypatch.setattr(makemkv_updater.subprocess, "Popen", fake_popen)

    with pytest.raises(makemkv_updater.MakeMKVUpdateError):
        makemkv_updater._run(cmd, logs=logs)

    err_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(err_records) >= 1
    err_msg = err_records[0].message
    assert "false" in err_msg
    assert "1" in err_msg
    assert "failure message" in err_msg


def test_fetch_latest_version_uses_wayback_when_primary_fails(monkeypatch):
    """When primary URLs timeout/fail, fetch_latest_version uses Wayback and returns version."""
    primary_calls = []

    def failing_get(url, timeout=None):
        primary_calls.append(url)
        raise requests.RequestException("timeout")

    monkeypatch.setattr(requests, "get", failing_get)

    # First two calls are primary (download page, forum); then we try Wayback.
    # Mock _fetch_versions_from_wayback to return a version so we don't need real archive.org
    def wayback_returns_versions():
        return ["1.18.3"]

    monkeypatch.setattr(
        makemkv_updater, "_fetch_versions_from_wayback", wayback_returns_versions
    )

    result = makemkv_updater.fetch_latest_version()
    assert result == "1.18.3"
    assert len(primary_calls) == 2  # both primary URLs tried


def test_fetch_latest_version_raises_when_primary_and_wayback_fail(monkeypatch):
    """When primary and Wayback both fail, MakeMKVUpdateError is raised."""
    def failing_get(url, timeout=None):
        raise requests.RequestException("timeout")

    monkeypatch.setattr(requests, "get", failing_get)

    def wayback_returns_empty():
        return []

    monkeypatch.setattr(
        makemkv_updater, "_fetch_versions_from_wayback", wayback_returns_empty
    )

    with pytest.raises(makemkv_updater.MakeMKVUpdateError) as excinfo:
        makemkv_updater.fetch_latest_version()
    assert "primary" in excinfo.value.args[0].lower() or "wayback" in excinfo.value.args[0].lower()


@pytest.mark.xfail(reason="staging baseline fail; tracked in #406", strict=True)
def test_download_with_fallback_uses_wayback_when_primary_fails(monkeypatch, tmp_path):
    """When primary download fails, _download_with_fallback tries Wayback and succeeds."""
    url = "https://www.makemkv.com/download/makemkv-bin-1.18.2.tar.gz"
    dest = tmp_path / "out.tar.gz"
    logs = []

    # First urlopen call (primary) raises; second call (wayback) returns readable content
    call_count = [0]

    def urlopen_side_effect(request, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            raise urllib.error.URLError("timeout")
        # Second call is for wayback URL: return a response that read() returns content then b""
        chunks = [b"fake-tarball-content", b""]

        class FakeResponse:
            def getheader(self, name):
                return None

            def read(self, size=-1):
                if chunks:
                    return chunks.pop(0)
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        return FakeResponse()

    monkeypatch.setattr(
        makemkv_updater.urllib.request, "urlopen", urlopen_side_effect
    )
    wayback_url = "https://web.archive.org/web/20200101000000id_/" + url
    # Patch the capture-list resolver (the seam _download_with_fallback uses).
    # Stubbing the old single-snapshot helper let this test reach the live
    # archive.org index instead of the stub.
    monkeypatch.setattr(
        makemkv_updater, "_wayback_snapshot_urls_for",
        lambda u, **kwargs: [wayback_url] if u == url else [],
    )

    makemkv_updater._download_with_fallback(url, dest, logs)

    assert dest.read_bytes() == b"fake-tarball-content"
    assert any("Wayback" in line for line in logs)


def test_download_with_fallback_raises_when_primary_and_wayback_fail(
    monkeypatch, tmp_path
):
    """When primary fails and no Wayback snapshot exists, MakeMKVUpdateError is raised."""
    url = "https://www.makemkv.com/download/makemkv-bin-1.18.2.tar.gz"
    dest = tmp_path / "out.tar.gz"
    logs = []

    def urlopen_fail(request, timeout=None):
        raise urllib.error.URLError("timeout")

    monkeypatch.setattr(makemkv_updater.urllib.request, "urlopen", urlopen_fail)
    monkeypatch.setattr(
        makemkv_updater, "_wayback_snapshot_urls_for", lambda u, **kwargs: []
    )

    with pytest.raises(makemkv_updater.MakeMKVUpdateError) as excinfo:
        makemkv_updater._download_with_fallback(url, dest, logs)
    assert "Wayback" in excinfo.value.args[0] or "Primary" in excinfo.value.args[0]


# ── FFmpeg compatibility ceiling ────────────────────────────────────────────
# MakeMKV's libffabi uses AVCodec fields removed in FFmpeg 9.0
# (ch_layouts / sample_fmts / supported_samplerates). Before the ceiling,
# fetch_latest_ffmpeg() took the newest tarball unconditionally, so the day
# FFmpeg 9.0 was published (2026-08-03) every fresh install started failing
# with "error: 'AVCodec' has no member named 'ch_layouts'" — with no change
# on our side. These tests pin the gate so the next major can't do it again.

def _patch_releases_page(monkeypatch, versions):
    """Serve a fake ffmpeg.org/releases listing containing `versions`."""
    body = "".join(
        f'<a href="ffmpeg-{v}.tar.xz">ffmpeg-{v}.tar.xz</a>' for v in versions
    )
    resp = Mock()
    resp.text = body
    resp.raise_for_status = Mock()
    monkeypatch.setattr(makemkv_updater.requests, "get", lambda *a, **k: resp)


def test_ffmpeg_resolver_skips_versions_at_or_above_the_ceiling(monkeypatch):
    # The real-world case: 9.0 is newest, 8.1.2 is the newest we can build.
    _patch_releases_page(monkeypatch, ["7.1", "8.0", "8.1.2", "9.0"])
    assert makemkv_updater.fetch_latest_ffmpeg() == "8.1.2"


def test_ffmpeg_resolver_still_tracks_newest_point_release_below_ceiling(monkeypatch):
    # The ceiling gates the MAJOR only — 8.x security/point releases must
    # keep rolling in automatically, or pinning becomes its own liability.
    _patch_releases_page(monkeypatch, ["8.0", "8.1", "8.1.2", "8.2"])
    assert makemkv_updater.fetch_latest_ffmpeg() == "8.2"


def test_ffmpeg_resolver_ignores_a_future_major(monkeypatch):
    # FFmpeg 10 must not silently become the build target either.
    _patch_releases_page(monkeypatch, ["8.1.2", "9.0", "9.3", "10.0"])
    assert makemkv_updater.fetch_latest_ffmpeg() == "8.1.2"


def test_ffmpeg_resolver_returns_none_when_everything_is_above_the_ceiling(monkeypatch):
    # Better to resolve nothing (and log loudly) than to hand MakeMKV a
    # version we know cannot compile it.
    _patch_releases_page(monkeypatch, ["9.0", "9.1"])
    assert makemkv_updater.fetch_latest_ffmpeg() is None


def test_ffmpeg_version_support_gate():
    supported = makemkv_updater.is_ffmpeg_version_supported
    assert supported("8.1.2") is True
    assert supported("7.1") is True
    assert supported("9.0") is False       # the version that broke the build
    assert supported("9.0.1") is False
    assert supported("10.0") is False
    # Unparseable input is rejected, never assumed good.
    assert supported("not-a-version") is False
    assert supported("") is False


def test_ffmpeg_ceiling_excludes_nine_zero():
    # Guards the constant itself: if someone raises this, they must also
    # verify MakeMKV builds against the new major (see the comment on
    # FFMPEG_MAX_VERSION_EXCLUSIVE). 9.0 is where ffabi.c stops compiling.
    assert makemkv_updater.FFMPEG_MAX_VERSION_EXCLUSIVE <= (9, 0)


def test_ffmpeg_version_key_is_width_normalized():
    # A bare major must not compare below its own x.0 release: plain tuple
    # comparison makes the shorter tuple smaller, which would let an
    # incompatible major slip under the ceiling.
    assert makemkv_updater.ffmpeg_version_key("9") == (9, 0, 0)
    assert makemkv_updater.ffmpeg_version_key("8.1") == (8, 1, 0)
    assert makemkv_updater.is_ffmpeg_version_supported("9") is False


# ── Wayback CDX fallback ────────────────────────────────────────────────────
# The tarball download falls back to archive.org when makemkv.com is down
# (it is, as of 2026-08-03, serving Cloudflare 525s). The old resolver asked
# the availability API for the single "closest" capture and treated ANY
# exception as "not archived" — so a routine HTTP 429 was reported to users
# as "no Wayback snapshot for URL ...", for files that were in fact archived.

CDX_HEADER = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]


def _cdx_row(timestamp, length, status="200", digest="D1"):
    return ["com,makemkv)/x", timestamp, "https://www.makemkv.com/download/x.tar.gz",
            "application/octet-stream", status, digest, str(length)]


def _patch_cdx(monkeypatch, *, body=None, status=200, text=None, boom=None):
    # Backoff is real in production; tests must not actually wait for it.
    monkeypatch.setattr(makemkv_updater.time, "sleep", lambda s: None)

    def fake_get(url, **kwargs):
        if boom:
            raise boom
        resp = Mock()
        resp.status_code = status
        resp.headers = {}
        resp.text = text if text is not None else json.dumps(body or [])
        resp.json = lambda: json.loads(resp.text)
        resp.raise_for_status = Mock()
        return resp
    monkeypatch.setattr(makemkv_updater.requests, "get", fake_get)


def test_cdx_returns_captures_newest_first(monkeypatch):
    _patch_cdx(monkeypatch, body=[
        CDX_HEADER,
        _cdx_row("20260101000000", 18_000_000, digest="OLD"),
        _cdx_row("20260617084255", 18_600_000, digest="NEW"),
    ])
    urls = makemkv_updater._wayback_snapshot_urls_for("https://www.makemkv.com/download/x.tar.gz")
    assert len(urls) == 2
    assert "20260617084255id_/" in urls[0], "newest capture must be tried first"
    assert "20260101000000id_/" in urls[1]


def test_cdx_filters_out_undersized_junk_captures(monkeypatch):
    # Real case: both 1.18.4 tarballs have a 1,027-byte capture from
    # 2026-07-11, recorded while makemkv.com was already failing, sitting
    # next to a good capture from June. Spending a download on the dud (and
    # failing verification) is avoidable — the index already told us.
    _patch_cdx(monkeypatch, body=[
        CDX_HEADER,
        _cdx_row("20260617084255", 18_600_000, digest="GOOD"),
        _cdx_row("20260711012600", 1_027, digest="JUNK"),
    ])
    urls = makemkv_updater._wayback_snapshot_urls_for(
        "https://www.makemkv.com/download/x.tar.gz",
        min_bytes=makemkv_updater.WAYBACK_MIN_TARBALL_BYTES,
    )
    assert len(urls) == 1
    assert "20260617084255id_/" in urls[0]


def test_rate_limit_is_a_lookup_error_not_an_empty_result(monkeypatch):
    # The bug this whole path exists to fix: 429 must never be reported as
    # "this file isn't archived".
    _patch_cdx(monkeypatch, status=429, text="<html><body><h1>429 Too Many Requests</h1>")
    with pytest.raises(makemkv_updater.WaybackLookupError):
        makemkv_updater._wayback_snapshot_urls_for("https://www.makemkv.com/download/x.tar.gz")


def test_non_json_body_is_a_lookup_error(monkeypatch):
    _patch_cdx(monkeypatch, status=200, text="<html>we are down</html>")
    with pytest.raises(makemkv_updater.WaybackLookupError):
        makemkv_updater._wayback_snapshot_urls_for("https://www.makemkv.com/download/x.tar.gz")


def test_empty_index_is_genuinely_no_captures(monkeypatch):
    # CDX answers with an empty body when it has nothing. That IS an answer,
    # and must not raise — the caller should report "not archived".
    _patch_cdx(monkeypatch, status=200, text="")
    assert makemkv_updater._wayback_snapshot_urls_for("https://www.makemkv.com/download/x.tar.gz") == []


def test_cdx_failure_falls_back_to_availability_api(monkeypatch):
    # A CDX outage degrades to the previous single-snapshot behaviour
    # rather than to nothing at all.
    calls = {"n": 0}

    def fake_get(url, **kwargs):
        calls["n"] += 1
        resp = Mock()
        if makemkv_updater.WAYBACK_CDX_API in url:
            resp.status_code = 503
            resp.text = "unavailable"
            return resp
        resp.status_code = 200
        resp.raise_for_status = Mock()
        resp.json = lambda: {"archived_snapshots": {"closest": {
            "available": True, "timestamp": "20260617084255"}}}
        return resp

    monkeypatch.setattr(makemkv_updater.requests, "get", fake_get)
    urls = makemkv_updater._wayback_snapshot_urls_for("https://www.makemkv.com/download/x.tar.gz")
    assert calls["n"] == 2
    assert len(urls) == 1 and "20260617084255id_/" in urls[0]


def test_download_walks_to_an_older_capture_when_the_newest_is_bad(monkeypatch, tmp_path):
    """A snapshot existing is not the same as the file being usable."""
    url = "https://www.makemkv.com/download/makemkv-bin-1.18.4.tar.gz"
    dest = tmp_path / "makemkv-bin-1.18.4.tar.gz"

    monkeypatch.setattr(
        makemkv_updater, "_wayback_snapshot_urls_for",
        lambda u, **k: ["https://web.archive.org/web/NEWid_/" + u,
                        "https://web.archive.org/web/OLDid_/" + u],
    )
    attempted = []

    def fake_download(u, d, logs, **kwargs):
        attempted.append(u)
        if u.startswith("https://www.makemkv.com"):
            raise makemkv_updater.MakeMKVUpdateError("primary down (525)")
        d.write_bytes(b"payload")

    monkeypatch.setattr(makemkv_updater, "_download", fake_download)
    monkeypatch.setattr(makemkv_updater, "_unwrap_double_gzip_if_needed", lambda *a, **k: None)
    # First capture verifies false, second true.
    verdicts = iter([False, True])
    monkeypatch.setattr(makemkv_updater, "_verify_tarball_gz", lambda *a, **k: next(verdicts))

    makemkv_updater._download_with_fallback(url, dest, [])

    assert len(attempted) == 3, "primary, then both captures"
    assert "NEWid_" in attempted[1] and "OLDid_" in attempted[2]


def test_download_reports_rate_limit_distinctly_from_not_archived(monkeypatch, tmp_path):
    url = "https://www.makemkv.com/download/makemkv-bin-1.18.4.tar.gz"
    dest = tmp_path / "makemkv-bin-1.18.4.tar.gz"

    def boom(u, **k):
        raise makemkv_updater.WaybackLookupError("CDX rate-limited (HTTP 429)")

    monkeypatch.setattr(makemkv_updater, "_wayback_snapshot_urls_for", boom)
    monkeypatch.setattr(
        makemkv_updater, "_download",
        Mock(side_effect=makemkv_updater.MakeMKVUpdateError("primary down")),
    )
    with pytest.raises(makemkv_updater.MakeMKVUpdateError) as exc:
        makemkv_updater._download_with_fallback(url, dest, [])
    message = str(exc.value)
    assert "could not be queried" in message
    assert "no Wayback snapshot" not in message, "must not claim the file is unarchived"


def test_cdx_retries_rate_limits_before_giving_up(monkeypatch):
    """429 is 'come back shortly', not an answer — retry with backoff."""
    calls = {"n": 0}

    def fake_get(url, **kwargs):
        calls["n"] += 1
        resp = Mock()
        resp.headers = {}
        if calls["n"] <= 2:
            resp.status_code = 429
            resp.text = "<html>429</html>"
            return resp
        resp.status_code = 200
        resp.text = json.dumps([CDX_HEADER, _cdx_row("20260617084255", 18_600_000)])
        resp.json = lambda: json.loads(resp.text)
        return resp

    monkeypatch.setattr(makemkv_updater.requests, "get", fake_get)
    monkeypatch.setattr(makemkv_updater.time, "sleep", lambda s: None)  # no real waiting

    urls = makemkv_updater._wayback_snapshot_urls_for("https://www.makemkv.com/download/x.tar.gz")
    assert calls["n"] == 3, "should retry twice then succeed"
    assert len(urls) == 1


def test_cdx_gives_up_after_retry_budget(monkeypatch):
    def always_429(url, **kwargs):
        resp = Mock()
        resp.status_code = 429
        resp.text = "<html>429</html>"
        resp.headers = {}
        return resp

    monkeypatch.setattr(makemkv_updater.requests, "get", always_429)
    monkeypatch.setattr(makemkv_updater.time, "sleep", lambda s: None)
    with pytest.raises(makemkv_updater.WaybackLookupError) as exc:
        makemkv_updater._wayback_snapshot_urls_for("https://www.makemkv.com/download/x.tar.gz")
    assert "429" in str(exc.value), "the rate limit must survive into the message"


# ── Manifest-driven pinning and verification ────────────────────────────────

def _write_manifest(tmp_path, monkeypatch, manifest):
    cache = tmp_path / "manifest-cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "makemkv-versions.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(makemkv_updater, "get_mkvauto_tmp", lambda: tmp_path)
    return cache


def test_ffmpeg_pinned_from_the_validated_pairing(monkeypatch, tmp_path):
    """A pair CI actually built beats the global ceiling guess."""
    _write_manifest(tmp_path, monkeypatch, {
        "schema": 1, "validated": {"1.18.4": {"ffmpeg_version": "7.1"}},
    })
    # The ceiling would pick something newer; the manifest must win.
    monkeypatch.setattr(makemkv_updater, "fetch_latest_ffmpeg", lambda: "8.1.2")
    assert makemkv_updater.resolve_ffmpeg_for_build("1.18.4") == "7.1"


def test_ffmpeg_falls_back_to_the_ceiling_without_a_pairing(monkeypatch, tmp_path):
    # Offline installs and brand-new MakeMKV versions land here; the ceiling
    # survives precisely for this case.
    _write_manifest(tmp_path, monkeypatch, {"schema": 1, "validated": {}})
    monkeypatch.setattr(makemkv_updater, "fetch_latest_ffmpeg", lambda: "8.1.2")
    assert makemkv_updater.resolve_ffmpeg_for_build("1.18.9") == "8.1.2"


def test_download_refuses_an_artifact_that_fails_its_validated_hash(monkeypatch, tmp_path):
    """The payoff of build-validated hashes: a substituted or corrupted
    tarball never reaches the compiler."""
    _write_manifest(tmp_path, monkeypatch, {
        "schema": 1,
        "validated": {"1.18.4": {
            "ffmpeg_version": "8.1.2",
            "sha256": {"makemkv-bin-1.18.4.tar.gz": "0" * 64},
        }},
    })
    tar = tmp_path / "makemkv-bin-1.18.4.tar.gz"
    tar.write_bytes(b"not the tarball we validated")

    with pytest.raises(makemkv_updater.MakeMKVUpdateError) as exc:
        makemkv_updater._verify_against_manifest("1.18.4", [tar], [])
    assert "validated build hash" in str(exc.value)
    assert not tar.exists(), "the rejected artifact must not be left on disk"


def test_download_accepts_a_matching_hash(monkeypatch, tmp_path):
    tar = tmp_path / "makemkv-bin-1.18.4.tar.gz"
    tar.write_bytes(b"the real thing")
    digest = hashlib.sha256(b"the real thing").hexdigest()
    _write_manifest(tmp_path, monkeypatch, {
        "schema": 1,
        "validated": {"1.18.4": {"sha256": {"makemkv-bin-1.18.4.tar.gz": digest}}},
    })
    logs = []
    makemkv_updater._verify_against_manifest("1.18.4", [tar], logs)
    assert tar.exists()
    assert any("Verified" in line for line in logs)


def test_unpublished_hash_is_unverified_not_rejected(monkeypatch, tmp_path):
    # Refusing here would gate users on our CI for no security gain — we
    # have nothing to compare against either way.
    _write_manifest(tmp_path, monkeypatch, {"schema": 1, "validated": {}})
    tar = tmp_path / "makemkv-bin-9.9.9.tar.gz"
    tar.write_bytes(b"brand new version")
    logs = []
    makemkv_updater._verify_against_manifest("9.9.9", [tar], logs)
    assert tar.exists()
    assert any("unverified" in line for line in logs)


def test_update_detection_offers_only_validated_versions(monkeypatch, tmp_path):
    """Gating is structural: an unbuilt version is not in the manifest, so
    there is no policy check that can be got wrong."""
    monkeypatch.setattr(makemkv_updater, "get_mkvauto_tmp", lambda: tmp_path)
    monkeypatch.setattr(
        makemkv_updater, "fetch_latest_version",
        lambda: (_ for _ in ()).throw(AssertionError("must not scrape upstream")),
    )
    from core import makemkv_manifest as mf
    monkeypatch.setattr(mf, "fetch_manifest", lambda *a, **k: ({
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "latest_validated": "1.18.4",
        "validated": {"1.18.4": {"ffmpeg_version": "8.1.2"}},
        "known_incompatible": [
            {"makemkv_version": "1.18.5", "ffmpeg_version": "9.0", "reason": "build_failed"},
        ],
    }, "fresh"))

    version, note, source = makemkv_updater.resolve_offerable_version()
    assert version == "1.18.4"
    assert source == "manifest"
    # The held-back version is explained rather than silently absent.
    assert note and "1.18.5" in note


def test_update_detection_falls_back_to_upstream_without_a_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(makemkv_updater, "get_mkvauto_tmp", lambda: tmp_path)
    from core import makemkv_manifest as mf
    monkeypatch.setattr(mf, "fetch_manifest", lambda *a, **k: (None, "unavailable"))
    monkeypatch.setattr(makemkv_updater, "fetch_latest_version", lambda: "1.18.4")
    version, _note, source = makemkv_updater.resolve_offerable_version()
    assert (version, source) == ("1.18.4", "upstream")


def test_explicit_ffmpeg_pin_beats_resolution(monkeypatch, tmp_path):
    """The version matrix tests one candidate at a time, so it must be able
    to pin explicitly — previously it monkeypatched the resolver, which
    silently changed behaviour for every other caller in the process."""
    captured = {}

    def fake_download(version, **kwargs):
        raise makemkv_updater.MakeMKVUpdateError("stop after the pin is chosen")

    monkeypatch.setattr(makemkv_updater, "download_makemkv_sources", fake_download)
    monkeypatch.setattr(
        makemkv_updater, "resolve_ffmpeg_for_build",
        lambda v: captured.setdefault("resolver_called", True) or "9.9.9",
    )
    with pytest.raises(makemkv_updater.MakeMKVUpdateError):
        makemkv_updater.install_makemkv_from_sources(
            "1.18.4", build_ffmpeg=True, ffmpeg_version="7.1",
            install_prefix=str(tmp_path),
        )
    # The resolver must not even be consulted when a pin is supplied.
    assert "resolver_called" not in captured


def test_a_timed_out_capture_is_retried_before_being_written_off(monkeypatch, tmp_path):
    """With one usable capture — the common case — 'try the next capture' is
    not an option, so a transient stall must not fail the install.

    Measured on a GitHub runner (2026-08-05): the 6.6MB oss tarball timed out
    where the same fetch succeeded elsewhere, and because that URL has
    exactly ONE archived capture the whole job died.
    """
    url = "https://www.makemkv.com/download/makemkv-oss-1.18.4.tar.gz"
    dest = tmp_path / "makemkv-oss-1.18.4.tar.gz"
    monkeypatch.setattr(
        makemkv_updater, "_wayback_snapshot_urls_for",
        lambda u, **k: ["https://web.archive.org/web/ONLYid_/" + u],
    )
    monkeypatch.setattr(makemkv_updater.time, "sleep", lambda s: None)
    monkeypatch.setattr(makemkv_updater, "_unwrap_double_gzip_if_needed", lambda *a, **k: None)
    monkeypatch.setattr(makemkv_updater, "_verify_tarball_gz", lambda *a, **k: True)

    attempts = {"n": 0}

    def flaky(u, d, logs, **kwargs):
        if u.startswith("https://www.makemkv.com"):
            raise makemkv_updater.MakeMKVUpdateError("primary down (525)")
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise makemkv_updater.MakeMKVUpdateError("timed out")
        d.write_bytes(b"payload")

    monkeypatch.setattr(makemkv_updater, "_download", flaky)
    makemkv_updater._download_with_fallback(url, dest, [])
    assert attempts["n"] == 2, "the single capture must be retried, not abandoned"
    assert dest.read_bytes() == b"payload"


def test_capture_retries_are_bounded(monkeypatch, tmp_path):
    url = "https://www.makemkv.com/download/makemkv-oss-1.18.4.tar.gz"
    dest = tmp_path / "out.tar.gz"
    monkeypatch.setattr(
        makemkv_updater, "_wayback_snapshot_urls_for",
        lambda u, **k: ["https://web.archive.org/web/ONLYid_/" + u],
    )
    monkeypatch.setattr(makemkv_updater.time, "sleep", lambda s: None)
    attempts = {"n": 0}

    def always_fail(u, d, logs, **kwargs):
        if u.startswith("https://web.archive.org"):
            attempts["n"] += 1
        raise makemkv_updater.MakeMKVUpdateError("timed out")

    monkeypatch.setattr(makemkv_updater, "_download", always_fail)
    with pytest.raises(makemkv_updater.MakeMKVUpdateError):
        makemkv_updater._download_with_fallback(url, dest, [])
    assert attempts["n"] == makemkv_updater.WAYBACK_DOWNLOAD_ATTEMPTS
