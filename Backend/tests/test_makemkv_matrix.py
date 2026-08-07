"""The version-matrix publisher: what reaches the public manifest.

The manifest gates user-visible MakeMKV updates and is what clients verify
downloads against, so what it does — and does not — claim matters more than
most code in this repo.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "Backend"))


@pytest.fixture(scope="module")
def mx():
    spec = importlib.util.spec_from_file_location(
        "makemkv_matrix", ROOT / "scripts" / "makemkv-matrix.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(mkv, ff, build="ok", source=None, sha=None, tested_at="2026-08-05T00:00:00+00:00"):
    row = {"makemkv_version": mkv, "ffmpeg_version": ff, "build": build, "tested_at": tested_at}
    if source is not None:
        row["source"] = source
    if sha is not None:
        row["sha256"] = sha
    return row


VENDOR = {"makemkv-bin-1.18.4.tar.gz": "vendor", "makemkv-oss-1.18.4.tar.gz": "vendor"}
ARCHIVE = {"makemkv-bin-1.18.4.tar.gz": "archive", "makemkv-oss-1.18.4.tar.gz": "vendor"}
SHA = {"makemkv-bin-1.18.4.tar.gz": "cee56de0"}


class TestHashProvenance:
    def test_vendor_sourced_build_publishes_its_hashes(self, mx):
        m = mx.build_manifest([_row("1.18.4", "8.1.2", source=VENDOR, sha=SHA)])
        assert m["validated"]["1.18.4"]["sha256"] == SHA
        assert "hashes_withheld" not in m["validated"]["1.18.4"]

    def test_archive_sourced_build_publishes_the_pair_but_not_the_hashes(self, mx):
        """A mirror copy that builds is good evidence for the version
        RELATIONSHIP but is not a vendor-authenticity claim. Publishing it as
        one cuts both ways: a client fetching from makemkv.com would reject
        the vendor's own bytes if the mirror ever differed, and a client
        fetching from the archive would be checking a copy against itself."""
        m = mx.build_manifest([_row("1.18.4", "8.1.2", source=ARCHIVE, sha=SHA)])
        entry = m["validated"]["1.18.4"]
        # The relationship still ships — gating must keep working while
        # makemkv.com is down.
        assert entry["ffmpeg_version"] == "8.1.2"
        assert m["latest_validated"] == "1.18.4"
        # But nothing is offered for verification.
        assert entry["sha256"] == {}
        assert "hashes_withheld" in entry

    def test_unknown_origin_is_treated_as_not_vendor(self, mx):
        # "cached" means the tarball was already on disk; we cannot say where
        # it came from, so the conservative reading applies.
        cached = {"makemkv-bin-1.18.4.tar.gz": "cached"}
        m = mx.build_manifest([_row("1.18.4", "8.1.2", source=cached, sha=SHA)])
        assert m["validated"]["1.18.4"]["sha256"] == {}

    def test_missing_provenance_is_treated_as_not_vendor(self, mx):
        # Ledger rows written before provenance existed must not be assumed good.
        m = mx.build_manifest([_row("1.18.4", "8.1.2", sha=SHA)])
        assert m["validated"]["1.18.4"]["sha256"] == {}


class TestManifestShape:
    def test_only_successful_builds_are_validated(self, mx):
        m = mx.build_manifest([
            _row("1.18.4", "9.0", build="build_failed"),
            _row("1.18.4", "8.1.2", source=VENDOR, sha=SHA),
        ])
        assert set(m["validated"]) == {"1.18.4"}
        assert m["validated"]["1.18.4"]["ffmpeg_version"] == "8.1.2"

    def test_failures_are_published_so_a_held_back_update_can_be_explained(self, mx):
        m = mx.build_manifest([_row("1.18.5", "9.0", build="build_failed")])
        assert m["validated"] == {}
        assert m["known_incompatible"][0]["makemkv_version"] == "1.18.5"

    def test_a_version_that_later_succeeded_is_not_reported_incompatible(self, mx):
        m = mx.build_manifest([
            _row("1.18.4", "9.0", build="build_failed"),
            _row("1.18.4", "8.1.2", source=VENDOR, sha=SHA),
        ])
        assert all(r["makemkv_version"] != "1.18.4" for r in m["known_incompatible"])

    def test_fallback_mismatch_is_never_certified(self, mx):
        m = mx.build_manifest([_row("1.18.4", "8.1.2", build="fallback_mismatch")])
        assert m["validated"] == {}
        assert m["known_incompatible"][0]["reason"] == "fallback_mismatch"

    def test_newest_working_ffmpeg_wins_for_a_version(self, mx):
        m = mx.build_manifest([
            _row("1.18.4", "7.1", source=VENDOR, sha=SHA),
            _row("1.18.4", "8.1.2", source=VENDOR, sha=SHA),
        ])
        assert m["validated"]["1.18.4"]["ffmpeg_version"] == "8.1.2"

    def test_client_can_read_what_the_publisher_writes(self, mx):
        """The publisher and the client share one contract; drift here is
        invisible until an update silently stops being offered."""
        from core import makemkv_manifest as mf

        m = mx.build_manifest([
            _row("1.18.4", "8.1.2", source=VENDOR, sha=SHA),
            _row("1.18.5", "9.0", build="build_failed"),
        ])
        assert m["schema"] == mf.SCHEMA_VERSION
        assert mf.latest_validated(m) == "1.18.4"
        assert mf.ffmpeg_for(m, "1.18.4") == "8.1.2"
        assert mf.expected_sha256(m, "1.18.4", "makemkv-bin-1.18.4.tar.gz") == "cee56de0"
        assert mf.is_stale(m) is False
        note = mf.incompatibility_note(m, "1.18.5")
        assert note and "1.18.5" in note

    def test_withheld_hashes_read_as_unverifiable_by_the_client(self, mx):
        from core import makemkv_manifest as mf

        m = mx.build_manifest([_row("1.18.4", "8.1.2", source=ARCHIVE, sha=SHA)])
        # None => "we cannot verify this", which the installer treats as
        # unverified rather than as a match.
        assert mf.expected_sha256(m, "1.18.4", "makemkv-bin-1.18.4.tar.gz") is None
        assert mf.ffmpeg_for(m, "1.18.4") == "8.1.2"


class TestEnvironmentFailuresAreNotCompatibilityVerdicts:
    """A broken build host must never publish a claim about the software.

    Observed 2026-08-05: the runner was missing libx264-dev, every candidate
    build failed for that reason, and the job published
    "MakeMKV 1.18.4 is incompatible with 8.1.2 / 8.1.1 / 8.1 / 8.0.3" to the
    public manifest — telling users something false about their software, and
    poisoning the ledger, whose memoization would have skipped those pairs
    forever.
    """

    def test_build_environment_error_is_not_a_makemkv_update_error(self, mx):
        BuildEnvironmentError = mx.mu.BuildEnvironmentError
        MakeMKVUpdateError = mx.mu.MakeMKVUpdateError
        # Kept deliberately unrelated so no `except MakeMKVUpdateError` can
        # silently absorb it and turn it into a per-pair verdict.
        assert not issubclass(BuildEnvironmentError, MakeMKVUpdateError)

    def test_missing_build_deps_raises_the_environment_error(self, mx, monkeypatch):
        """The exact real-world shape: tools present, but pkg-config reports
        x264 absent — which is what the runner hit."""
        mu = mx.mu

        monkeypatch.setattr(mu.shutil, "which", lambda name: f"/usr/bin/{name}")

        class _Missing:
            returncode = 1

        monkeypatch.setattr(mu.subprocess, "run", lambda *a, **k: _Missing())
        with pytest.raises(mu.BuildEnvironmentError) as exc:
            mu._check_build_deps([])
        assert "Missing build dependencies" in str(exc.value)
        assert "x264" in str(exc.value)

    def test_try_pair_propagates_environment_errors_instead_of_reporting_failure(self, mx, monkeypatch):
        # Use the SCRIPT's own module reference. Importing `core.makemkv_updater`
        # separately here can yield a different module object depending on
        # sys.path, and then `except mu.BuildEnvironmentError` inside the script
        # would not match the class raised by the test — which is exactly how
        # this passed locally and failed in CI.
        mu = mx.mu

        def boom(*a, **k):
            raise mu.BuildEnvironmentError("Missing build dependencies: libs=x264")

        monkeypatch.setattr(mu, "install_makemkv_from_sources", boom)
        with pytest.raises(mu.BuildEnvironmentError):
            mx.try_pair("1.18.4", "8.1.2", {})

    def test_such_a_row_would_never_reach_the_manifest(self, mx):
        # Belt and braces: even if a row somehow carried this reason, the
        # published manifest must not assert the pair is validated.
        m = mx.build_manifest([_row("1.18.4", "8.1.2", build="build_failed",
                                    tested_at="2026-08-05T00:00:00+00:00")])
        assert m["validated"] == {}


class TestSourceArchiveReadPath:
    """The sources repo exists so the job is not hostage to a mirror.

    archive.org would not serve the 6.6MB oss tarball to a GitHub runner —
    two runs died after 3x300s of retries on a file that downloads in ~60s
    elsewhere — while those exact bytes were already in the repo this job
    clones at startup. It wrote to that archive and never read from it.
    """

    def _tar_gz(self, path: Path, payload=b"x" * 64):
        import gzip, io, tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo("f.txt")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        path.write_bytes(gzip.compress(buf.getvalue()))

    def test_verified_archive_copies_are_restored_without_the_network(self, mx, tmp_path):
        ledger = tmp_path / "ledger"
        src = ledger / "sources" / "1.18.4"
        src.mkdir(parents=True)
        self._tar_gz(src / "makemkv-bin-1.18.4.tar.gz")
        self._tar_gz(src / "makemkv-oss-1.18.4.tar.gz")

        dest = tmp_path / "work"
        restored = mx.restore_from_archive(ledger, "1.18.4", dest)
        assert set(restored) == {"makemkv-bin-1.18.4.tar.gz", "makemkv-oss-1.18.4.tar.gz"}
        assert (dest / "makemkv-bin-1.18.4.tar.gz").exists()

    def test_recorded_origin_survives_the_round_trip(self, mx, tmp_path):
        """A vendor fetch archived today must still read back as `vendor`
        tomorrow, or hashes could never be certified from the archive."""
        ledger = tmp_path / "ledger"
        work = tmp_path / "work"
        work.mkdir()
        tar = work / "makemkv-bin-1.18.4.tar.gz"
        self._tar_gz(tar)

        mx.archive_sources(ledger, "1.18.4", [tar], {"makemkv-bin-1.18.4.tar.gz": "vendor"})
        restored = mx.restore_from_archive(ledger, "1.18.4", tmp_path / "work2")
        assert restored["makemkv-bin-1.18.4.tar.gz"] == "vendor"

    def test_a_later_mirror_fetch_never_downgrades_a_recorded_vendor_origin(self, mx, tmp_path):
        # Bytes are identical (hash-verified); the stronger claim is the true one.
        ledger = tmp_path / "ledger"
        work = tmp_path / "work"
        work.mkdir()
        tar = work / "makemkv-bin-1.18.4.tar.gz"
        self._tar_gz(tar)

        mx.archive_sources(ledger, "1.18.4", [tar], {"makemkv-bin-1.18.4.tar.gz": "vendor"})
        mx.archive_sources(ledger, "1.18.4", [tar], {"makemkv-bin-1.18.4.tar.gz": "archive"})
        restored = mx.restore_from_archive(ledger, "1.18.4", tmp_path / "work2")
        assert restored["makemkv-bin-1.18.4.tar.gz"] == "vendor"

    def test_unrecorded_origin_reads_back_as_not_vendor(self, mx, tmp_path):
        """Restoring must never launder an unknown origin into a vendor
        claim — `archived` is treated as not-vendor by the manifest."""
        ledger = tmp_path / "ledger"
        src = ledger / "sources" / "1.18.4"
        src.mkdir(parents=True)
        self._tar_gz(src / "makemkv-bin-1.18.4.tar.gz")   # no provenance.json

        restored = mx.restore_from_archive(ledger, "1.18.4", tmp_path / "work")
        assert restored["makemkv-bin-1.18.4.tar.gz"] == "archived"
        m = mx.build_manifest([_row("1.18.4", "8.1.2",
                                    source={"makemkv-bin-1.18.4.tar.gz": "archived"},
                                    sha={"makemkv-bin-1.18.4.tar.gz": "abc"})])
        assert m["validated"]["1.18.4"]["sha256"] == {}, "must not certify"

    def test_a_corrupt_archive_copy_is_skipped_so_the_caller_refetches(self, mx, tmp_path):
        ledger = tmp_path / "ledger"
        src = ledger / "sources" / "1.18.4"
        src.mkdir(parents=True)
        (src / "makemkv-bin-1.18.4.tar.gz").write_bytes(b"truncated garbage")

        restored = mx.restore_from_archive(ledger, "1.18.4", tmp_path / "work")
        assert restored == {}

    def test_missing_archive_is_not_an_error(self, mx, tmp_path):
        assert mx.restore_from_archive(tmp_path / "nope", "1.18.4", tmp_path / "work") == {}


class TestFetchFailuresAreNotCompatibilityVerdicts:
    """A slow mirror is not evidence that two versions are incompatible.

    Observed on the first successful run (2026-08-06): a transient
    ffmpeg.org timeout recorded `1.18.4 + 8.1.2 -> build_failed`. That
    verdict is memoized, so the pair would never have been retried and the
    matrix would have pinned users to 8.1.1 on the strength of one slow
    download — while 8.1.2 had built fine three times that day.
    """

    def test_source_unavailable_stays_a_makemkv_update_error(self, mx):
        """It must remain catchable by the archive-fallback retry loop, which
        handles MakeMKVUpdateError — breaking that hierarchy would stop the
        fallback retrying at all."""
        mu = mx.mu
        assert issubclass(mu.SourceUnavailableError, mu.MakeMKVUpdateError)

    def test_try_pair_propagates_a_fetch_failure_rather_than_failing_the_pair(self, mx, monkeypatch):
        mu = mx.mu

        def boom(*a, **k):
            raise mu.SourceUnavailableError("Download failed (timeout or network): timed out")

        monkeypatch.setattr(mu, "install_makemkv_from_sources", boom)
        with pytest.raises(mu.SourceUnavailableError):
            mx.try_pair("1.18.4", "8.1.2", {})

    def test_a_genuine_build_failure_is_still_recorded(self, mx, monkeypatch):
        # The distinction must not swallow real compile failures.
        mu = mx.mu

        def boom(*a, **k):
            raise mu.MakeMKVUpdateError("Command failed (2): make")

        monkeypatch.setattr(mu, "install_makemkv_from_sources", boom)
        ok, detail = mx.try_pair("1.18.4", "9.0", {})
        assert ok is False and "make" in detail
