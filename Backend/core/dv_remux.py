"""Dolby Vision profile 7 -> 8.1 conversion (#894).

Converts dual-layer Dolby Vision profile 7 MKVs (UHD Blu-ray rips) into
single-layer profile 8.1 files that Plex/Jellyfin clients, browsers and
streaming devices (Apple TV, Shield, consoles) can play.

The conversion is lossless: the HEVC base layer is copied bit for bit, the
dynamic DV metadata (RPU) is converted to profile 8.1 and kept, and the
enhancement layer, which no consumer streaming device decodes, is dropped.

The technique is PR #671's, validated there on a 105-file / 3.4 TB DV7
library (every output decode-verified identical to its source). This module
keeps that tested core and replaces only the original-file handling: per the
#894 decisions the original is replaced in place, and only after
verification passes. There is no archive copy.

Pipeline:

1. ``ffprobe`` detection: the first video stream carries a ``DOVI
   configuration record`` with ``dv_profile == 7``. Only ``.mkv`` inputs.
2. Extract + convert in one stream (no second full copy on disk)::

       ffmpeg -nostdin -v error -i <src> -map 0:v:0 -c:v copy \
         -bsf:v hevc_mp4toannexb -f hevc - \
         | dovi_tool -m 2 convert --discard - -o <work>/BL_RPU.hevc

3. Remux with the original audio/subs/chapters/attachments::

       mkvmerge -o <work>/out.mkv [--title <t>] \
         --default-duration 0:<r_frame_rate>p <work>/BL_RPU.hevc -D <src>

4. Verify (mandatory before the original is touched): mkvmerge exit code,
   output carries DV profile 8, duration within 2 s of the source,
   audio/subtitle stream counts unchanged.
5. ``os.replace`` the verified output over the original (same directory, so
   an atomic rename on one filesystem).

Tools are invoked as bare names on PATH (repo convention; see
``core/ffmpeg_detection.py``). Sources can approach ~100 GB, so subprocess
steps are bounded in hours. Nothing here raises for an operational failure:
callers get a ``DvRemuxResult``, and on anything but ``converted`` the
original file is untouched and the work dir is removed.
"""
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core.logging_utils import get_logger

log = get_logger("core.dv_remux", "dv_remux")

# Generous bounds — these are large files (up to ~100 GB).
PROBE_TIMEOUT_SECONDS = 15 * 60
EXTRACT_CONVERT_TIMEOUT_SECONDS = 4 * 60 * 60
MKVMERGE_TIMEOUT_SECONDS = 4 * 60 * 60

# Container duration drift allowed between source and remuxed output.
DURATION_TOLERANCE_SECONDS = 2.0

# Work-dir prefix; created next to the source so the final os.replace is
# a same-filesystem rename.
_WORK_DIR_PREFIX = ".dv7remux-"

# Peak scratch space during a conversion, as a multiple of the source size.
FREE_SPACE_FACTOR = 2

@dataclass
class DvRemuxResult:
    """Outcome of one remux attempt.

    status: "converted" | "skipped" | "failed"
    reason: human-readable explanation for skipped/failed (None on success)
    """
    status: str
    reason: Optional[str] = None

    @property
    def converted(self) -> bool:
        return self.status == "converted"


# ────────────────────────────────────────────────────────────────
# Detection
# ────────────────────────────────────────────────────────────────

def _ffprobe_json(args: list) -> Optional[dict]:
    """Run ffprobe with JSON output; return the parsed dict or None."""
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json"] + args
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.warning("dv_remux: ffprobe failed for %s: %s", args[-1:], exc)
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout or "{}")
    except (json.JSONDecodeError, ValueError):
        return None


