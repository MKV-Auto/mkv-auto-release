"""Unit tests for core/dv_remux.py (DV7→DV8.1 remux).

No real ffmpeg/dovi_tool/mkvmerge binaries: detection tests mock
subprocess.run with canned ffprobe JSON; pipeline tests patch the
per-step helpers so the orchestration (verify-before-replace, temp cleanup)
is exercised against real files in tmp_path.

Ported from PR #671 for #894; the archive-policy tests were dropped with the
archive policy (originals are replaced in place after verification).
"""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import dv_remux


def _ffprobe_result(payload: dict, returncode: int = 0):
    return SimpleNamespace(returncode=returncode, stdout=json.dumps(payload), stderr="")


def _dv7_stream_payload(profile=7, r_frame_rate="24000/1001"):
    return {
        "streams": [
            {
                "codec_type": "video",
                "r_frame_rate": r_frame_rate,
                "side_data_list": [
                    {"side_data_type": "DOVI configuration record", "dv_profile": profile},
                ],
            }
        ]
    }


# ────────────────────────────────────────────────────────────────
# Detection
# ────────────────────────────────────────────────────────────────

class TestDetection:
    def test_profile_7_needs_remux(self, monkeypatch):
        monkeypatch.setattr(
            dv_remux.subprocess, "run",
            lambda *a, **k: _ffprobe_result(_dv7_stream_payload(profile=7)),
        )
        assert dv_remux.needs_dv7_remux(Path("/x/movie.mkv")) is True

    @pytest.mark.parametrize("profile", [5, 8])
    def test_other_dv_profiles_skipped(self, monkeypatch, profile):
        monkeypatch.setattr(
            dv_remux.subprocess, "run",
            lambda *a, **k: _ffprobe_result(_dv7_stream_payload(profile=profile)),
        )
        assert dv_remux.needs_dv7_remux(Path("/x/movie.mkv")) is False

    def test_no_dovi_side_data_skipped(self, monkeypatch):
        payload = {"streams": [{"codec_type": "video", "r_frame_rate": "24/1", "side_data_list": []}]}
        monkeypatch.setattr(dv_remux.subprocess, "run", lambda *a, **k: _ffprobe_result(payload))
        assert dv_remux.needs_dv7_remux(Path("/x/movie.mkv")) is False

    def test_non_mkv_skipped_without_probing(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("ffprobe must not run for non-mkv inputs")
        monkeypatch.setattr(dv_remux.subprocess, "run", _boom)
        assert dv_remux.needs_dv7_remux(Path("/x/movie.mp4")) is False

    def test_probe_failure_is_not_dv7(self, monkeypatch):
        monkeypatch.setattr(
            dv_remux.subprocess, "run",
            lambda *a, **k: _ffprobe_result({}, returncode=1),
        )
        assert dv_remux.needs_dv7_remux(Path("/x/movie.mkv")) is False

    def test_probe_dv_info_captures_frame_rate(self, monkeypatch):
        monkeypatch.setattr(
            dv_remux.subprocess, "run",
            lambda *a, **k: _ffprobe_result(_dv7_stream_payload(r_frame_rate="24000/1001")),
        )
        profile, r_frame_rate = dv_remux.probe_dv_info(Path("/x/movie.mkv"))
        assert profile == 7
        assert r_frame_rate == "24000/1001"


# ────────────────────────────────────────────────────────────────
# Command construction
# ────────────────────────────────────────────────────────────────

class TestCommandConstruction:
    def test_extract_convert_commands(self):
        ffmpeg_cmd, dovi_cmd = dv_remux.build_extract_convert_commands(
            Path("/src/movie.mkv"), Path("/work/BL_RPU.hevc")
        )
        assert ffmpeg_cmd == [
            "ffmpeg", "-nostdin", "-v", "error",
            "-i", "/src/movie.mkv",
            "-map", "0:v:0", "-c:v", "copy",
            "-bsf:v", "hevc_mp4toannexb",
            "-f", "hevc", "-",
        ]
        assert dovi_cmd == [
            "dovi_tool", "-m", "2", "convert", "--discard",
            "-", "-o", "/work/BL_RPU.hevc",
        ]

    def test_mkvmerge_command_with_title(self):
        cmd = dv_remux.build_mkvmerge_command(
            Path("/src/movie.mkv"), Path("/work/BL_RPU.hevc"), Path("/work/out.mkv"),
            "24000/1001", title="My Movie",
        )
        assert cmd == [
            "mkvmerge", "-o", "/work/out.mkv",
            "--title", "My Movie",
            "--default-duration", "0:24000/1001p",
            "/work/BL_RPU.hevc",
            "-D", "/src/movie.mkv",
        ]

    def test_mkvmerge_command_keeps_the_source_video_track_metadata(self):
        """#894 prod test: the converted file lost the video track's ``eng``
        language and was flagged default; both are copied from the source."""
        cmd = dv_remux.build_mkvmerge_command(
            Path("/src/movie.mkv"), Path("/work/BL_RPU.hevc"), Path("/work/out.mkv"),
            "24000/1001", video_language="eng", video_name="Main", video_default=0,
        )
        assert cmd == [
            "mkvmerge", "-o", "/work/out.mkv",
            "--language", "0:eng",
            "--track-name", "0:Main",
            "--default-track-flag", "0:0",
            "--default-duration", "0:24000/1001p",
            "/work/BL_RPU.hevc",
            "-D", "/src/movie.mkv",
        ]

    def test_video_metadata_is_read_from_the_source(self, monkeypatch):
        payload = {
            "streams": [
                {"codec_type": "video", "tags": {"language": "eng"}, "disposition": {"default": 0},
                 "side_data_list": [{"side_data_type": "DOVI configuration record", "dv_profile": 7}]},
                {"codec_type": "audio"},
            ],
            "format": {"duration": "10.0"},
        }
        monkeypatch.setattr(dv_remux.subprocess, "run", lambda *a, **k: _ffprobe_result(payload))
        info = dv_remux._probe_verification_info(Path("/x/m.mkv"))
        assert info["video_language"] == "eng" and info["video_default"] == 0 and info["video_name"] is None

    def test_mkvmerge_command_without_title(self):
        cmd = dv_remux.build_mkvmerge_command(
            Path("/src/movie.mkv"), Path("/work/BL_RPU.hevc"), Path("/work/out.mkv"),
            "24000/1001",
        )
        assert "--title" not in cmd


# ────────────────────────────────────────────────────────────────
# Verification
# ────────────────────────────────────────────────────────────────

def _src_info(**overrides):
    info = {
        "dv_profile": 7,
        "duration": 7200.0,
        "audio_streams": 3,
        "subtitle_streams": 5,
        "title": "Movie",
    }
    info.update(overrides)
    return info


class TestVerifyOutput:
    def _patch_out_info(self, monkeypatch, out_info):
        monkeypatch.setattr(dv_remux, "_probe_verification_info", lambda _p: out_info)

    def test_passes_when_everything_matches(self, monkeypatch):
        self._patch_out_info(monkeypatch, _src_info(dv_profile=8, duration=7200.5))
        assert dv_remux._verify_output(_src_info(), Path("/x/out.mkv")) is None

    def test_fails_on_wrong_profile(self, monkeypatch):
        self._patch_out_info(monkeypatch, _src_info(dv_profile=7))
        error = dv_remux._verify_output(_src_info(), Path("/x/out.mkv"))
        assert error and "profile 8" in error

    def test_fails_on_duration_drift(self, monkeypatch):
        self._patch_out_info(monkeypatch, _src_info(dv_profile=8, duration=7195.0))
        error = dv_remux._verify_output(_src_info(), Path("/x/out.mkv"))
        assert error and "duration" in error.lower()

    def test_fails_on_audio_count_change(self, monkeypatch):
        self._patch_out_info(monkeypatch, _src_info(dv_profile=8, audio_streams=2))
        error = dv_remux._verify_output(_src_info(), Path("/x/out.mkv"))
        assert error and "audio" in error.lower()

    def test_fails_on_subtitle_count_change(self, monkeypatch):
        self._patch_out_info(monkeypatch, _src_info(dv_profile=8, subtitle_streams=0))
        error = dv_remux._verify_output(_src_info(), Path("/x/out.mkv"))
        assert error and "subtitle" in error.lower()

    def test_fails_when_output_unprobeable(self, monkeypatch):
        self._patch_out_info(monkeypatch, None)
        error = dv_remux._verify_output(_src_info(), Path("/x/out.mkv"))
        assert error


# ────────────────────────────────────────────────────────────────
# mkvmerge exit-code semantics
# ────────────────────────────────────────────────────────────────

class TestRunMkvmerge:
    def _patch_run(self, monkeypatch, returncode, stdout="", stderr=""):
        monkeypatch.setattr(
            dv_remux.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr),
        )

    def test_exit_0_ok(self, monkeypatch):
        self._patch_run(monkeypatch, 0)
        assert dv_remux._run_mkvmerge(["mkvmerge"]) == (None, None)

    def test_exit_1_ok_with_warnings(self, monkeypatch):
        self._patch_run(monkeypatch, 1, stdout="Warning: something")
        error, warnings = dv_remux._run_mkvmerge(["mkvmerge"])
        assert error is None
        assert warnings and "Warning" in warnings

    def test_exit_2_failure(self, monkeypatch):
        self._patch_run(monkeypatch, 2, stderr="Error: bad file")
        error, warnings = dv_remux._run_mkvmerge(["mkvmerge"])
        assert error and "code 2" in error
        assert warnings is None


