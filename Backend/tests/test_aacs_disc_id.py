"""TheDiscDB's GlobalDiscId — the AACS disc ID (#741).

`SHA1(AACS/Unit_Key_RO.inf)`, uppercase hex. It only exists on the physical disc,
so the scan is the one chance to capture it; everything here is about never
letting that capture break a scan, and never inventing a value. Upstream's field
is immutable once set, so a wrong ID is worse than a missing one.
"""
import hashlib
from unittest.mock import patch

import pytest

from core.aacs_disc_id import compute_from_device, compute_from_mount


@pytest.fixture
def bluray(tmp_path):
    """A mounted Blu-ray root: AACS/ sits beside BDMV/."""
    (tmp_path / "BDMV" / "STREAM").mkdir(parents=True)
    aacs = tmp_path / "AACS"
    aacs.mkdir()
    (aacs / "Unit_Key_RO.inf").write_bytes(b"\x00\x01unit-key-bytes")
    return tmp_path


def test_matches_sha1_of_the_unit_key_file(bluray):
    expected = hashlib.sha1(b"\x00\x01unit-key-bytes").hexdigest().upper()
    assert compute_from_mount(bluray) == expected


def test_result_is_uppercase_40_hex(bluray):
    """Upstream stores it uppercase and unprefixed; a case mismatch would not match."""
    out = compute_from_mount(bluray)
    assert len(out) == 40
    assert out == out.upper()
    assert all(c in "0123456789ABCDEF" for c in out)


def test_dvd_returns_none(tmp_path):
    """DVDs have no AACS directory — upstream deferred their ID entirely."""
    (tmp_path / "VIDEO_TS").mkdir()
    assert compute_from_mount(tmp_path) is None


def test_bluray_without_aacs_returns_none(tmp_path):
    (tmp_path / "BDMV").mkdir()
    assert compute_from_mount(tmp_path) is None


def test_missing_mount_returns_none(tmp_path):
    assert compute_from_mount(tmp_path / "nope") is None


def test_empty_unit_key_returns_none(bluray):
    """SHA1 of nothing is a valid-looking hash that identifies no disc."""
    (bluray / "AACS" / "Unit_Key_RO.inf").write_bytes(b"")
    assert compute_from_mount(bluray) is None


def test_implausibly_large_file_returns_none(bluray):
    """Guards against pulling an arbitrary read into memory off a bad filesystem."""
    (bluray / "AACS" / "Unit_Key_RO.inf").write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    assert compute_from_mount(bluray) is None


def test_read_error_returns_none_rather_than_raising(bluray):
    """A scratched or mid-eject disc must not take down the scan."""
    with patch("pathlib.Path.read_bytes", side_effect=OSError("I/O error")):
        assert compute_from_mount(bluray) is None


class TestFromDevice:
    def test_mounts_the_device_and_hashes(self, bluray):
        from contextlib import contextmanager

        @contextmanager
        def fake_mount(device):
            yield bluray

        # The block-device guard runs first, and /dev/sr0 does not exist on a
        # dev machine — so state it explicitly rather than relying on the host.
        with patch("core.aacs_disc_id._is_block_device", return_value=True), \
             patch("core.segment_reorder._mounted_disc", fake_mount):
            assert compute_from_device("/dev/sr0") == compute_from_mount(bluray)

    def test_a_mount_failure_is_not_fatal(self):
        """No optical drive, no disc, unmountable filesystem — all just mean 'no ID'."""
        with patch("core.segment_reorder._mounted_disc", side_effect=OSError("mount failed")):
            assert compute_from_device("/dev/sr0") is None


class TestPersistence:
    """The column is add-only, which is what makes re-inserting a disc a backfill."""

    def test_an_existing_disc_is_filled_when_empty(self, test_db):
        from api import crud, models

        with test_db() as session:
            disc = models.Disc(content_hash="h-backfill")
            session.add(disc)
            session.commit()

            out = crud.get_or_create_disc(
                session, "h-backfill", None, {"global_disc_id": "abc123"}
            )
            # Stored uppercase to match upstream, whatever case the scan produced.
            assert out.global_disc_id == "ABC123"

    def test_an_existing_id_is_never_overwritten(self, test_db):
        """A later scan that failed to read one must not clear a good value."""
        from api import crud, models

        with test_db() as session:
            session.add(models.Disc(content_hash="h-keep", global_disc_id="ORIGINAL"))
            session.commit()

            out = crud.get_or_create_disc(session, "h-keep", None, {"global_disc_id": "OTHER"})
            assert out.global_disc_id == "ORIGINAL"

            out = crud.get_or_create_disc(session, "h-keep", None, {})
            assert out.global_disc_id == "ORIGINAL"

    def test_a_new_disc_without_an_id_stores_null(self, test_db):
        from api import crud

        with test_db() as session:
            disc = crud.get_or_create_disc(session, "h-new", None, {})
            assert disc.global_disc_id is None

    def test_the_export_emits_it_once_captured(self, test_db):
        """End of the chain: scan -> column -> label payload -> GlobalDiscId."""
        from api import models
        from core.discdb_finalize import _to_disc_json, build_label_payload_from_disc

        with test_db() as session:
            movie = models.Movie(name="X")
            session.add(movie)
            session.flush()
            release = models.Release(name="X", slug="x", type="movie", movie_id=movie.id)
            session.add(release)
            session.flush()
            disc = models.Disc(
                content_hash="h-export", global_disc_id="D2924B73", release_id=release.id
            )
            session.add(disc)
            session.commit()
            session.refresh(disc)

            payload = build_label_payload_from_disc(disc, release)
            assert payload["global_disc_id"] == "D2924B73"
            assert _to_disc_json(payload, "h-export", 1, "disc01", None)["GlobalDiscId"] == "D2924B73"