def probe_dv_info(path: Path) -> tuple[Optional[int], Optional[str]]:
    """Return ``(dv_profile, r_frame_rate)`` for the first video stream.

    ``dv_profile`` is None when the stream has no DOVI configuration
    record (or probing failed). ``r_frame_rate`` is the raw ffprobe
    string (e.g. ``"24000/1001"``) needed for the mkvmerge
    ``--default-duration`` flag.
    """
    data = _ffprobe_json(["-show_streams", "-select_streams", "v:0", str(path)])
    if not data:
        return None, None
    streams = data.get("streams") or []
    if not streams:
        return None, None
    stream = streams[0]
    r_frame_rate = stream.get("r_frame_rate") or None
    dv_profile = None
    for side_data in stream.get("side_data_list") or []:
        if not isinstance(side_data, dict):
            continue
        if side_data.get("side_data_type") == "DOVI configuration record":
            try:
                dv_profile = int(side_data.get("dv_profile"))
            except (TypeError, ValueError):
                dv_profile = None
            break
    return dv_profile, r_frame_rate


def needs_dv7_remux(path: Path) -> bool:
    """True iff ``path`` is an ``.mkv`` whose first video stream carries a
    Dolby Vision profile 7 configuration record. Profiles 5/8 and non-DV
    files are skipped untouched."""
    path = Path(path)
    if path.suffix.lower() != ".mkv":
        return False
    profile, _ = probe_dv_info(path)
    return profile == 7


def _probe_verification_info(path: Path) -> Optional[dict]:
    """Full-file probe used for pre/post-remux verification.

    Returns dict with dv_profile (first video stream), duration (float
    seconds or None), audio_streams, subtitle_streams, title (container
    title tag or None); or None when probing failed.
    """
    data = _ffprobe_json(["-show_streams", "-show_format", str(path)])
    if not data:
        return None
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    dv_profile = None
    audio = 0
    subs = 0
    seen_video = False
    video_language = video_name = video_default = None
    for stream in streams:
        codec_type = (stream.get("codec_type") or "").lower()
        if codec_type == "audio":
            audio += 1
        elif codec_type == "subtitle":
            subs += 1
        elif codec_type == "video" and not seen_video:
            seen_video = True
            vtags = stream.get("tags") or {}
            video_language = vtags.get("language") or vtags.get("LANGUAGE") or None
            video_name = vtags.get("title") or vtags.get("TITLE") or None
            video_default = (stream.get("disposition") or {}).get("default")
            for side_data in stream.get("side_data_list") or []:
                if isinstance(side_data, dict) and side_data.get("side_data_type") == "DOVI configuration record":
                    try:
                        dv_profile = int(side_data.get("dv_profile"))
                    except (TypeError, ValueError):
                        dv_profile = None
                    break
    duration = None
    try:
        raw = fmt.get("duration")
        if raw is not None:
            duration = float(raw)
    except (TypeError, ValueError):
        duration = None
    title = (fmt.get("tags") or {}).get("title") or None
    return {
        "dv_profile": dv_profile,
        "duration": duration,
        "audio_streams": audio,
        "subtitle_streams": subs,
        "title": title,
        "video_language": video_language,
        "video_name": video_name,
        "video_default": video_default,
    }


# ────────────────────────────────────────────────────────────────
# Command construction (kept pure for unit tests)
# ────────────────────────────────────────────────────────────────

def build_extract_convert_commands(src: Path, bl_rpu_path: Path) -> tuple[list, list]:
    """The piped ffmpeg | dovi_tool pair: extract the HEVC video stream as
    Annex-B, convert the RPU to profile 8.1 (``-m 2``) and discard the
    enhancement layer (``--discard``)."""
    ffmpeg_cmd = [
        "ffmpeg", "-nostdin", "-v", "error",
        "-i", str(src),
        "-map", "0:v:0", "-c:v", "copy",
        "-bsf:v", "hevc_mp4toannexb",
        "-f", "hevc", "-",
    ]
    dovi_cmd = [
        "dovi_tool", "-m", "2", "convert", "--discard",
        "-", "-o", str(bl_rpu_path),
    ]
    return ffmpeg_cmd, dovi_cmd


