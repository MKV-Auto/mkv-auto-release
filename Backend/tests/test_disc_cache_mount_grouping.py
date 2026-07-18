"""Disc cache: coordinator-facing get_cached_discs groups by mount_point."""


def test_get_cached_discs_keeps_latest_payload_per_mount_point(monkeypatch):
    """After MakeMKV renumbers, old and new disc_num keys can coexist; UI must see one tray."""

    def fake_snapshot():
        return [
            ("0", 10.0, {"disc_num": "0", "mount_point": "/dev/sr1", "disc_id": "old"}),
            ("1", 20.0, {"disc_num": "1", "mount_point": "/dev/sr1", "disc_id": "new"}),
        ]

    monkeypatch.setattr("core.disc_cache.snapshot_entries", fake_snapshot)
    from core.disc_manager import get_cached_discs

    out = get_cached_discs()
    assert len(out) == 1
    assert out[0]["disc_num"] == "1"
    assert out[0]["mount_point"] == "/dev/sr1"
    assert out[0]["disc_id"] == "new"
