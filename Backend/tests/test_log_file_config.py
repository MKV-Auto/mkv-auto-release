"""Tests for core.log_file_config rotation helper."""

from pathlib import Path

from core.log_file_config import LOG_ROTATE_BACKUP_COUNT, rotate_file_if_needed


def test_rotate_skips_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "app.log"
    rotate_file_if_needed(p, max_bytes=100, backup_count=3)
    assert not p.exists()


def test_rotate_skips_small_file(tmp_path: Path) -> None:
    p = tmp_path / "app.log"
    p.write_text("x" * 50, encoding="utf-8")
    rotate_file_if_needed(p, max_bytes=100, backup_count=3)
    assert p.exists()
    assert p.read_text(encoding="utf-8") == "x" * 50
    assert not (tmp_path / "app.log.1").exists()


def test_rotate_when_at_or_over_limit(tmp_path: Path) -> None:
    p = tmp_path / "app.log"
    p.write_text("x" * 100, encoding="utf-8")
    rotate_file_if_needed(p, max_bytes=100, backup_count=3)
    assert not p.exists()
    assert (tmp_path / "app.log.1").exists()
    assert (tmp_path / "app.log.1").read_text(encoding="utf-8") == "x" * 100


def test_rotate_shifts_backups_and_drops_oldest(tmp_path: Path) -> None:
    p = tmp_path / "app.log"
    (tmp_path / "app.log.1").write_text("old1", encoding="utf-8")
    (tmp_path / "app.log.2").write_text("old2", encoding="utf-8")
    (tmp_path / "app.log.3").write_text("old3", encoding="utf-8")
    p.write_text("x" * 50, encoding="utf-8")
    rotate_file_if_needed(p, max_bytes=10, backup_count=LOG_ROTATE_BACKUP_COUNT)
    assert (tmp_path / "app.log.1").read_text(encoding="utf-8") == "x" * 50
    assert (tmp_path / "app.log.2").read_text(encoding="utf-8") == "old1"
    assert (tmp_path / "app.log.3").read_text(encoding="utf-8") == "old2"
    assert not (tmp_path / "app.log.4").exists()


def test_constants_are_ten_mb_and_three_backups() -> None:
    from core import log_file_config

    assert log_file_config.LOG_ROTATE_MAX_BYTES == 10 * 1024 * 1024
    assert log_file_config.LOG_ROTATE_BACKUP_COUNT == 3
