import os

import pytest

from core import utils
from core.utils import sanitize_path_component


def _force_exdev(_src: str, _dest: str) -> None:
    # mimic cross-device rename to exercise the copy+verify path
    raise OSError(getattr(os, "EXDEV", 18), "cross-device")


def test_move_with_progress_copy_and_verify(tmp_path, monkeypatch):
    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    data = os.urandom(1024 * 128 + 123)
    src.write_bytes(data)

    # force the function down the copy path
    monkeypatch.setattr(utils.os, "rename", _force_exdev)

    seen = []
    utils.move_with_progress(str(src), str(dest), hash_verify=True, progress_cb=seen.append)

    assert dest.read_bytes() == data
    assert not src.exists()
    assert 100 in seen


def test_move_with_progress_detects_hash_mismatch(tmp_path, monkeypatch):
    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"good-data")

    # force copy path
    monkeypatch.setattr(utils.os, "rename", _force_exdev)

    # corrupt the expected digest while leaving the destination hash correct
    real_new = utils.hashlib.new
    call_count = {"n": 0}

    def fake_new(name: str):
        call_count["n"] += 1
        if call_count["n"] == 1:
            class Dummy:
                def __init__(self):
                    self._inner = real_new(name)

                def update(self, data):
                    self._inner.update(data)

                def hexdigest(self):
                    return "deadbeef"
            return Dummy()
        return real_new(name)

    monkeypatch.setattr(utils.hashlib, "new", fake_new)

    with pytest.raises(ValueError):
        utils.move_with_progress(str(src), str(dest), hash_verify=True)

    assert src.exists(), "source should remain when verification fails"
    assert not dest.exists(), "corrupted destination should be removed"


class TestSanitizePathComponent:
    """Tests for sanitize_path_component (Linux/Windows-safe path segments)."""

    def test_removes_unsafe_chars(self):
        assert sanitize_path_component(r'Movie\Name: "Test"') == "MovieName Test"
        assert sanitize_path_component("a*b?c<d>e|f") == "abcdef"
        assert sanitize_path_component("path/with/slashes") == "pathwithslashes"

    def test_removes_control_chars(self):
        assert sanitize_path_component("foo\x00bar\x1f") == "foobar"
        assert sanitize_path_component("a\tb\nc") == "abc"

    def test_trims_spaces_and_dots(self):
        assert sanitize_path_component("  title  ") == "title"
        assert sanitize_path_component("..leading") == "leading"
        assert sanitize_path_component("trailing..") == "trailing"

    def test_safe_names_unchanged(self):
        assert sanitize_path_component("Best Movie Ever (2019)") == "Best Movie Ever (2019)"
        assert sanitize_path_component("Show Name - s01e01 - Episode") == "Show Name - s01e01 - Episode"
        assert sanitize_path_component("Track1") == "Track1"

    def test_empty_and_none(self):
        assert sanitize_path_component("") == ""
        assert sanitize_path_component(None) == ""
