"""#902: the DV7 library is the active transfer destination.

``describe`` says what the library is without mounting; ``ensure_root`` turns
an SMB/NFS destination into a folder by mounting it through the root helper,
reusing a mount that's already there and remounting when the destination
changed. The root helper and /proc/self/mountinfo are faked.
"""
from __future__ import annotations

import pytest

from api import models
from core import library_destination as ld
from core import utils


def _config(db, mode, *, transfer_dir=None, **config_data):
    cfg = models.TransferConfig(mode=mode, name="Plex", is_active=True,
                                transfer_dir=transfer_dir, config_data=config_data or None)
    db.add(cfg)
    db.commit()
    return cfg


SMB = {"host": "nas.local", "share": "PLEX Media", "port": 445, "path": "/"}


@pytest.fixture
def mount_env(monkeypatch, tmp_path):
    """A fake root helper plus a mountinfo file it keeps up to date."""
    mount_point = tmp_path / "mnt" / "library"
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("22 1 0:21 / / rw - overlay overlay rw\n")
    monkeypatch.setattr(ld, "LIBRARY_MOUNT", str(mount_point))
    monkeypatch.setattr(ld, "MOUNTINFO", str(mountinfo))
    monkeypatch.setattr(utils, "get_mkvauto_tmp", lambda: str(tmp_path / "tmp"))
    calls = []

    def _add(source, fstype):
        esc = str(mount_point).replace(" ", "\\040")
        with mountinfo.open("a") as fh:
            fh.write(f"99 22 0:50 / {esc} rw - {fstype} {source.replace(' ', chr(92) + '040')} rw\n")

    def mount_smb(host, share, mp, port, user, pw, domain):
        calls.append(("smb", host, share, mp, user))
        if host == "unreachable":
            return None, "Mount failed: Host is down"
        _add(f"//{host}/{share}", "cifs")
        (mount_point / "Movies").mkdir(parents=True, exist_ok=True)
        return mp, None

    def mount_nfs(server, export, mp, options):
        calls.append(("nfs", server, export, mp, options))
        _add(f"{server}:{export}", "nfs4")
        return mp, None

    def unmount(mp):
        calls.append(("unmount", mp))
        mountinfo.write_text("22 1 0:21 / / rw - overlay overlay rw\n")

    monkeypatch.setattr(utils, "_root_helper_mount_smb", mount_smb)
    monkeypatch.setattr(utils, "_root_helper_mount_nfs", mount_nfs)
    monkeypatch.setattr(utils, "_root_helper_unmount", unmount)
    return {"calls": calls, "mount_point": mount_point, "add": _add}


def test_no_destination(test_db):
    with test_db() as db:
        d = ld.describe(db)
    assert d.available is False and d.problem == ld.NO_DESTINATION


def test_local_destination_is_its_folder(test_db, tmp_path):
    lib = tmp_path / "media"
    lib.mkdir()
    with test_db() as db:
        _config(db, "local", transfer_dir=str(lib))
        d = ld.ensure_root(db)
    assert d.available and d.mode == "local" and d.root == str(lib) and d.display == str(lib)


def test_local_destination_that_is_missing_says_why(test_db, tmp_path):
    with test_db() as db:
        _config(db, "local", transfer_dir=str(tmp_path / "nope"))
        d = ld.describe(db)
    assert d.available is False and "doesn't exist" in d.problem


def test_rsync_is_not_scannable(test_db):
    with test_db() as db:
        _config(db, "rsync", host="nas", path="/media")
        d = ld.ensure_root(db)
    assert d.available is False and d.problem == ld.RSYNC_UNSUPPORTED


def test_describe_never_mounts(test_db, mount_env):
    with test_db() as db:
        _config(db, "smb", **SMB)
        d = ld.describe(db)
    assert d.available and d.display == "smb://nas.local/PLEX Media" and d.root is None
    assert mount_env["calls"] == []


def test_smb_is_mounted_once_and_reused(test_db, mount_env):
    with test_db() as db:
        _config(db, "smb", **{**SMB, "path": "/Movies"})
        first = ld.ensure_root(db)
        second = ld.ensure_root(db)
    assert first.available and first.root == f"{mount_env['mount_point']}/Movies"
    assert second.root == first.root
    assert [c[0] for c in mount_env["calls"]] == ["smb"]           # the second call reused it
    assert mount_env["calls"][0][1:3] == ("nas.local", "PLEX Media")


def test_a_changed_destination_is_remounted(test_db, mount_env):
    mount_env["add"]("//old-nas/Media", "cifs")
    with test_db() as db:
        _config(db, "smb", **SMB)
        d = ld.ensure_root(db)
    assert d.available
    assert [c[0] for c in mount_env["calls"]] == ["unmount", "smb"]


def test_a_failed_mount_is_reported(test_db, mount_env):
    with test_db() as db:
        _config(db, "smb", **{**SMB, "host": "unreachable"})
        d = ld.ensure_root(db)
    assert d.available is False
    assert d.problem == "Couldn't mount //unreachable/PLEX Media: Mount failed: Host is down"


def test_a_missing_subfolder_on_the_share_is_reported(test_db, mount_env):
    with test_db() as db:
        _config(db, "smb", **{**SMB, "path": "/NoSuchFolder"})
        d = ld.ensure_root(db)
    assert d.available is False and "doesn't exist on the share" in d.problem


def test_nfs_is_mounted_with_its_export(test_db, mount_env):
    with test_db() as db:
        _config(db, "nfs", server="nas.local", export_path="/mnt/user/media", path="")
        d = ld.ensure_root(db)
    assert d.available and d.root == str(mount_env["mount_point"])
    assert mount_env["calls"][0][:3] == ("nfs", "nas.local", "/mnt/user/media")
