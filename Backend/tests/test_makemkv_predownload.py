"""
Tests for MakeMKV source pre-download (#625).

Covers the idempotent download flow, the EULA extractor, and the
PredownloadState singleton + startup helper.
"""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import makemkv_predownload_state, makemkv_updater


@pytest.fixture(autouse=True)
def _reset_state():
    makemkv_predownload_state.reset_for_test()
    yield
    makemkv_predownload_state.reset_for_test()


@pytest.fixture
def tmp_mkvauto_root(tmp_path, monkeypatch):
    """Isolate MKVAUTO_TMP_DIR so tests don't collide with the real cache."""
    root = tmp_path / "mkvauto"
    tmp = root / "tmp"
    tmp.mkdir(parents=True)
    monkeypatch.setenv("MKVAUTO_ROOT", str(root))
    monkeypatch.setenv("MKVAUTO_TMP_DIR", str(tmp))
    return tmp


def _write_synthetic_tar(path: Path, entries: dict[str, bytes]) -> None:
    """Write a .tar.gz containing the given path→bytes entries."""
    with tarfile.open(path, "w:gz") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


# ─── EULA extractor ──────────────────────────────────────────────────────

def test_extract_eula_picks_readme_containing_marker(tmp_path):
    bin_tar = tmp_path / "makemkv-bin-1.17.5.tar.gz"
    oss_tar = tmp_path / "makemkv-oss-1.17.5.tar.gz"
    eula_text = (
        "MakeMKV End User License Agreement\n"
        "\n"
        "By installing this software you agree to the following terms...\n"
    )
    _write_synthetic_tar(bin_tar, {
        "makemkv-bin-1.17.5/README.txt": eula_text.encode("utf-8"),
        "makemkv-bin-1.17.5/bin/makemkvcon": b"binary-stub",
    })
    _write_synthetic_tar(oss_tar, {
        "makemkv-oss-1.17.5/Makefile": b"# nothing",
    })
    out = tmp_path / "EULA.txt"
    logs: list[str] = []

    assert makemkv_updater._extract_eula_text(bin_tar, oss_tar, out, logs)
    assert out.read_text() == eula_text
    assert any("Extracted EULA" in line for line in logs)


def test_extract_eula_returns_false_when_no_candidate_matches(tmp_path):
    bin_tar = tmp_path / "makemkv-bin-1.17.5.tar.gz"
    oss_tar = tmp_path / "makemkv-oss-1.17.5.tar.gz"
    _write_synthetic_tar(bin_tar, {
        "makemkv-bin-1.17.5/README.txt": b"Just release notes, no license phrase.",
    })
    _write_synthetic_tar(oss_tar, {"makemkv-oss-1.17.5/Makefile": b"# nothing"})
    out = tmp_path / "EULA.txt"
    logs: list[str] = []

    assert not makemkv_updater._extract_eula_text(bin_tar, oss_tar, out, logs)
    assert not out.exists()


def test_extract_eula_scans_oss_when_bin_lacks_marker(tmp_path):
    bin_tar = tmp_path / "makemkv-bin-1.17.5.tar.gz"
    oss_tar = tmp_path / "makemkv-oss-1.17.5.tar.gz"
    _write_synthetic_tar(bin_tar, {
        "makemkv-bin-1.17.5/README.txt": b"nothing here",
    })
    eula_text = "The End User License Agreement follows...\n"
    _write_synthetic_tar(oss_tar, {
        "makemkv-oss-1.17.5/LICENSE": eula_text.encode("utf-8"),
    })
    out = tmp_path / "EULA.txt"
    logs: list[str] = []

    assert makemkv_updater._extract_eula_text(bin_tar, oss_tar, out, logs)
    assert out.read_text() == eula_text


# ─── download_makemkv_sources ─────────────────────────────────────────────

