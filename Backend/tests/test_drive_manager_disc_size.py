from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

from drive_manager.main import (
    _block_device_name_from_mount_point,
    _get_disc_size_bytes,
    _read_sys_block_size,
)


def test_read_sys_block_size_parses_sectors(tmp_path):
    device_dir = tmp_path / "sr0"
    device_dir.mkdir()
    (device_dir / "size").write_text("2048\n")

    size_bytes = _read_sys_block_size("sr0", sys_block_root=tmp_path)

    assert size_bytes == 2048 * 512


def test_read_sys_block_size_missing_returns_none(tmp_path):
    assert _read_sys_block_size("nonexistent", sys_block_root=tmp_path) is None


def test_read_sys_block_size_zero_sectors_returns_none(tmp_path):
    device_dir = tmp_path / "sr0"
    device_dir.mkdir()
    (device_dir / "size").write_text("0\n")
    assert _read_sys_block_size("sr0", sys_block_root=tmp_path) is None


class TestBlockDeviceNameFromMountPoint:
    """Tests for _block_device_name_from_mount_point (mount_point only, no disc_num)."""

    def test_block_device_path_returns_device_name(self):
        with patch("drive_manager.main.os.path.exists", return_value=True), patch(
            "drive_manager.main.os.stat"
        ) as mock_stat:
            mock_stat.return_value.st_mode = 0o060000  # S_IFBLK
            assert _block_device_name_from_mount_point("/dev/sr1") == "sr1"
            assert _block_device_name_from_mount_point("/dev/sr0") == "sr0"

    def test_block_device_path_nonexistent_returns_none(self):
        with patch("drive_manager.main.os.path.exists", return_value=False), patch(
            "drive_manager.main.open", mock_open(read_data="")
        ):
            assert _block_device_name_from_mount_point("/dev/sr1") is None

    def test_directory_resolved_from_proc_mounts(self):
        proc_mounts = "/dev/sr0 /media/cdrom iso9660 ro 0 0\n"
        with patch("drive_manager.main.open", mock_open(read_data=proc_mounts)), patch(
            "drive_manager.main.os.path.exists", return_value=True
        ), patch("drive_manager.main.os.stat") as mock_stat:
            mock_stat.return_value.st_mode = 0o040000  # S_IFDIR, not block
            with patch("drive_manager.main.Path") as mock_path:
                def path_factory(path_arg):
                    if path_arg == "/media/cdrom":
                        m = MagicMock()
                        m.resolve.return_value = MagicMock(
                            __str__=lambda _: "/media/cdrom", name="cdrom"
                        )
                        return m
                    if path_arg == "/dev/sr0":
                        m = MagicMock()
                        m.name = "sr0"
                        return m
                    if "/sys/class/block" in str(path_arg):
                        m = MagicMock()
                        m.__truediv__ = lambda self, other: MagicMock(
                            exists=MagicMock(return_value=False)
                        )
                        return m
                    return Path(path_arg)

                mock_path.side_effect = path_factory
                result = _block_device_name_from_mount_point("/media/cdrom")
                assert result == "sr0"

    def test_proc_mounts_unavailable_returns_none(self):
        with patch("drive_manager.main.os.path.exists", return_value=True), patch(
            "drive_manager.main.os.stat"
        ) as mock_stat:
            mock_stat.return_value.st_mode = 0o040000
            with patch("drive_manager.main.open", side_effect=OSError("no /proc")):
                assert _block_device_name_from_mount_point("/media/cdrom") is None


class TestGetDiscSizeBytes:
    """Tests for _get_disc_size_bytes(mount_point)."""

    def test_returns_size_when_device_resolved(self):
        with patch(
            "drive_manager.main._block_device_name_from_mount_point", return_value="sr0"
        ), patch(
            "drive_manager.main._read_sys_block_size", return_value=1024 * 1024
        ):
            assert _get_disc_size_bytes("/media/cdrom") == 1024 * 1024

    def test_returns_none_when_device_not_resolved(self):
        with patch(
            "drive_manager.main._block_device_name_from_mount_point", return_value=None
        ):
            assert _get_disc_size_bytes("/media/cdrom") is None

    def test_returns_none_when_sys_block_size_unavailable(self):
        with patch(
            "drive_manager.main._block_device_name_from_mount_point", return_value="sr0"
        ), patch("drive_manager.main._read_sys_block_size", return_value=None):
            assert _get_disc_size_bytes("/dev/sr0") is None