# ────────────────────────────────────────────────────────────────
# Full pipeline orchestration (per-step helpers patched)
# ────────────────────────────────────────────────────────────────

SRC_CONTENT = b"original dv7 bytes"
OUT_CONTENT = b"remuxed dv81 bytes"


@pytest.fixture
def src_file(tmp_path):
    library = tmp_path / "library" / "Movies" / "Movie (2020)"
    library.mkdir(parents=True)
    src = library / "Movie (2020).4k.mkv"
    src.write_bytes(SRC_CONTENT)
    return src


def _patch_pipeline(monkeypatch, *, verify_error=None, mkvmerge_error=None,
                    mkvmerge_warnings=None, extract_error=None):
    """Patch every subprocess-touching step; the mkvmerge stub writes the
    remuxed output file so the replace path operates on real files."""
    monkeypatch.setattr(dv_remux, "probe_dv_info", lambda _p: (7, "24000/1001"))
    monkeypatch.setattr(dv_remux, "_probe_verification_info", lambda _p: _src_info())
    monkeypatch.setattr(dv_remux, "_check_free_space", lambda _p: None)
    monkeypatch.setattr(dv_remux, "_extract_and_convert", lambda _s, _b: extract_error)

    def _fake_mkvmerge(cmd):
        if mkvmerge_error is None:
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(OUT_CONTENT)
        return mkvmerge_error, mkvmerge_warnings

    monkeypatch.setattr(dv_remux, "_run_mkvmerge", _fake_mkvmerge)
    monkeypatch.setattr(dv_remux, "_verify_output", lambda _i, _o: verify_error)


