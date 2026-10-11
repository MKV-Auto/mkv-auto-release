"""#885: local destinations are never created silently, a path on the
container's own writable layer is caught, and space is never measured on a
parent directory standing in for the configured path."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import core.storage_location as sl


def _mountinfo(tmp_path: Path, mounts: list[tuple[str, str]]) -> Path:
    lines = [
        f"{800 + i} 769 0:{49 + i} / {mp.replace(' ', chr(92) + '040')} rw,relatime - {fs} {fs} rw"
        for i, (mp, fs) in enumerate(mounts)
    ]
    p = tmp_path / "mountinfo"
    p.write_text("\n".join(lines) + "\n")
    return p


@pytest.fixture
def container(tmp_path, monkeypatch):
    """A Docker-like view: / is overlay, a volume is mounted at <tmp>/data and
    a host drive at <tmp>/media dir (with a space in its name)."""
    data = tmp_path / "data"
    media = tmp_path / "my media"
    layer = tmp_path / "library"          # exists, but on no mount: the writable layer
    for d in (data, media, layer):
        d.mkdir()
    monkeypatch.setattr(sl, "MOUNTINFO", str(_mountinfo(tmp_path, [
        ("/", "overlay"), (str(data), "ext4"), (str(media), "xfs"),
    ])))
    monkeypatch.setattr(sl, "DOCKERENV", str(tmp_path / "no-dockerenv"))
    return SimpleNamespace(data=data, media=media, layer=layer, missing=tmp_path / "mnt" / "user" / "media")


def test_mounted_drive_is_fine(container):
    assert sl.destination_problem(container.media) is None
    assert sl.usage_problem(container.data) == (None, None)
    assert sl.check_local_path(container.media).mount_point == str(container.media)


def test_missing_destination_is_an_error_and_is_not_created(container):
    problem = sl.destination_problem(container.missing)
    assert problem and "doesn't exist inside the container" in problem
    assert "/media, not /mnt/user/media" in problem
    assert not container.missing.exists()


def test_writable_layer_destination_is_refused(container):
    problem = sl.destination_problem(container.layer)
    assert problem and "isn't on a mounted drive" in problem
    assert "lost when the container is updated or recreated" in problem
    err, warning = sl.usage_problem(container.layer)
    assert err is None and "isn't on a mounted drive" in warning


def test_unset_destination_is_an_error():
    assert "No transfer folder is set" in sl.destination_problem("")
    assert "No transfer folder is set" in sl.destination_problem(None)


def test_no_mountinfo_flags_nothing(tmp_path, monkeypatch):
    """Off Linux (or /proc unreadable) an existing path is never flagged."""
    monkeypatch.setattr(sl, "MOUNTINFO", str(tmp_path / "absent"))
    monkeypatch.setattr(sl, "DOCKERENV", str(tmp_path / "no-dockerenv"))
    assert sl.destination_problem(tmp_path) is None


def test_bare_metal_root_is_not_a_container_layer(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "MOUNTINFO", str(_mountinfo(tmp_path, [("/", "ext4")])))
    monkeypatch.setattr(sl, "DOCKERENV", str(tmp_path / "no-dockerenv"))
    assert sl.destination_problem(tmp_path) is None
    assert "Create it, or mount the drive" in sl.destination_problem(tmp_path / "nope")


# --- call sites ------------------------------------------------------------

def test_local_transfer_refuses_a_missing_root(container, tmp_path):
    from core.transfer.protocols.local import transfer_local

    src = tmp_path / "src.mkv"
    src.write_bytes(b"x")
    cfg = SimpleNamespace(transfer_dir=str(container.missing), mode="local")
    with pytest.raises(RuntimeError, match="doesn't exist inside the container"):
        transfer_local(None, "job", src, cfg)
    assert not container.missing.exists()


def test_local_validation_refuses_without_creating(container):
    from core.transfer.validation import validate_local_transfer, validate_path

    ok, msg = validate_local_transfer(SimpleNamespace(transfer_dir=str(container.missing)), 1)
    assert not ok and "doesn't exist inside the container" in msg
    ok, msg = validate_local_transfer(SimpleNamespace(transfer_dir=str(container.layer)), 1)
    assert not ok and "isn't on a mounted drive" in msg
    ok, _ = validate_path(SimpleNamespace(mode="local"), str(container.missing))
    assert not ok
    assert not container.missing.exists()


def test_capability_probe_does_not_create_the_root(container):
    from core.transfer.capabilities import _probe_local

    caps = _probe_local(SimpleNamespace(transfer_dir=str(container.missing)))
    assert not caps.can_write_new
    assert not container.missing.exists()


def test_local_storage_info_never_walks_up(container):
    from core.storage_detection import get_local_storage_info

    info, err = get_local_storage_info(SimpleNamespace(transfer_dir=str(container.missing)))
    assert info is None and "doesn't exist inside the container" in err
    info, err = get_local_storage_info(SimpleNamespace(transfer_dir=str(container.media)))
    assert err is None and info["path"] == str(container.media)


def test_usage_for_reports_the_path_itself(container):
    from api.routers.system import _usage_for

    missing = _usage_for(container.missing)
    assert missing.path == str(container.missing)
    assert missing.total == 0 and "doesn't exist" in missing.error
    layer = _usage_for(container.layer)
    assert layer.total > 0 and layer.error is None and "isn't on a mounted drive" in layer.warning
    ok = _usage_for(container.media)
    assert ok.error is None and ok.warning is None
