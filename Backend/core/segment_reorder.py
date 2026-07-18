"""
Path A — segment-reorder workflow primitives.

The end-to-end flow on a Midway-class obfuscated disc:

  1. Phase 1 scan persists `obfuscation_flag` and `segment_map` per title.
  2. detect_duplicate_segment_groups() groups titles by sorted segment-map
     and surfaces obfuscation territory.
  3. User clicks Start Rip, projected size > threshold + groups detected,
     API returns 409 needs_user_choice and the frontend shows the modal.
  4. User picks "Find canonical" — one exploratory rip of a candidate
     playlist runs through Phase 1's selective-rip path with rip_set=[N].
  5. generate_previews() chops the ripped MKV into per-PlayItem previews
     using fast+accurate ffmpeg seek (boundaries from MPLS PlayItem
     durations parsed by core.bd_mpls — keyframe-snap stream-copy split
     leaks ~1s of the prior PlayItem and was rejected during the spike).
  6. User drags the previews into story order.
  7. match_user_order_to_playlists() finds the playlist whose segment_map
     matches the user's submitted order. Exact match wins; a sorted-set
     match (same segments, different order) is surfaced for the rare
     "user got the order wrong" case.
  8. The matched playlist's title index becomes the canonical for
     build_rip_title_set() and the final rip runs.

This module is the backend half. The frontend pieces (modal, drag-drop UI)
live under Frontend/src/app/pages/ripper/components/segment-reorder/.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from core.bd_mpls import parse_mpls_file, parse_playitem_durations

logger = logging.getLogger(__name__)


# ── Duplicate-group detection ─────────────────────────────────────────────────


@dataclass(frozen=True)
class DuplicateGroup:
    """A set of titles sharing the same sorted segment_map.

    Two titles are members iff their segment_map (after split-and-sort)
    is identical. Ordering within the group is preserved as the disc
    enumerated them — useful for UI presentation.
    """

    sorted_segment_key: str  # e.g. "501,502,503,504,505,506,507,508,509,510"
    title_indexes: tuple[int, ...]

    @property
    def size(self) -> int:
        return len(self.title_indexes)


def parse_segment_map_tokens(segment_map: str | None) -> list[str]:
    """Tokenize a MakeMKV segment_map into clip-id strings, in order.

    Most segment_maps are plain comma-separated (e.g. "504,510,501"),
    but MakeMKV also emits paren-wrapped forms for some playlists
    (e.g. "(502,501,503,500,506,507,505)") — the parens denote a
    sub-playlist grouping but the clip IDs inside are still the
    segments that play. Strip the wrapping characters so all readers
    see the same shape.

    Empty / None input → empty list.
    """
    if not segment_map:
        return []
    s = str(segment_map).strip()
    if not s:
        return []
    # Strip surrounding parens (or brackets, future-proofing) — only the
    # outermost pair; inner punctuation if any would indicate a different
    # encoding that we'd have to learn separately.
    while len(s) >= 2 and s[0] in "([{" and s[-1] in ")]}":
        s = s[1:-1].strip()
        if not s:
            return []
    return [p.strip() for p in s.split(",") if p.strip()]


def _segment_set_key(segment_map: str | None) -> str | None:
    """Produce the canonical sorted-segment-set key from a MakeMKV segment_map.

    Tokenized via `parse_segment_map_tokens` so paren-wrapped values are
    handled the same as plain comma-separated ones. De-duplicates, sorts
    lexicographically (stable across reps), and rejoins. Returns None
    when fewer than 2 tokens are present — singleton clips have no
    multi-segment signature.
    """
    parts = parse_segment_map_tokens(segment_map)
    if len(parts) < 2:
        return None
    return ",".join(sorted(set(parts)))


def detect_duplicate_segment_groups(
    titles: dict[int, dict],
    *,
    min_group_size: int = 2,
) -> list[DuplicateGroup]:
    """Return all duplicate-segment-map groups on a disc.

    Args:
        titles: mapping of MakeMKV title index → metadata dict. Each dict
            must have `segment_map` (comma-separated string) for grouping.
            Titles without a segment_map are skipped.
        min_group_size: minimum number of members for a group to be emitted.
            Defaults to 2 (only true duplicates surface). On Midway: one
            group of 201 members + the +60s outlier (a singleton, dropped).

    Returns:
        A list of DuplicateGroup, ordered by descending group size. Stable.
    """
    by_key: dict[str, list[int]] = {}
    for idx, t in titles.items():
        if not isinstance(t, dict):
            continue
        key = _segment_set_key(t.get("segment_map"))
        if key is None:
            continue
        by_key.setdefault(key, []).append(idx)

    groups = [
        DuplicateGroup(sorted_segment_key=k, title_indexes=tuple(sorted(v)))
        for k, v in by_key.items()
        if len(v) >= min_group_size
    ]
    groups.sort(key=lambda g: (-g.size, g.sorted_segment_key))
    return groups


def has_obfuscation_signature(
    titles: dict[int, dict],
    *,
    min_group_size: int = 2,
) -> bool:
    """True iff the disc has at least one duplicate-segment-map group.

    This is the "Midway-class" detector that gates the threshold modal:
    only on discs where a duplicate group exists is the segment-reorder
    workflow useful. A 250 GB UHD with no duplicates rips via the
    default path (no modal).
    """
    return bool(detect_duplicate_segment_groups(titles, min_group_size=min_group_size))


# ── User-order ↔ playlist matching ────────────────────────────────────────────


@dataclass(frozen=True)
class SupersetCandidate:
    """A playlist whose segment_map contains the user's ordered segments
    as an ordered subsequence — plus extra segments interleaved.

    Surfaced when no exact or sorted-set match is found. Advanced BD-J
    obfuscation has been observed where the real mpls plays the user's
    7 ordered clips PLUS 1–N noise clips between them; sorted-set is
    too strict (sets differ), exact too strict (order + segments must
    match). This tier catches the "user's order is preserved within a
    longer playlist" case so the UI can ask the user to verify.

    `extras_clips` and `extras_positions` are parallel lists describing
    the clips in the mpls that the user did NOT order. Positions are
    indices into the mpls's segment_map (absolute), not relative to the
    user's clips.
    """

    title_index: int
    source_file: str | None
    extras_clips: tuple[str, ...]
    extras_positions: tuple[int, ...]
    mpls_total_size_b: int | None
    sorted_set_key: str


@dataclass(frozen=True)
class MatchResult:
    """Result of matching a user-supplied segment ordering to disc playlists.

    Three tiers, ordered by signal strength:

    1. `exact` — `segment_map` equals the user order verbatim. The win
       condition; one exact match auto-confirms.
    2. `sorted_set` — same segments, different order. Surfaced when there
       are zero exact matches (user got the order wrong) or when exact is
       ambiguous.
    3. `subsequence_supersets` — user's order is preserved within an
       mpls that has additional segments interleaved. Surfaced when
       exact AND sorted_set are both empty — the advanced-obfuscation
       case where the real mpls has noise injected.
    """

    exact: list[int] = field(default_factory=list)
    sorted_set: list[int] = field(default_factory=list)
    subsequence_supersets: list[SupersetCandidate] = field(default_factory=list)

    @property
    def has_unique_exact(self) -> bool:
        return len(self.exact) == 1


def is_ordered_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """True iff `needle` appears within `haystack` in the same relative
    order, allowing other elements between needle elements.

    Linear two-pointer walk; O(len(haystack)).

    Examples:
        is_ordered_subsequence(["a","b","c"], ["a","x","b","y","c"]) -> True
        is_ordered_subsequence(["a","b","c"], ["b","a","c"]) -> False
        is_ordered_subsequence([], anything) -> True
    """
    if not needle:
        return True
    i = 0
    for h in haystack:
        if h == needle[i]:
            i += 1
            if i == len(needle):
                return True
    return False


def match_user_order_to_playlists(
    titles: dict[int, dict],
    user_order: list[str],
    *,
    max_extras_factor: float = 2.0,
    disc_flags: dict[str, str] | None = None,
) -> MatchResult:
    """Find titles whose segment_map matches the user's segment ordering.

    Args:
        titles: mapping of title index → metadata dict (must have segment_map).
        user_order: comma-separated segment IDs in the order the user
            arranged the previews. e.g. ["504", "510", "501", ...].
        max_extras_factor: cap subsequence-superset matches at mpls with at
            most `max_extras_factor * len(user_order)` extra segments.
            Prevents noise matches against unrelated long playlists.
            Default 2.0 — a 7-clip user order matches mpls up to 21
            segments long.
        disc_flags: optional `{clip_id: 'potentially' | 'definitely'}` from
            `discs.segment_obfuscation_flags`. Filters the subsequence-
            superset tier only:
            - `definitely`-flagged clip → any mpls containing it is
              excluded from supersets (the user has confirmed that clip
              is noise; mpls that play it can't be the real movie).
            - `potentially`-flagged clip → mpls that OMIT the clip
              rank-boost (sort earlier) within their cluster; mpls that
              include it are NOT excluded — just deprioritised.
            Exact and sorted_set tiers are unaffected; if the user's
            order matches verbatim, we trust that signal over the flags.

    Returns:
        A MatchResult with three tiers. Tiers 1+2 are stable ascending
        title-index lists; tier 3 is sorted by potentially-flag presence
        (omitters first), then extras count asc, then title_index.
    """
    if not user_order:
        return MatchResult()
    cleaned = [s.strip() for s in user_order if s and s.strip()]
    if not cleaned:
        return MatchResult()

    user_csv = ",".join(cleaned)
    user_sorted = sorted(set(cleaned))
    user_n = len(cleaned)
    max_extras = int(user_n * max_extras_factor)

    flags = disc_flags or {}
    definitely_clips = {clip for clip, f in flags.items() if f == "definitely"}
    potentially_clips = {clip for clip, f in flags.items() if f == "potentially"}

    exact: list[int] = []
    sorted_set: list[int] = []
    supersets: list[SupersetCandidate] = []
    for idx, t in titles.items():
        if not isinstance(t, dict):
            continue
        sm = t.get("segment_map")
        if not sm:
            continue
        # Tokenize via the shared helper so paren-wrapped MakeMKV
        # segment_maps ("(502,501,...)") match the same way as plain
        # comma-separated ones.
        sm_parts = parse_segment_map_tokens(sm)
        if not sm_parts:
            continue
        sm_csv = ",".join(sm_parts)
        if sm_csv == user_csv:
            exact.append(idx)
        if sorted(set(sm_parts)) == user_sorted:
            sorted_set.append(idx)
            # Equal sets means no extras possible — never a superset.
            continue
        extras_count = len(sm_parts) - user_n
        if extras_count <= 0 or extras_count > max_extras:
            continue
        if not is_ordered_subsequence(cleaned, sm_parts):
            continue
        sm_set = set(sm_parts)
        # `definitely` filter: any mpls containing a definitely-flagged
        # clip is excluded outright.
        if definitely_clips & sm_set:
            continue
        # Walk the mpls once to mark which positions were consumed by the
        # subsequence match; the rest are extras.
        extras_positions: list[int] = []
        extras_clips: list[str] = []
        i = 0
        for pos, h in enumerate(sm_parts):
            if i < user_n and h == cleaned[i]:
                i += 1
            else:
                extras_positions.append(pos)
                extras_clips.append(h)
        supersets.append(
            SupersetCandidate(
                title_index=idx,
                source_file=t.get("source_file"),
                extras_clips=tuple(extras_clips),
                extras_positions=tuple(extras_positions),
                mpls_total_size_b=t.get("size"),
                sorted_set_key=_segment_set_key(sm) or "",
            )
        )

    def _superset_sort_key(c: SupersetCandidate) -> tuple:
        # `potentially` rank-boost: mpls omitting all potentially-flagged
        # clips sort first within the same extras_count tier. 0 = omits
        # all, 1+ = contains one or more.
        contained_potentially = len(potentially_clips & set(c.extras_clips))
        return (contained_potentially, len(c.extras_clips), c.title_index)

    return MatchResult(
        exact=sorted(exact),
        sorted_set=sorted(sorted_set),
        subsequence_supersets=sorted(supersets, key=_superset_sort_key),
    )


RIP_THE_REST_HARD_CAP_BYTES = 200 * (1024 ** 3)
"""Hard cap on the remaining-playlist-size that allows the "rip the rest"
CTA, regardless of available disk. Set at 200 GB to keep the iteration
loop's final escape hatch from offering a multi-hundred-GB rip even on
machines with abundant disk. Promote to a setting if per-deployment
control is needed."""


RIP_THE_REST_DISK_HEADROOM = 0.9
"""Fraction of free disk we're willing to reserve for "rip the rest".
Keeps the operation from filling the disk to the brim and stranding
the post-rip workflow (mux, transfer staging, etc.)."""


def rip_the_rest_threshold_bytes(free_disk_bytes: int | None) -> int:
    """Compute the remaining-size ceiling at which "rip the rest" is offered.

    The lesser of the hard cap and a fraction of available disk; clamped
    at 0 if disk probe failed. Used by the GET remaining-playlist-size
    endpoint to gate the frontend CTA + by the POST rip-the-rest endpoint
    as a pre-flight safety check.
    """
    if free_disk_bytes is None or free_disk_bytes <= 0:
        return 0
    return min(
        RIP_THE_REST_HARD_CAP_BYTES,
        int(free_disk_bytes * RIP_THE_REST_DISK_HEADROOM),
    )


def cluster_supersets_by_sorted_set(
    candidates: list[SupersetCandidate],
) -> list[list[SupersetCandidate]]:
    """Group SupersetCandidates by their sorted-segment-set key, rank
    clusters by member count descending.

    Within each cluster, candidates are ordered by extras count ascending
    then by mpls size descending (smallest-extras-largest-mpls first is
    the best "looks like the real movie" candidate).

    Between clusters: most members first (largest obfuscation pattern),
    ties broken by smallest sum-of-extras across the cluster, then by
    sorted_set_key (deterministic).

    Same heuristic as the initial exploratory pick from
    `detect_duplicate_segment_groups`, applied to the superset-matched
    subset — the cluster with the most members is most likely to
    contain the real movie.
    """
    by_key: dict[str, list[SupersetCandidate]] = {}
    for c in candidates:
        by_key.setdefault(c.sorted_set_key, []).append(c)

    clusters = list(by_key.values())
    for cluster in clusters:
        cluster.sort(
            key=lambda c: (
                len(c.extras_clips),
                -(c.mpls_total_size_b or 0),
                c.title_index,
            )
        )
    clusters.sort(
        key=lambda cl: (
            -len(cl),
            sum(len(c.extras_clips) for c in cl),
            cl[0].sorted_set_key,
        )
    )
    return clusters


# ── Preview generation (ffmpeg, fast+accurate seek) ───────────────────────────


# Keep these in sync with the spike's make_previews_from_rip.py — they're
# the empirically-validated tuning. Long PlayItems get a 30s head + 2s
# breaker + 30s tail; short ones get the full clip transcoded.
DEFAULT_STITCH_THRESHOLD_S = 60.0
DEFAULT_HEAD_S = 30.0
DEFAULT_TAIL_S = 30.0
DEFAULT_BREAKER_S = 2.0
# `-ss <coarse> -i input -ss <fine>` is faster than pure accurate-seek and
# more accurate than pure keyframe-seek. 5s back from the target is enough
# to land before the prior IDR on every Blu-ray video stream we've tested.
DEFAULT_COARSE_BACK_S = 5.0
DEFAULT_W, DEFAULT_H, DEFAULT_FPS = 1280, 720, 24


@dataclass
class PreviewSpec:
    """One entry in the previews manifest written alongside the .mp4 files.

    `clip_name` is the MPLS PlayItem clip identifier (e.g. "00504") — the
    matching half of segment-reorder compares user-submitted orderings of
    these clip names against `segment_map` strings on disc titles. Without
    it the frontend would have to send back arbitrary positions and we'd
    lose the round-trip.
    """

    index: int
    path: str  # relative to the previews dir, e.g. "seg_00.mp4"
    cum_start_s: float
    mode: str  # "full" | "stitch"
    src_dur_s: float
    clip_name: str | None = None
    head_s: float | None = None
    tail_s: float | None = None

    def to_dict(self) -> dict:
        d = {
            "index": self.index,
            "path": self.path,
            "cum_start_s": self.cum_start_s,
            "mode": self.mode,
            "src_dur_s": self.src_dur_s,
        }
        if self.clip_name is not None:
            d["clip_name"] = self.clip_name
        if self.mode == "stitch":
            d["head_s"] = self.head_s
            d["tail_s"] = self.tail_s
        return d


def _ffmpeg_video_args(width: int, height: int, fps: int) -> list[str]:
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", "1000k",
        "-pix_fmt", "yuv420p", "-r", str(fps), "-g", str(fps * 2),
    ]


def _ffmpeg_audio_args() -> list[str]:
    return ["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000"]


def _ffmpeg_scale_filter(width: int, height: int) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    )


def _encode_window(
    rip: Path,
    cum_start_s: float,
    dur_s: float,
    out: Path,
    *,
    width: int,
    height: int,
    fps: int,
    coarse_back_s: float,
    runner=subprocess.run,
) -> None:
    """Encode `dur_s` seconds starting exactly at `cum_start_s` in the rip.

    Uses the fast+accurate seek pattern: coarse keyframe-seek before -i,
    then fine accurate-seek after -i. Faster than pure accurate-seek
    (which decodes from t=0) and more accurate than pure keyframe-seek
    (which can land ~1s off, which is the leakage the spike rejected).
    """
    coarse = max(0.0, cum_start_s - coarse_back_s)
    fine = cum_start_s - coarse
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{coarse:.3f}",
        "-i", str(rip),
        "-ss", f"{fine:.3f}",
        "-t", f"{dur_s:.3f}",
        "-map", "0:v:0", "-map", "0:a:0",
        # bin_data chapter track from the joined rip would inflate mp4
        # format duration to the full rip — drop it.
        "-map_chapters", "-1",
        "-vf", _ffmpeg_scale_filter(width, height),
        *_ffmpeg_video_args(width, height, fps),
        *_ffmpeg_audio_args(),
        "-movflags", "+faststart",
        str(out),
    ]
    runner(cmd, check=True)


def _encode_breaker(
    out: Path,
    *,
    width: int,
    height: int,
    fps: int,
    breaker_s: float,
    runner=subprocess.run,
    label: str = "->  END  ->  START  ->",
) -> None:
    """Build the 2-second black breaker that sits between head and tail
    of a stitched long-PlayItem preview. Helps the user mentally
    separate two fragments of the same segment when they're matching
    boundaries to neighbors during ordering.
    """
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i",
        f"color=c=black:s={width}x{height}:r={fps}:d={breaker_s}",
        "-f", "lavfi", "-i",
        f"anullsrc=channel_layout=stereo:sample_rate=48000:duration={breaker_s}",
        "-vf",
        f"drawtext=text='{label}':"
        "fontcolor=white:fontsize=56:"
        "x=(w-text_w)/2:y=(h-text_h)/2",
        *_ffmpeg_video_args(width, height, fps),
        *_ffmpeg_audio_args(),
        "-shortest", "-movflags", "+faststart",
        str(out),
    ]
    runner(cmd, check=True)


def _concat_parts(parts: list[Path], out: Path, runner=subprocess.run) -> None:
    """Stream-copy concat the head/breaker/tail trio into the final preview.
    Safe to stream-copy because all three were encoded with identical
    codec params (see _ffmpeg_video_args / _ffmpeg_audio_args).
    """
    list_path = out.parent / f".{out.stem}.concat.txt"
    list_path.write_text("\n".join(f"file '{p.name}'" for p in parts) + "\n")
    try:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy",
            "-movflags", "+faststart",
            str(out),
        ]
        runner(cmd, check=True)
    finally:
        list_path.unlink(missing_ok=True)


def generate_previews(
    rip: Path,
    previews_dir: Path,
    playitem_durations_s: Iterable[float],
    *,
    clip_names: Iterable[str] | None = None,
    stitch_threshold_s: float = DEFAULT_STITCH_THRESHOLD_S,
    head_s: float = DEFAULT_HEAD_S,
    tail_s: float = DEFAULT_TAIL_S,
    breaker_s: float = DEFAULT_BREAKER_S,
    coarse_back_s: float = DEFAULT_COARSE_BACK_S,
    width: int = DEFAULT_W,
    height: int = DEFAULT_H,
    fps: int = DEFAULT_FPS,
    runner=subprocess.run,
) -> list[PreviewSpec]:
    """Generate per-PlayItem previews from a joined rip and write a manifest.

    Long PlayItems (> stitch_threshold_s) become head + breaker + tail;
    short PlayItems are transcoded in full. `runner` is injected for tests.

    Args:
        rip: path to the canonical-or-exploratory ripped .mkv.
        previews_dir: output directory; created if missing.
        playitem_durations_s: per-PlayItem durations in seconds, in
            playlist order. Sourced from core.bd_mpls.parse_playitem_durations.
        runner: subprocess.run-shaped callable (for unit tests). The default
            uses real ffmpeg.

    Returns:
        The list of PreviewSpec rows written to manifest.json. Caller can
        emit `segment_reorder_ready` once this returns and all .mp4s are
        present on disk.
    """
    previews_dir.mkdir(parents=True, exist_ok=True)
    durations = list(playitem_durations_s)
    clip_list = list(clip_names) if clip_names is not None else []

    cum = 0.0
    manifest: list[PreviewSpec] = []
    for i, dur in enumerate(durations):
        out = previews_dir / f"seg_{i:02d}.mp4"
        clip = clip_list[i] if i < len(clip_list) else None
        if dur <= stitch_threshold_s:
            _encode_window(
                rip, cum, dur, out,
                width=width, height=height, fps=fps,
                coarse_back_s=coarse_back_s, runner=runner,
            )
            spec = PreviewSpec(
                index=i, path=out.name, cum_start_s=cum,
                mode="full", src_dur_s=dur, clip_name=clip,
            )
        else:
            head = previews_dir / f".{out.stem}.head.mp4"
            tail = previews_dir / f".{out.stem}.tail.mp4"
            breaker = previews_dir / f".{out.stem}.breaker.mp4"
            _encode_window(
                rip, cum, head_s, head,
                width=width, height=height, fps=fps,
                coarse_back_s=coarse_back_s, runner=runner,
            )
            _encode_breaker(
                breaker, width=width, height=height, fps=fps,
                breaker_s=breaker_s, runner=runner,
            )
            _encode_window(
                rip, cum + dur - tail_s, tail_s, tail,
                width=width, height=height, fps=fps,
                coarse_back_s=coarse_back_s, runner=runner,
            )
            _concat_parts([head, breaker, tail], out, runner=runner)
            for tmp in (head, tail, breaker):
                tmp.unlink(missing_ok=True)
            spec = PreviewSpec(
                index=i, path=out.name, cum_start_s=cum,
                mode="stitch", src_dur_s=dur,
                head_s=head_s, tail_s=tail_s, clip_name=clip,
            )
        manifest.append(spec)
        cum += dur

    (previews_dir / "manifest.json").write_text(
        json.dumps([s.to_dict() for s in manifest], indent=2)
    )
    return manifest


# ── Mount/parse helper for the post-exploratory-rip hook ─────────────────────


@contextlib.contextmanager
def _mounted_disc(device_path: str):
    """Yield the temp mount point of a read-only mount of `device_path`.

    Mount/unmount happens via `mount`/`umount` subprocess calls. The
    mkv-auto container runs as root so this works without sudo. The
    mount point is created and removed within the contextmanager;
    callers can read the BDMV tree freely while inside the with.

    On systems where the disc is already mounted (e.g. a host with
    udisks2 auto-mount), a busy-mount EBUSY is treated as success
    after we resolve the existing mount via /proc/mounts — we'd
    rather read the existing mount than fight it.
    """
    mount_dir = tempfile.mkdtemp(prefix="mkvauto-bd-")
    mounted_here = False
    try:
        result = subprocess.run(
            ["mount", "-o", "ro", device_path, mount_dir],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # Already mounted somewhere? Resolve and use that path.
            try:
                with open("/proc/mounts") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 2 and parts[0] == device_path:
                            existing = parts[1]
                            os.rmdir(mount_dir)
                            yield existing
                            return
            except OSError:
                pass
            raise RuntimeError(
                f"Failed to mount {device_path} at {mount_dir}: "
                f"rc={result.returncode} stderr={result.stderr.strip()}"
            )
        mounted_here = True
        yield mount_dir
    finally:
        if mounted_here:
            subprocess.run(["umount", mount_dir], capture_output=True)
        try:
            os.rmdir(mount_dir)
        except OSError:
            pass


def _read_mpls_clip_names(disc_mount_path: str | Path, mpls_filename: str) -> list[str]:
    """Best-effort: return the ordered clip_name list for an MPLS file.
    Empty list when the parse fails — caller handles the fallback."""
    candidate = Path(disc_mount_path) / "BDMV" / "PLAYLIST" / mpls_filename
    if not candidate.is_file():
        logger.debug("MPLS file not found at %s", candidate)
        return []
    try:
        return [pi.clip_name for pi in parse_mpls_file(candidate).play_items]
    except (ValueError, OSError) as e:
        logger.warning("Failed to parse MPLS %s for clip names: %s", candidate, e)
        return []


def run_exploratory_postprocess(
    rip_path: Path,
    previews_dir: Path,
    device_path: str,
    mpls_filename: str,
) -> list[PreviewSpec]:
    """End-to-end post-exploratory-rip orchestration.

    Mounts the disc read-only, parses the MPLS playlist's PlayItem durations
    + clip names, generates per-PlayItem previews from the joined rip via
    fast+accurate ffmpeg seek, and writes the manifest. Returns the list
    of PreviewSpec rows so the caller can persist them onto
    `job.segment_reorder_state.previews_manifest`.

    Args:
        rip_path: path to the joined .mkv produced by the exploratory rip.
        previews_dir: directory to write seg_*.mp4 + manifest.json into;
            created if missing.
        device_path: e.g. `/dev/sr1`; mounted r/o for the duration of
            MPLS parsing.
        mpls_filename: the .mpls file inside BDMV/PLAYLIST that the
            exploratory rip targeted (e.g. "00539.mpls").

    Returns:
        The PreviewSpec list. Empty when the MPLS couldn't be parsed
        (caller surfaces an error to the user; the exploratory rip
        artifacts are kept on disk for fallback to manual selection).
    """
    with _mounted_disc(device_path) as mount_path:
        durations = parse_playitem_durations(mount_path, mpls_filename) or []
        clip_names = _read_mpls_clip_names(mount_path, mpls_filename)
    if not durations:
        logger.error(
            "MPLS parse returned no durations for %s; cannot generate previews",
            mpls_filename,
        )
        return []
    return generate_previews(
        rip_path,
        previews_dir,
        durations,
        clip_names=clip_names,
    )