def build_mkvmerge_command(
    src: Path,
    bl_rpu_path: Path,
    out_path: Path,
    r_frame_rate: str,
    title: Optional[str] = None,
    *,
    video_language: Optional[str] = None,
    video_name: Optional[str] = None,
    video_default: Optional[int] = None,
) -> list:
    """mkvmerge remux: converted video track first (with the frame rate
    pinned — raw Annex-B has no container timing), then everything except
    video (``-D``) from the source.

    The raw stream carries no track metadata, so the source video track's
    language, name and default flag are copied onto the new track. Without
    this the converted file lost its ``eng`` video language and gained a
    default flag (#894 prod test, The Matrix Revolutions, 2026-10-11).
    """
    cmd = ["mkvmerge", "-o", str(out_path)]
    if title:
        cmd += ["--title", title]
    if video_language:
        cmd += ["--language", f"0:{video_language}"]
    if video_name:
        cmd += ["--track-name", f"0:{video_name}"]
    if video_default is not None:
        cmd += ["--default-track-flag", f"0:{'1' if video_default else '0'}"]
    cmd += [
        "--default-duration", f"0:{r_frame_rate}p",
        str(bl_rpu_path),
        "-D", str(src),
    ]
    return cmd


# ────────────────────────────────────────────────────────────────
# Pipeline steps (module-level so tests can patch them individually)
# ────────────────────────────────────────────────────────────────

def _check_free_space(src: Path) -> Optional[str]:
    """Precondition: free space next to the source >= twice its size.

    During the mkvmerge step the extracted video stream (about the video's
    size) and the new MKV (about the original's size) exist side by side in
    the work dir, so the peak need is close to 2x the source. #671 checked
    for 1x, which would fail mid-conversion on a nearly full share (#894).
    Returns an error reason, or None when there is enough space.
    """
    try:
        size = src.stat().st_size
        st = os.statvfs(src.parent)
        free = st.f_bavail * st.f_frsize
    except OSError as exc:
        return f"could not determine free space: {exc}"
    need = FREE_SPACE_FACTOR * size
    if free < need:
        return (
            f"not enough free space on {src.parent} "
            f"(need {need / 1e9:.1f} GB, have {free / 1e9:.1f} GB)"
        )
    return None