def test_download_skips_when_tars_present_and_valid(tmp_mkvauto_root, monkeypatch):
    version = "1.17.5"
    work = makemkv_updater.predownload_dir(version)
    bin_tar = work / f"makemkv-bin-{version}.tar.gz"
    oss_tar = work / f"makemkv-oss-{version}.tar.gz"
    _write_synthetic_tar(bin_tar, {"pkg/x": b"y"})
    _write_synthetic_tar(oss_tar, {"pkg/x": b"y"})

    called = []
    def _boom(*_a, **_kw):
        called.append(True)
        raise AssertionError("should not download when tars present")
    monkeypatch.setattr(makemkv_updater, "_download_with_fallback", _boom)

    result = makemkv_updater.download_makemkv_sources(version)
    assert result.already_present is True
    assert result.version == version
    assert not called


def test_download_fetches_when_tars_missing(tmp_mkvauto_root, monkeypatch):
    version = "1.17.5"

    def _fake_download(url, dest, logs, log_cb=None):
        _write_synthetic_tar(dest, {
            "pkg/README.txt": b"End User License Agreement fake fake",
        })
        logs.append(f"downloaded {url}")

    monkeypatch.setattr(makemkv_updater, "_download_with_fallback", _fake_download)

    result = makemkv_updater.download_makemkv_sources(version)

    assert result.already_present is False
    assert result.version == version
    assert result.bin_tar.exists()
    assert result.oss_tar.exists()
    manifest = json.loads((result.work_dir / makemkv_updater.PREDOWNLOAD_MANIFEST_NAME).read_text())
    assert manifest["version"] == version
    assert manifest["bin_tar"] == result.bin_tar.name
    assert manifest["oss_tar"] == result.oss_tar.name
    assert manifest["sha256_bin"] and manifest["sha256_oss"]
    # EULA extracted because our fake payload contains the marker
    assert manifest["eula_file"] == makemkv_updater.PREDOWNLOAD_EULA_NAME
    assert result.eula_path is not None
    assert result.eula_path.exists()


def test_download_redownloads_when_existing_tar_is_corrupt(tmp_mkvauto_root, monkeypatch):
    version = "1.17.5"
    work = makemkv_updater.predownload_dir(version)
    bin_tar = work / f"makemkv-bin-{version}.tar.gz"
    oss_tar = work / f"makemkv-oss-{version}.tar.gz"
    # Corrupt (not a valid gzip stream)
    bin_tar.write_bytes(b"not a real tar")
    oss_tar.write_bytes(b"also nope")

    fresh_calls: list[str] = []
    def _fake_download(url, dest, logs, log_cb=None):
        fresh_calls.append(url)
        _write_synthetic_tar(dest, {"pkg/x": b"y"})

    monkeypatch.setattr(makemkv_updater, "_download_with_fallback", _fake_download)

    result = makemkv_updater.download_makemkv_sources(version)
    assert result.already_present is False
    assert len(fresh_calls) == 2


def test_read_predownload_manifest_by_version(tmp_mkvauto_root, monkeypatch):
    version = "1.17.5"
    def _fake_download(url, dest, logs, log_cb=None):
        _write_synthetic_tar(dest, {"pkg/x": b"y"})
    monkeypatch.setattr(makemkv_updater, "_download_with_fallback", _fake_download)

    makemkv_updater.download_makemkv_sources(version)
    m = makemkv_updater.read_predownload_manifest(version)
    assert m is not None
    assert m["version"] == version

    assert makemkv_updater.read_predownload_manifest() is not None


# ─── PredownloadState + startup helper ────────────────────────────────────

def test_state_transitions():
    assert makemkv_predownload_state.snapshot()["state"] == "missing"
    makemkv_predownload_state.mark_downloading("1.17.5")
    s = makemkv_predownload_state.snapshot()
    assert s["state"] == "downloading" and s["version"] == "1.17.5"
    makemkv_predownload_state.mark_ready("1.17.5")
    assert makemkv_predownload_state.snapshot()["state"] == "ready"
    makemkv_predownload_state.mark_failed("boom")
    s = makemkv_predownload_state.snapshot()
    assert s["state"] == "failed" and s["error"] == "boom"