class TestRemuxPipeline:
    def test_replace_policy_swaps_file_in_place(self, monkeypatch, src_file):
        _patch_pipeline(monkeypatch)
        result = dv_remux.remux_dv7_to_dv81(src_file)
        assert result.status == "converted"
        assert src_file.read_bytes() == OUT_CONTENT
        # Work dir cleaned up.
        assert not list(src_file.parent.glob(".dv7remux-*"))

    def test_verification_failure_keeps_original_and_cleans_temp(self, monkeypatch, src_file):
        _patch_pipeline(monkeypatch, verify_error="duration mismatch")
        result = dv_remux.remux_dv7_to_dv81(src_file)
        assert result.status == "failed"
        assert "verification failed" in result.reason
        # In-place replace happens ONLY after verify: original untouched.
        assert src_file.read_bytes() == SRC_CONTENT
        assert not list(src_file.parent.glob(".dv7remux-*"))

    def test_mkvmerge_failure_keeps_original(self, monkeypatch, src_file):
        _patch_pipeline(monkeypatch, mkvmerge_error="mkvmerge exited with code 2: boom")
        result = dv_remux.remux_dv7_to_dv81(src_file)
        assert result.status == "failed"
        assert src_file.read_bytes() == SRC_CONTENT
        assert not list(src_file.parent.glob(".dv7remux-*"))

    def test_extract_failure_keeps_original(self, monkeypatch, src_file):
        _patch_pipeline(monkeypatch, extract_error="dovi_tool exited with code 1: bad rpu")
        result = dv_remux.remux_dv7_to_dv81(src_file)
        assert result.status == "failed"
        assert src_file.read_bytes() == SRC_CONTENT

    def test_non_dv7_source_skipped(self, monkeypatch, src_file):
        _patch_pipeline(monkeypatch)
        monkeypatch.setattr(dv_remux, "probe_dv_info", lambda _p: (8, "24/1"))
        result = dv_remux.remux_dv7_to_dv81(src_file)
        assert result.status == "skipped"
        assert src_file.read_bytes() == SRC_CONTENT

    def test_free_space_shortage_skips(self, monkeypatch, src_file):
        _patch_pipeline(monkeypatch)
        monkeypatch.setattr(dv_remux, "_check_free_space", lambda _p: "not enough free space")
        result = dv_remux.remux_dv7_to_dv81(src_file)
        assert result.status == "skipped"
        assert src_file.read_bytes() == SRC_CONTENT