def _extract_and_convert(src: Path, bl_rpu_path: Path) -> Optional[str]:
    """Run ``ffmpeg | dovi_tool`` producing ``bl_rpu_path``. Both exit
    codes are checked (pipefail semantics). Returns an error reason or
    None on success."""
    ffmpeg_cmd, dovi_cmd = build_extract_convert_commands(src, bl_rpu_path)
    ff = None
    dv = None
    try:
        ff = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        dv = subprocess.Popen(dovi_cmd, stdin=ff.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Close our copy of the pipe so ffmpeg receives SIGPIPE if
        # dovi_tool exits early.
        ff.stdout.close()
        dv_out, dv_err = dv.communicate(timeout=EXTRACT_CONVERT_TIMEOUT_SECONDS)
        ff.wait(timeout=600)
        ff_err = b""
        try:
            if ff.stderr is not None:
                ff_err = ff.stderr.read() or b""
        finally:
            if ff.stderr is not None:
                ff.stderr.close()
        if ff.returncode != 0:
            return (
                f"ffmpeg exited with code {ff.returncode}: "
                f"{(ff_err or b'').decode(errors='replace').strip()[:2000]}"
            )
        if dv.returncode != 0:
            return (
                f"dovi_tool exited with code {dv.returncode}: "
                f"{(dv_err or b'').decode(errors='replace').strip()[:2000]}"
            )
        if not bl_rpu_path.exists() or bl_rpu_path.stat().st_size == 0:
            return "dovi_tool produced no output (BL_RPU.hevc missing or empty)"
        return None
    except subprocess.TimeoutExpired:
        for proc in (dv, ff):
            if proc is not None:
                try:
                    proc.kill()
                except OSError:
                    pass
        return "extract/convert step timed out"
    except (FileNotFoundError, OSError) as exc:
        for proc in (dv, ff):
            if proc is not None:
                try:
                    proc.kill()
                except OSError:
                    pass
        return f"could not run extract/convert step: {exc}"


def _run_mkvmerge(cmd: list) -> tuple[Optional[str], Optional[str]]:
    """Run mkvmerge. Exit 0 = ok, 1 = ok-with-warnings, 2 = failure.
    Returns ``(error_reason, warnings_text)``."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=MKVMERGE_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        return "mkvmerge timed out", None
    except (FileNotFoundError, OSError) as exc:
        return f"could not run mkvmerge: {exc}", None
    if out.returncode == 0:
        return None, None
    if out.returncode == 1:
        warnings = ((out.stdout or "") + (out.stderr or "")).strip()[:4000]
        return None, warnings or "mkvmerge exited with warnings (code 1)"
    return (
        f"mkvmerge exited with code {out.returncode}: "
        f"{((out.stdout or '') + (out.stderr or '')).strip()[:2000]}",
        None,
    )


def _verify_output(src_info: dict, out_path: Path) -> Optional[str]:
    """Mandatory verification before the original is replaced/consumed.
    Returns an error reason, or None when the output checks out."""
    out_info = _probe_verification_info(out_path)
    if not out_info:
        return "could not probe remuxed output"
    if out_info.get("dv_profile") != 8:
        return (
            "remuxed output does not carry Dolby Vision profile 8 "
            f"(got {out_info.get('dv_profile')})"
        )
    src_duration = src_info.get("duration")
    out_duration = out_info.get("duration")
    if src_duration is not None and out_duration is not None:
        if abs(src_duration - out_duration) > DURATION_TOLERANCE_SECONDS:
            return (
                f"duration mismatch: source {src_duration:.2f}s vs "
                f"output {out_duration:.2f}s (> {DURATION_TOLERANCE_SECONDS}s)"
            )
    elif src_duration is not None or out_duration is not None:
        return "could not compare container durations"
    if out_info.get("audio_streams") != src_info.get("audio_streams"):
        return (
            f"audio stream count changed: source {src_info.get('audio_streams')} "
            f"vs output {out_info.get('audio_streams')}"
        )
    if out_info.get("subtitle_streams") != src_info.get("subtitle_streams"):
        return (
            f"subtitle stream count changed: source {src_info.get('subtitle_streams')} "
            f"vs output {out_info.get('subtitle_streams')}"
        )
    return None


def probe_media_info(path: Path) -> Optional[dict]:
    """One cheap ffprobe of a file's first video stream and container, for
    the library catalog (#894). Reads headers only.

    Returns ``{video_codec, width, height, dv_profile, duration}`` (values
    may be None), or None when the file could not be probed.
    """
    data = _ffprobe_json(["-show_streams", "-select_streams", "v:0", "-show_format", str(path)])
    if not data:
        return None
    streams = data.get("streams") or []
    stream = streams[0] if streams else {}
    dv_profile = None
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict) and side_data.get("side_data_type") == "DOVI configuration record":
            try:
                dv_profile = int(side_data.get("dv_profile"))
            except (TypeError, ValueError):
                dv_profile = None
            break
    duration = None
    try:
        raw = (data.get("format") or {}).get("duration")
        duration = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        duration = None

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "video_codec": stream.get("codec_name") or None,
        "width": _int(stream.get("width")),
        "height": _int(stream.get("height")),
        "dv_profile": dv_profile,
        "duration": duration,
    }


# ────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────

def work_dir_for(src, scratch_dir=None) -> Path:
    """Where a conversion of ``src`` writes its intermediate files: next to
    the source by default, or under ``scratch_dir`` (local scratch, #896)."""
    src = Path(src)
    if scratch_dir:
        return Path(scratch_dir) / f"{_WORK_DIR_PREFIX}{src.stem}"
    return src.parent / f"{_WORK_DIR_PREFIX}{src.stem}"


def push_temp_path(src) -> Path:
    """Hidden temp name next to ``src`` that a scratch result is copied to
    before the atomic rename (hidden, so a library scan never catalogs it)."""
    src = Path(src)
    return src.parent / f"{_WORK_DIR_PREFIX}push-{src.name}"


def _copy_durably(src: Path, dst: Path) -> None:
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        shutil.copyfileobj(fin, fout, length=16 * 1024 * 1024)
        fout.flush()
        os.fsync(fout.fileno())


def remux_dv7_to_dv81(
    src,
    *,
    progress_cb: Optional[Callable[[float], None]] = None,
    scratch_dir=None,
    check_space: bool = True,
) -> DvRemuxResult:
    """Convert a DV profile 7 MKV to profile 8.1 in place, replacing ``src``
    with the converted file only after verification passes.

    ``scratch_dir`` (#896): write the intermediate stream and the new MKV
    there instead of next to the source (for a source on a network share),
    then copy the verified result next to the original under a hidden temp
    name, check its size, and rename it over the original. ``check_space``
    is False when the caller holds a space reservation (#895).

    Never raises for operational failures: returns a ``DvRemuxResult`` with
    status ``converted``/``skipped``/``failed``. On anything other than
    ``converted`` the original file is untouched and temp files are removed.
    """
    src = Path(src)

    def _progress(fraction: float) -> None:
        if progress_cb is not None:
            try:
                progress_cb(fraction)
            except Exception:  # noqa: BLE001 - progress must never break the remux
                pass

    if src.suffix.lower() != ".mkv" or not src.exists():
        return DvRemuxResult("skipped", "not an existing .mkv file")

    profile, r_frame_rate = probe_dv_info(src)
    if profile != 7:
        return DvRemuxResult("skipped", f"not Dolby Vision profile 7 (dv_profile={profile})")
    if not r_frame_rate:
        return DvRemuxResult("failed", "could not determine source frame rate (r_frame_rate)")
    _progress(0.05)

    if check_space and not scratch_dir:
        space_error = _check_free_space(src)
        if space_error:
            return DvRemuxResult("skipped", space_error)

    src_info = _probe_verification_info(src)
    if not src_info:
        return DvRemuxResult("failed", "could not probe source file")

    work_dir = work_dir_for(src, scratch_dir)
    bl_rpu_path = work_dir / "BL_RPU.hevc"
    out_path = work_dir / "out.mkv"
    push_path = push_temp_path(src)
    try:
        work_dir.mkdir(parents=True, exist_ok=True)

        error = _extract_and_convert(src, bl_rpu_path)
        if error:
            return DvRemuxResult("failed", error)
        _progress(0.6)

        mkv_cmd = build_mkvmerge_command(
            src, bl_rpu_path, out_path, r_frame_rate, title=src_info.get("title"),
            video_language=src_info.get("video_language"),
            video_name=src_info.get("video_name"),
            video_default=src_info.get("video_default"),
        )
        error, warnings = _run_mkvmerge(mkv_cmd)
        if warnings:
            log.warning("dv_remux: mkvmerge warnings for %s: %s", src, warnings)
        # Drop the intermediate Annex-B stream as soon as mkvmerge is done.
        try:
            bl_rpu_path.unlink(missing_ok=True)
        except OSError:
            pass
        if error:
            return DvRemuxResult("failed", error)
        _progress(0.85)

        error = _verify_output(src_info, out_path)
        if error:
            return DvRemuxResult("failed", f"verification failed: {error}")
        _progress(0.95)

        if scratch_dir:
            # Different filesystem: copy next to the original first, so the
            # final step is still an atomic same-directory rename.
            try:
                _copy_durably(out_path, push_path)
                if push_path.stat().st_size != out_path.stat().st_size:
                    return DvRemuxResult("failed", "copy back to the library was incomplete")
                os.replace(push_path, src)
            except OSError as exc:
                return DvRemuxResult("failed", f"copying the result back failed: {exc}")
        else:
            try:
                os.replace(out_path, src)
            except OSError as exc:
                return DvRemuxResult("failed", f"replacing original failed: {exc}")

        _progress(1.0)
        log.info("dv_remux: converted %s to Dolby Vision profile 8.1", src)
        return DvRemuxResult("converted")
    except Exception as exc:  # noqa: BLE001 - a remux failure must never fail the caller
        log.warning("dv_remux: unexpected error remuxing %s: %s", src, exc, exc_info=True)
        return DvRemuxResult("failed", f"unexpected error: {exc}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        try:
            push_path.unlink(missing_ok=True)
        except OSError:
            pass
