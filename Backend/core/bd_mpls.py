"""
Blu-ray .mpls (MPLS, MovieObject PlayList) parser.

PlayItem boundaries are not detectable from a joined .mkv after rip — Cue density
and SPS-resend tests both proved unable to identify them. The only reliable source
is the disc's MPLS files at scan time. This parser extracts PlayItem durations
from a playlist file so we can persist them on `disc_titles.playitem_durations_s`
and use them later for segment-reorder previews and matching.

Format reference:
- 32-bit values are big-endian.
- Times are 45 kHz units (1 unit = 1/45000 s).
- https://github.com/lerks/BluRay/wiki/PlayItem
"""
from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)


class PlayItem(NamedTuple):
    clip_name: str           # 5-char clip identifier, e.g. "00504"
    codec: str               # 4-char clip codec id, e.g. "M2TS"
    in_time_45khz: int       # raw 45 kHz units
    out_time_45khz: int
    in_s: float              # seconds
    out_s: float
    duration_s: float        # out_s - in_s


class MplsPlaylist(NamedTuple):
    version: str             # 4-char version, e.g. "0200"
    play_items: list[PlayItem]

    @property
    def total_duration_s(self) -> float:
        return sum(pi.duration_s for pi in self.play_items)

    @property
    def durations_s(self) -> list[float]:
        return [pi.duration_s for pi in self.play_items]


def _read_u16(buf: bytes, off: int) -> tuple[int, int]:
    return struct.unpack(">H", buf[off:off + 2])[0], off + 2


def _read_u32(buf: bytes, off: int) -> tuple[int, int]:
    return struct.unpack(">I", buf[off:off + 4])[0], off + 4


def parse_mpls_bytes(data: bytes) -> MplsPlaylist:
    """Parse the bytes of a .mpls file. Raises ValueError on malformed input."""
    if len(data) < 16 or data[:4] != b"MPLS":
        raise ValueError(f"not an MPLS file (magic={data[:4]!r})")

    try:
        version = data[4:8].decode("ascii")
    except UnicodeDecodeError as e:
        raise ValueError(f"non-ascii MPLS version field: {e}") from e

    playlist_off, _ = _read_u32(data, 8)

    # PlayList section
    off = playlist_off
    if off + 6 > len(data):
        raise ValueError("MPLS playlist offset out of range")
    _section_len, off = _read_u32(data, off)  # sanity-only
    off += 2  # reserved
    pi_count, off = _read_u16(data, off)
    _sp_count, off = _read_u16(data, off)

    play_items: list[PlayItem] = []
    for i in range(pi_count):
        if off + 2 > len(data):
            raise ValueError(f"MPLS truncated at PlayItem {i}/{pi_count}")
        pi_len, off = _read_u16(data, off)
        pi_start = off
        if pi_start + pi_len > len(data) or pi_len < 18:
            raise ValueError(f"MPLS PlayItem {i} length {pi_len} out of range")

        try:
            clip_name = data[off:off + 5].decode("ascii")
            codec = data[off + 5:off + 9].decode("ascii")
        except UnicodeDecodeError as e:
            raise ValueError(f"non-ascii clip metadata in PlayItem {i}: {e}") from e

        # PlayItem layout (offsets within the PlayItem body):
        #   0..4   clip_name (ascii)
        #   5..8   codec (ascii)
        #   9..10  flags (2 bytes)
        #   11     ref_to_stc_id (1 byte)
        #   12..15 in_time (u32, 45 kHz)
        #   16..19 out_time (u32, 45 kHz)
        in_time, _ = _read_u32(data, off + 12)
        out_time, _ = _read_u32(data, off + 16)

        play_items.append(PlayItem(
            clip_name=clip_name,
            codec=codec,
            in_time_45khz=in_time,
            out_time_45khz=out_time,
            in_s=in_time / 45000.0,
            out_s=out_time / 45000.0,
            duration_s=(out_time - in_time) / 45000.0,
        ))
        off = pi_start + pi_len

    return MplsPlaylist(version=version, play_items=play_items)


def parse_mpls_file(path: Path) -> MplsPlaylist:
    """Parse an .mpls file at `path`. Raises ValueError on malformed input or FileNotFoundError if missing."""
    return parse_mpls_bytes(Path(path).read_bytes())


def parse_playitem_durations(disc_mount_path: str | Path, mpls_filename: str) -> list[float] | None:
    """
    Best-effort lookup of PlayItem durations for a given .mpls filename under a disc mount.

    Returns the list of per-PlayItem durations in seconds (in playlist order), or None
    if the disc isn't a Blu-ray, the file is missing, or the parse fails. Callers
    should treat None as "no per-segment timing available" — segment-reorder won't
    function on this title, but other rip operations are unaffected.
    """
    if not mpls_filename.lower().endswith(".mpls"):
        return None
    candidate = Path(disc_mount_path) / "BDMV" / "PLAYLIST" / mpls_filename
    if not candidate.is_file():
        logger.debug("MPLS file not found at %s", candidate)
        return None
    try:
        return parse_mpls_file(candidate).durations_s
    except (ValueError, OSError) as e:
        logger.warning("Failed to parse MPLS %s: %s", candidate, e)
        return None