# ────────────────────────────────────────────────────────────────
# #894 additions
# ────────────────────────────────────────────────────────────────

class TestFreeSpace:
    def test_needs_twice_the_source_size(self, monkeypatch, tmp_path):
        f = tmp_path / "movie.mkv"
        f.write_bytes(b"x" * 1000)
        monkeypatch.setattr(dv_remux.os, "statvfs", lambda _p: SimpleNamespace(f_bavail=1500, f_frsize=1))
        assert "not enough free space" in dv_remux._check_free_space(f)
        monkeypatch.setattr(dv_remux.os, "statvfs", lambda _p: SimpleNamespace(f_bavail=2000, f_frsize=1))
        assert dv_remux._check_free_space(f) is None


class TestProbeMediaInfo:
    def test_reports_codec_resolution_profile_and_duration(self, monkeypatch):
        payload = {
            "streams": [{"codec_name": "hevc", "width": 3840, "height": 2160,
                         "side_data_list": [{"side_data_type": "DOVI configuration record", "dv_profile": 7}]}],
            "format": {"duration": "7563.200000"},
        }
        monkeypatch.setattr(dv_remux.subprocess, "run", lambda *a, **k: _ffprobe_result(payload))
        info = dv_remux.probe_media_info(Path("/x/movie.mkv"))
        assert info == {"video_codec": "hevc", "width": 3840, "height": 2160, "dv_profile": 7, "duration": 7563.2}

    def test_non_dv_and_unprobeable(self, monkeypatch):
        payload = {"streams": [{"codec_name": "h264", "width": 1920, "height": 1080}], "format": {}}
        monkeypatch.setattr(dv_remux.subprocess, "run", lambda *a, **k: _ffprobe_result(payload))
        info = dv_remux.probe_media_info(Path("/x/movie.mkv"))
        assert info["dv_profile"] is None and info["duration"] is None and info["video_codec"] == "h264"
        monkeypatch.setattr(dv_remux.subprocess, "run", lambda *a, **k: _ffprobe_result({}, returncode=1))
        assert dv_remux.probe_media_info(Path("/x/broken.mkv")) is None


class TestScratchMode:
    """#896: work in a local scratch dir, then copy back and rename."""

    def test_result_is_copied_back_over_the_original(self, monkeypatch, src_file, tmp_path):
        _patch_pipeline(monkeypatch)
        scratch = tmp_path / "scratch"
        result = dv_remux.remux_dv7_to_dv81(src_file, scratch_dir=scratch, check_space=False)
        assert result.status == "converted"
        assert src_file.read_bytes() == OUT_CONTENT
        # Nothing left behind: no scratch work dir, no hidden push temp.
        assert not list(scratch.glob(".dv7remux-*"))
        assert not list(src_file.parent.glob(".dv7remux-*"))

    def test_intermediates_are_written_to_scratch_not_beside_the_source(self, monkeypatch, src_file, tmp_path):
        _patch_pipeline(monkeypatch)
        seen = []
        real = dv_remux._run_mkvmerge
        monkeypatch.setattr(dv_remux, "_run_mkvmerge", lambda cmd: (seen.append(cmd[cmd.index("-o") + 1]), real(cmd))[1])
        dv_remux.remux_dv7_to_dv81(src_file, scratch_dir=tmp_path / "scratch", check_space=False)
        assert seen and seen[0].startswith(str(tmp_path / "scratch"))

    def test_failed_copy_back_keeps_the_original(self, monkeypatch, src_file, tmp_path):
        _patch_pipeline(monkeypatch)

        def broken_copy(src, dst):
            Path(dst).write_bytes(b"partial")
            raise OSError("network share went away")
        monkeypatch.setattr(dv_remux, "_copy_durably", broken_copy)
        result = dv_remux.remux_dv7_to_dv81(src_file, scratch_dir=tmp_path / "scratch", check_space=False)
        assert result.status == "failed" and "copying the result back failed" in result.reason
        assert src_file.read_bytes() == SRC_CONTENT
        assert not list(src_file.parent.glob(".dv7remux-*"))   # partial push removed

    def test_short_copy_back_is_refused(self, monkeypatch, src_file, tmp_path):
        _patch_pipeline(monkeypatch)
        monkeypatch.setattr(dv_remux, "_copy_durably", lambda s, d: Path(d).write_bytes(b"short"))
        result = dv_remux.remux_dv7_to_dv81(src_file, scratch_dir=tmp_path / "scratch", check_space=False)
        assert result.status == "failed" and "incomplete" in result.reason
        assert src_file.read_bytes() == SRC_CONTENT