def test_initialize_from_disk_reads_manifest(tmp_mkvauto_root, monkeypatch):
    version = "1.17.5"
    def _fake_download(url, dest, logs, log_cb=None):
        _write_synthetic_tar(dest, {"pkg/x": b"y"})
    monkeypatch.setattr(makemkv_updater, "_download_with_fallback", _fake_download)
    makemkv_updater.download_makemkv_sources(version)

    makemkv_predownload_state.reset_for_test()
    makemkv_predownload_state.initialize_from_disk()

    s = makemkv_predownload_state.snapshot()
    assert s["state"] == "ready"
    assert s["version"] == version


def test_run_predownload_skips_when_makemkv_installed():
    calls: list[str] = []
    def _download():
        calls.append("dl")
        return SimpleNamespace(version="1.17.5", already_present=False)
    makemkv_predownload_state.run_predownload_if_needed(
        validation_fn=lambda: {"is_valid": True},
        download_fn=_download,
    )
    assert not calls
    assert makemkv_predownload_state.snapshot()["state"] == "missing"


def test_run_predownload_skips_when_state_ready():
    makemkv_predownload_state.mark_ready("1.17.5")
    calls: list[str] = []
    def _download():
        calls.append("dl")
        return SimpleNamespace(version="1.17.5", already_present=True)
    makemkv_predownload_state.run_predownload_if_needed(
        validation_fn=lambda: {"is_valid": False},
        download_fn=_download,
    )
    assert not calls
    assert makemkv_predownload_state.snapshot()["state"] == "ready"


def test_run_predownload_transitions_to_ready():
    def _download():
        return SimpleNamespace(version="1.17.5", already_present=False)
    makemkv_predownload_state.run_predownload_if_needed(
        validation_fn=lambda: {"is_valid": False},
        download_fn=_download,
    )
    s = makemkv_predownload_state.snapshot()
    assert s["state"] == "ready"
    assert s["version"] == "1.17.5"


def test_run_predownload_transitions_to_failed():
    def _download():
        raise RuntimeError("network down")
    makemkv_predownload_state.run_predownload_if_needed(
        validation_fn=lambda: {"is_valid": False},
        download_fn=_download,
    )
    s = makemkv_predownload_state.snapshot()
    assert s["state"] == "failed"
    assert "network down" in s["error"]


# ---------------------------------------------------------------------------
# install_makemkv_from_sources version resolution
#
# The install must reuse the version pre-downloaded at startup instead of
# re-scraping (a fresh scrape can resolve a different, older version than the
# pre-download — e.g. the Wayback fallback serves a stale forum snapshot — which
# would install an expired beta that then rejects a valid registration key).
# ---------------------------------------------------------------------------


class _StopBuild(Exception):
    """Sentinel so the version-resolution assertion runs without a real build."""


def _patch_download_capture(monkeypatch):
    """Patch download_makemkv_sources to record the version it's called with, then
    stop before the (expensive, network/compile) build steps."""
    captured: dict = {}

    def _fake_download(version=None, *, log_cb=None):
        captured["version"] = version
        raise _StopBuild()

    monkeypatch.setattr(makemkv_updater, "download_makemkv_sources", _fake_download)
    return captured


def test_install_uses_predownloaded_version_when_none(monkeypatch):
    """install(None) installs the pre-downloaded 'ready' version, not a fresh scrape."""
    makemkv_predownload_state.mark_ready("1.18.4")
    captured = _patch_download_capture(monkeypatch)
    # A live scrape must NOT happen when a pre-download is ready.
    monkeypatch.setattr(
        makemkv_updater, "fetch_latest_version",
        lambda: (_ for _ in ()).throw(AssertionError("must not scrape when pre-download ready")),
    )
    with pytest.raises(_StopBuild):
        makemkv_updater.install_makemkv_from_sources(
            None, build_ffmpeg=False, install_prefix="/tmp/mkvauto-test"
        )
    assert captured["version"] == "1.18.4"


def test_install_falls_back_to_scrape_when_no_predownload(monkeypatch):
    """With no pre-download ready, install passes version=None downstream (scrape path)."""
    makemkv_predownload_state.mark_failed("boom")  # state != "ready"
    captured = _patch_download_capture(monkeypatch)
    with pytest.raises(_StopBuild):
        makemkv_updater.install_makemkv_from_sources(
            None, build_ffmpeg=False, install_prefix="/tmp/mkvauto-test"
        )
    assert captured["version"] is None


