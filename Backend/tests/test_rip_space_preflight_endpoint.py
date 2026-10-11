"""#875: POST /jobs/rip must size the rip from the title sizes it already has.

Prod 2026-09-19: the cached scan payload's titles carry {file, flag,
obfuscated} and no sizes, so the estimate fell back to the 46 GB physical disc
size, passed against ~78 GB free, and the `mkv DEV all OUT` rip wrote 80 GB
(the feature is listed twice) — filling the volume and crash-looping Postgres.
The DiscTitle rows held the real 81 GB all along.
"""
from collections import namedtuple
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import models
from api.main import app

GB = 1024**3
_Usage = namedtuple("_Usage", "total used free")

# The real disc: 46 GB physical, 81 GB of enumerated titles because the main
# feature appears as both a playlist and a raw stream.
PHYSICAL_BYTES = 46 * GB
TITLE_SIZES = {
    10: int(40.3 * GB),   # 00800.mpls, main feature
    15: int(589.1 * 1024**2),
    16: int(39.7 * GB),   # 00301.m2ts, the SAME feature again
    17: int(197.4 * 1024**2),
}
SIZELESS_PAYLOAD_TITLES = {
    str(i): {"file": f"{i:05d}.mpls", "flag": 0, "obfuscated": False} for i in TITLE_SIZES
}


def _make_disc(session, *, disc_hash, with_sizes=True):
    disc = models.Disc(
        content_hash=disc_hash,
        format="Blu-Ray",
        scan_state="completed",
        disc_size_bytes=PHYSICAL_BYTES,
    )
    session.add(disc)
    session.commit()
    session.refresh(disc)
    for index, size in TITLE_SIZES.items():
        session.add(
            models.DiscTitle(
                disc_id=disc.id,
                source_file=f"{index:05d}.mpls",
                index=index,
                size=size if with_sizes else None,
            )
        )
    session.commit()
    session.refresh(disc)
    return disc


def _post_rip(disc_id, disc_hash, free_bytes):
    client = TestClient(app)
    with patch(
        "core.disc_scan_dispatch.disc_info_cache_satisfies", return_value=True
    ), patch(
        "core.disc_manager.get_cached_discs",
        return_value=[
            {
                "disc_num": "0",
                "mount_point": "/dev/sr0",
                "disc_hash": disc_hash,
                # Sizeless, exactly as the on-disc payload is
                "titles": SIZELESS_PAYLOAD_TITLES,
            }
        ],
    ), patch(
        "core.makemkv_updater.validate_makemkv_installation",
        return_value={"can_rip": True, "missing_components": [], "error_message": None},
    ), patch(
        "shutil.disk_usage",
        return_value=_Usage(total=983 * GB, used=983 * GB - free_bytes, free=free_bytes),
    ):
        return client.post(
            "/jobs/rip",
            json={
                "disc_num": "0",
                "mount_point": "/dev/sr0",
                "disc_id": disc_id,
                "mode": "copy",
            },
        )


def test_rip_refused_when_db_title_sizes_exceed_free_space(test_db):
    """The 17:19 rip that filled prod: ~78 GB free, 81 GB of titles → refuse."""
    session = test_db()
    try:
        disc = _make_disc(session, disc_hash="hash-875-sizes")
        disc_id = disc.id
    finally:
        session.close()

    resp = _post_rip(disc_id, "hash-875-sizes", free_bytes=78 * GB)

    assert resp.status_code == 400, resp.text
    detail = resp.json().get("detail") or ""
    assert "Insufficient disk space" in detail
    # Sized from the 81 GB of titles (×1.3 ≈ 105 GB), not the 46 GB disc (≈60 GB).
    required_gb = float(detail.split("Required: ")[1].split(" GB")[0])
    assert required_gb > 100, detail


def test_rip_allowed_when_free_space_covers_the_real_title_total(test_db):
    """Same disc, enough room for what it will actually write → not blocked on space."""
    session = test_db()
    try:
        disc = _make_disc(session, disc_hash="hash-875-roomy")
        disc_id = disc.id
    finally:
        session.close()

    resp = _post_rip(disc_id, "hash-875-roomy", free_bytes=400 * GB)

    assert "Insufficient disk space" not in str(resp.json().get("detail") or "")


def test_rip_still_uses_physical_size_when_no_title_sizes_exist(test_db):
    """No sizes anywhere: the physical size remains the floor, so an obviously
    too-small volume is still refused."""
    session = test_db()
    try:
        disc = _make_disc(session, disc_hash="hash-875-nosizes", with_sizes=False)
        disc_id = disc.id
    finally:
        session.close()

    resp = _post_rip(disc_id, "hash-875-nosizes", free_bytes=5 * GB)

    assert resp.status_code == 400, resp.text
    assert "Insufficient disk space" in (resp.json().get("detail") or "")