class TestDeviceGuard:
    """The scan calls this on every disc. It must not shell out to `mount` for
    a path that cannot be mounted — an external command with no timeout of its
    own does not belong on that path, and unit tests drive the scan with
    placeholder device paths."""

    def test_a_nonexistent_device_never_reaches_mount(self):
        with patch("core.segment_reorder._mounted_disc") as mounted:
            assert compute_from_device("/dev/sr0-does-not-exist") is None
        mounted.assert_not_called()

    def test_a_regular_file_is_not_treated_as_a_device(self, tmp_path):
        f = tmp_path / "not-a-device"
        f.write_text("x")
        with patch("core.segment_reorder._mounted_disc") as mounted:
            assert compute_from_device(str(f)) is None
        mounted.assert_not_called()

    def test_a_directory_is_not_treated_as_a_device(self, tmp_path):
        with patch("core.segment_reorder._mounted_disc") as mounted:
            assert compute_from_device(str(tmp_path)) is None
        mounted.assert_not_called()

    def test_a_real_block_device_still_gets_mounted(self, bluray):
        from contextlib import contextmanager

        @contextmanager
        def fake_mount(device):
            yield bluray

        with patch("core.aacs_disc_id._is_block_device", return_value=True), \
             patch("core.segment_reorder._mounted_disc", fake_mount):
            assert compute_from_device("/dev/sr0") == compute_from_mount(bluray)


class TestInsertPathCapture:
    """The udev-insert handler must capture the ID, not just the refresh path.

    rc-1.1.0-rc.2 shipped with the capture only in ``_load_discinfo`` — so the
    way a disc normally arrives (insertion) never captured, and the backfill
    only fired on an explicit rescan, which nobody does. Caught live: a real
    insert matched the disc by content hash and left the column NULL.
    """

    def _run_insert(self, monkeypatch, *, aacs_id):
        from unittest.mock import patch as _patch

        from core import _drive_operations as drv_ops
        from core.disc_slot_state import reset_disc_slot_state_for_tests

        reset_disc_slot_state_for_tests()
        monkeypatch.setattr(drv_ops, "hash_media_disc", lambda *a, **k: "HASH123")
        # MakeMKV missing entirely — the fresh-container case the live test hit.
        # The insert path continues without it; the capture must still run.
        def _no_makemkv(*a, **k):
            raise FileNotFoundError("/usr/bin/makemkvcon")
        monkeypatch.setattr(drv_ops, "run_makemkv", _no_makemkv)

        captured = {}
        with _patch("core.disc_manager.on_disc_inserted"), \
             _patch("core.disc_manager.on_disc_scan_complete",
                    side_effect=lambda rd: captured.update(rd)), \
             _patch.object(drv_ops, "clear_keys_by_mount_point"), \
             _patch("core.aacs_disc_id.compute_from_device",
                    return_value=aacs_id) as compute:
            result = drv_ops.handle_disc_insert("0", "/dev/sr0")
        return result, captured, compute

    def test_insert_captures_and_forwards_the_id(self, monkeypatch):
        result, raw_data, compute = self._run_insert(
            monkeypatch, aacs_id="9932699DF246D4C02B3B386873A790AAA14923E1"
        )
        assert result["status"] == "ok"
        compute.assert_called_once_with("/dev/sr0")
        # disc_manager spreads raw_data into disc_info verbatim, and
        # get_or_create_disc reads payload["global_disc_id"] — so landing in
        # raw_data is what gets it onto the disc row.
        assert raw_data["global_disc_id"] == "9932699DF246D4C02B3B386873A790AAA14923E1"

    def test_a_dvd_yields_no_key_rather_than_null(self, monkeypatch):
        _, raw_data, _ = self._run_insert(monkeypatch, aacs_id=None)
        assert "global_disc_id" not in raw_data

    def test_no_hash_means_no_mount_attempt(self, monkeypatch):
        """A disc that would not even hash must not get a second mount."""
        from unittest.mock import patch as _patch

        from core import _drive_operations as drv_ops
        from core.disc_slot_state import reset_disc_slot_state_for_tests

        reset_disc_slot_state_for_tests()
        monkeypatch.setattr(drv_ops, "hash_media_disc",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("no structure")))
        monkeypatch.setattr(drv_ops, "run_makemkv",
                            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))

        with _patch("core.disc_manager.on_disc_inserted"), \
             _patch("core.disc_manager.on_disc_scan_complete"), \
             _patch.object(drv_ops, "clear_keys_by_mount_point"), \
             _patch("core.aacs_disc_id.compute_from_device") as compute:
            drv_ops.handle_disc_insert("0", "/dev/sr0")
        compute.assert_not_called()