def test_install_honors_explicit_version_over_predownload(monkeypatch):
    """An explicitly requested version is never overridden by the pre-download."""
    makemkv_predownload_state.mark_ready("1.18.4")
    captured = _patch_download_capture(monkeypatch)
    with pytest.raises(_StopBuild):
        makemkv_updater.install_makemkv_from_sources(
            "1.17.5", build_ffmpeg=False, install_prefix="/tmp/mkvauto-test"
        )
    assert captured["version"] == "1.17.5"


def test_partial_predownload_is_resumed_not_discarded(monkeypatch, tmp_path):
    """A good tarball must survive its sibling's failed fetch.

    download_makemkv_sources used to delete BOTH tarballs whenever either
    was missing, then refetch both. On a fresh install with makemkv.com
    down (2026-08-05), the bin tarball came from archive.org over ~2
    minutes, the oss fetch was then rate-limited, and the retry deleted the
    18MB it had just earned — turning a resumable install into a loop that
    could not finish while the archive was throttling.
    """
    import gzip
    import tarfile
    import io

    def _make_tar_gz(path, name="payload.txt"):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            data = b"x" * 32
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        path.write_bytes(gzip.compress(buf.getvalue()))

    work = tmp_path / "makemkv-download" / "1.18.4"
    work.mkdir(parents=True)
    bin_tar = work / "makemkv-bin-1.18.4.tar.gz"
    oss_tar = work / "makemkv-oss-1.18.4.tar.gz"
    _make_tar_gz(bin_tar)  # already fetched; oss is absent
    bin_bytes_before = bin_tar.read_bytes()

    monkeypatch.setattr(makemkv_updater, "predownload_dir", lambda v: work)
    monkeypatch.setattr(makemkv_updater, "fetch_latest_version", lambda: "1.18.4")
    monkeypatch.setattr(makemkv_updater, "_extract_eula_text", lambda *a, **k: False)
    monkeypatch.setattr(makemkv_updater, "_verify_against_manifest", lambda *a, **k: None)

    fetched = []

    def fake_download(url, dest, logs, **kwargs):
        fetched.append(url)
        _make_tar_gz(dest)

    monkeypatch.setattr(makemkv_updater, "_download_with_fallback", fake_download)

    makemkv_updater.download_makemkv_sources("1.18.4")

    assert len(fetched) == 1, f"only the missing artifact should be fetched, got {fetched}"
    assert "makemkv-oss" in fetched[0]
    assert bin_tar.read_bytes() == bin_bytes_before, \
        "the already-downloaded tarball must not be deleted or refetched"


def test_corrupt_tarball_is_refetched_but_its_sibling_is_kept(monkeypatch, tmp_path):
    import gzip
    import io
    import tarfile

    def _make_tar_gz(path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            data = b"y" * 16
            info = tarfile.TarInfo("f.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        path.write_bytes(gzip.compress(buf.getvalue()))

    work = tmp_path / "makemkv-download" / "1.18.4"
    work.mkdir(parents=True)
    bin_tar = work / "makemkv-bin-1.18.4.tar.gz"
    oss_tar = work / "makemkv-oss-1.18.4.tar.gz"
    _make_tar_gz(bin_tar)
    oss_tar.write_bytes(b"truncated garbage")  # fails verification
    bin_before = bin_tar.read_bytes()

    monkeypatch.setattr(makemkv_updater, "predownload_dir", lambda v: work)
    monkeypatch.setattr(makemkv_updater, "fetch_latest_version", lambda: "1.18.4")
    monkeypatch.setattr(makemkv_updater, "_extract_eula_text", lambda *a, **k: False)
    monkeypatch.setattr(makemkv_updater, "_verify_against_manifest", lambda *a, **k: None)

    fetched = []

    def fake_download(url, dest, logs, **kwargs):
        fetched.append(url)
        _make_tar_gz(dest)

    monkeypatch.setattr(makemkv_updater, "_download_with_fallback", fake_download)
    makemkv_updater.download_makemkv_sources("1.18.4")

    assert len(fetched) == 1 and "makemkv-oss" in fetched[0]
    assert bin_tar.read_bytes() == bin_before
