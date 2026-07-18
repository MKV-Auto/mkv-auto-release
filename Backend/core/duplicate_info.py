"""
On-the-fly duplicate title detection.

Groups titles by (disc_id, segment_map) and attaches duplicate_info to each
title payload. Used when building workflow context (disc and job).

- tags: absolute per-title metadata — primarily from MakeMKV `streams`, with
  `metadata_summary` hints overlaid (see `_tags_from_title_payload`).
- diff_tags: comparative tags — from ffprobe `metadata_scan` only (no MakeMKV
  stream merge for emission). Wire tags: audio:best, audio:more-languages,
  video:best, subs:more-languages, chapters:more (group-max; ties get the tag).

`include_stream_fallback` on `_comparative_metrics` keeps MakeMKV stream lift
for auto-primary selection when scan is unusable; diff_tags use scan-only metrics.
"""

import hashlib
import json
import re
from typing import Any


def _metadata_scan_usable(meta: dict[str, Any]) -> bool:
    """True when ffprobe-derived metadata_scan looks usable (not failed/empty)."""
    w = meta.get("warning")
    if isinstance(w, str) and w.strip():
        return False
    fmt = meta.get("format") or {}
    if isinstance(fmt, dict) and (
        fmt.get("duration") is not None or fmt.get("bit_rate") is not None or fmt.get("size") is not None
    ):
        return True
    if meta.get("video_hints"):
        return True
    sc = meta.get("stream_counts") or {}
    if isinstance(sc, dict) and (sc.get("video") or sc.get("audio")):
        return True
    return False


def _normalize_streams_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return MakeMKV stream dicts from title payload (list or JSON string)."""
    raw = payload.get("streams")
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


def _stream_row_kind(stream: dict[str, Any]) -> str:
    t = stream.get("type") or stream.get("Type") or ""
    return str(t).strip().lower()


def _parse_mkv_stream_bitrate_bps(val: Any) -> int | None:
    """Best-effort parse of MakeMKV SINFO bitrate to bits per second (container-ish)."""
    if val is None:
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        v = float(val)
        if v >= 1_000_000:
            return int(v)
        if v >= 500:
            return int(v * 1000)
        return int(v * 1_000_000)
    s = str(val).strip().lower().replace(",", ".")
    if not s:
        return None
    if "mb/s" in s or "mbps" in s:
        m = re.search(r"(\d+(?:\.\d+)?)", s)
        if m:
            return int(float(m.group(1)) * 1_000_000)
    if "kb/s" in s or "kbps" in s:
        m = re.search(r"(\d+(?:\.\d+)?)", s)
        if m:
            return int(float(m.group(1)) * 1_000)
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    num = float(m.group(1))
    if num >= 1_000_000:
        return int(num)
    if num >= 500:
        return int(num * 1000)
    return int(num * 1_000_000)


def _chapters_count_from_title_payload(payload: dict[str, Any]) -> int:
    ch = payload.get("chapters") or {}
    if isinstance(ch, dict):
        c = ch.get("count")
        if isinstance(c, int) and c >= 0:
            return c
    return 0


def _audio_score_from_channels_layout(ch: Any, layout: str, codec: str) -> int:
    best_channels = 0
    if isinstance(ch, int) and ch > 0:
        best_channels = max(best_channels, ch)
    lo = (layout or "").lower()
    if "7.1" in lo:
        best_channels = max(best_channels, 8)
    elif "5.1" in lo or "6.1" in lo:
        best_channels = max(best_channels, 6)
    elif "stereo" in lo or "2.0" in lo:
        best_channels = max(best_channels, 2)
    c_low = (codec or "").lower()
    has_lossless = any(x in c_low for x in ("truehd", "dts", "flac", "pcm"))
    if best_channels >= 8 or (has_lossless and best_channels >= 6):
        return 3
    if best_channels >= 6 or has_lossless:
        return 2
    if best_channels >= 2:
        return 1
    return 0


def _comparative_metrics_from_streams(payload: dict[str, Any]) -> dict[str, Any]:
    """Derive comparative metrics from MakeMKV streams + title chapters when ffprobe is blind."""
    out: dict[str, Any] = {
        "chapters_count": _chapters_count_from_title_payload(payload),
        "subtitle_count": 0,
        "subtitle_language_count": 0,
        "audio_score": 0,
        "audio_language_count": 0,
        "video_bitrate": None,
        "video_pixels": 0,
    }
    streams = _normalize_streams_list(payload)
    if not streams:
        return out
    n_sub = 0
    sub_langs: set[str] = set()
    audio_langs: set[str] = set()
    best_audio = 0
    best_vbr: int | None = None
    best_px = 0
    for st in streams:
        kind = _stream_row_kind(st)
        if kind in ("subtitles", "subtitle", "sub"):
            n_sub += 1
            lang = st.get("language_code") or st.get("language")
            if lang:
                sub_langs.add(str(lang).strip().lower())
        elif kind in ("audio", "aud"):
            layout = str(st.get("layout") or st.get("audio_type") or "")
            codec = str(st.get("codec_short") or st.get("codec_hint") or "")
            score = _audio_score_from_channels_layout(st.get("channels"), layout, codec)
            best_audio = max(best_audio, score)
            lang = st.get("language_code") or st.get("language")
            if lang:
                audio_langs.add(str(lang).strip().lower())
        elif kind == "video":
            br = _parse_mkv_stream_bitrate_bps(st.get("bitrate"))
            if br is not None:
                best_vbr = br if best_vbr is None else max(best_vbr, br)
            res = str(st.get("resolution") or "")
            m = re.match(r"(\d+)\s*x\s*(\d+)", res.replace(" ", ""), re.I)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                best_px = max(best_px, w * h)
    out["subtitle_count"] = n_sub
    out["subtitle_language_count"] = len(sub_langs) if sub_langs else (1 if n_sub else 0)
    out["audio_score"] = best_audio
    out["audio_language_count"] = len(audio_langs) if audio_langs else 0
    out["video_bitrate"] = best_vbr
    out["video_pixels"] = best_px
    return out


def _merge_stream_metrics_when_scan_unusable(out: dict[str, Any], payload: dict[str, Any]) -> None:
    """If ffprobe scan is unusable, lift metrics from MakeMKV streams where stronger."""
    if out.get("scan_usable"):
        return
    fb = _comparative_metrics_from_streams(payload)
    out["chapters_count"] = max(int(out.get("chapters_count") or 0), int(fb.get("chapters_count") or 0))
    out["subtitle_count"] = max(int(out.get("subtitle_count") or 0), int(fb.get("subtitle_count") or 0))
    out["subtitle_language_count"] = max(
        int(out.get("subtitle_language_count") or 0), int(fb.get("subtitle_language_count") or 0)
    )
    out["audio_score"] = max(int(out.get("audio_score") or 0), int(fb.get("audio_score") or 0))
    out["audio_language_count"] = max(
        int(out.get("audio_language_count") or 0), int(fb.get("audio_language_count") or 0)
    )
    out["video_pixels"] = max(int(out.get("video_pixels") or 0), int(fb.get("video_pixels") or 0))
    if out.get("video_bitrate") is None and fb.get("video_bitrate") is not None:
        out["video_bitrate"] = fb["video_bitrate"]


def _distinct_languages_from_summary(rows: list[Any], key: str = "language") -> int:
    langs: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        v = row.get(key)
        if v is not None and str(v).strip():
            langs.add(str(v).strip().lower())
    return len(langs)


def _comparative_metrics(payload: dict[str, Any], *, include_stream_fallback: bool = True) -> dict[str, Any]:
    """
    Extract comparable metrics from a title payload.

    When include_stream_fallback is False (for diff_tags / duplicate_info.metrics),
    only ffprobe scan fields contribute; no MakeMKV stream merge.
    """
    out: dict[str, Any] = {
        "chapters_count": 0,
        "subtitle_count": 0,
        "subtitle_language_count": 0,
        "audio_score": 0,
        "audio_language_count": 0,
        "video_bitrate": None,
        "video_pixels": 0,
        "scan_usable": False,
    }
    meta = payload.get("metadata_scan") or {}
    if not isinstance(meta, dict):
        if include_stream_fallback:
            fb = _comparative_metrics_from_streams(payload)
            out.update(fb)
        return out

    out["scan_usable"] = _metadata_scan_usable(meta)
    out["chapters_count"] = int(meta.get("chapters_count") or 0)
    sub_summary = meta.get("subtitle_summary") or []
    if isinstance(sub_summary, list):
        out["subtitle_count"] = len(sub_summary)
        out["subtitle_language_count"] = _distinct_languages_from_summary(sub_summary)
        if out["subtitle_language_count"] == 0 and out["subtitle_count"] > 0:
            out["subtitle_language_count"] = 1

    fmt = meta.get("format") or {}
    if isinstance(fmt, dict) and fmt.get("bit_rate") is not None:
        try:
            out["video_bitrate"] = int(fmt["bit_rate"])
        except (TypeError, ValueError):
            pass

    v = meta.get("video_hints") or {}
    if isinstance(v, dict):
        w = v.get("width") or v.get("Width")
        h = v.get("height") or v.get("Height")
        if isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0:
            out["video_pixels"] = int(w) * int(h)

    audio_summary = meta.get("audio_summary") or []
    if isinstance(audio_summary, list) and audio_summary:
        out["audio_language_count"] = _distinct_languages_from_summary(audio_summary)
        if out["audio_language_count"] == 0 and audio_summary:
            out["audio_language_count"] = 1
        best_channels = 0
        has_lossless = False
        for a in audio_summary:
            if not isinstance(a, dict):
                continue
            ch = a.get("channels")
            if isinstance(ch, int) and ch > 0:
                best_channels = max(best_channels, ch)
            layout = (a.get("channel_layout") or "").lower()
            if "7.1" in layout or "7" in layout:
                best_channels = max(best_channels, 8)
            elif "5.1" in layout or "6" in layout:
                best_channels = max(best_channels, 6)
            codec = (a.get("codec_name") or "").lower()
            if codec in ("truehd", "dts", "dts_hd", "flac", "pcm"):
                has_lossless = True
        if best_channels >= 8 or (has_lossless and best_channels >= 6):
            out["audio_score"] = 3
        elif best_channels >= 6 or has_lossless:
            out["audio_score"] = 2
        elif best_channels >= 2:
            out["audio_score"] = 1

    if include_stream_fallback:
        _merge_stream_metrics_when_scan_unusable(out, payload)
    return out


def _comparative_metrics_for_diff(payload: dict[str, Any]) -> dict[str, Any]:
    """Scan-only metrics for comparative diff_tags and duplicate_info.metrics."""
    return _comparative_metrics(payload, include_stream_fallback=False)


def _metrics_for_duplicate_info(m: dict[str, Any]) -> dict[str, Any]:
    """Scan-only metrics exposed on duplicate_info for frontend tooltips (aligned with diff_tags)."""
    return {
        "chapters_count": int(m.get("chapters_count") or 0),
        "subtitle_track_count": int(m.get("subtitle_count") or 0),
        "subtitle_language_count": int(m.get("subtitle_language_count") or 0),
        "audio_score": int(m.get("audio_score") or 0),
        "audio_language_count": int(m.get("audio_language_count") or 0),
        "video_bitrate": m.get("video_bitrate"),
        "video_pixels": int(m.get("video_pixels") or 0),
        "scan_usable": bool(m.get("scan_usable")),
    }


def _comparative_diff_tags(
    my_metrics: dict[str, Any],
    other_metrics_list: list[dict[str, Any]],
) -> list[str]:
    """
    Group-max comparative tags (scan-only metrics): emit when value equals group max
    and the axis is meaningful (group max > 0 or video has pixels/bitrate).
    """
    if not other_metrics_list:
        return []
    all_m: list[dict[str, Any]] = [my_metrics] + list(other_metrics_list)

    def numeric_max(key: str) -> int:
        return max(int(m.get(key) or 0) for m in all_m)

    diff: list[str] = []

    max_ch = numeric_max("chapters_count")
    if max_ch > 0 and (my_metrics.get("chapters_count") or 0) == max_ch:
        diff.append("chapters:more")

    max_sub_lang = numeric_max("subtitle_language_count")
    if max_sub_lang > 0 and (my_metrics.get("subtitle_language_count") or 0) == max_sub_lang:
        diff.append("subs:more-languages")

    max_audio = numeric_max("audio_score")
    if max_audio > 0 and (my_metrics.get("audio_score") or 0) == max_audio:
        diff.append("audio:best")

    max_audio_lang = numeric_max("audio_language_count")
    if max_audio_lang > 0 and (my_metrics.get("audio_language_count") or 0) == max_audio_lang:
        diff.append("audio:more-languages")

    px_list = [int(m.get("video_pixels") or 0) for m in all_m]
    max_px = max(px_list)
    min_px = min(px_list)
    my_px = int(my_metrics.get("video_pixels") or 0)
    my_br = my_metrics.get("video_bitrate")
    br_list = [m.get("video_bitrate") for m in all_m if m.get("video_bitrate") is not None]

    if max_px > min_px:
        if my_px == max_px:
            diff.append("video:best")
    else:
        if br_list:
            max_br = max(int(x) for x in br_list)
            if my_br is not None and int(my_br) == max_br:
                diff.append("video:best")
        elif max_px > 0:
            diff.append("video:best")

    return diff


def _normalize_tag_value(value: str) -> str:
    if not value or not isinstance(value, str):
        return ""
    return value.strip().lower().replace(" ", "-")


def _parse_resolution_pixels(res: str) -> int:
    m = re.match(r"(\d+)\s*x\s*(\d+)", str(res).replace(" ", ""), re.I)
    if m:
        return int(m.group(1)) * int(m.group(2))
    return 0


def _tags_from_streams(payload: dict[str, Any]) -> list[str]:
    """Absolute metadata tags from MakeMKV streams."""
    tags: list[str] = []
    streams = _normalize_streams_list(payload)
    if not streams:
        return tags

    best_px = 0
    has_hdr_name = False
    for st in streams:
        if _stream_row_kind(st) != "video":
            continue
        res = str(st.get("resolution") or "")
        best_px = max(best_px, _parse_resolution_pixels(res))
        name = (st.get("name") or st.get("description") or "").lower()
        if "hdr" in name or "dvhe" in name or "dovi" in name:
            has_hdr_name = True
    if best_px >= 3840 * 2160 or best_px >= 8_000_000:
        tags.append("quality:4k")
    elif best_px >= 1920 * 1080 or best_px >= 2_000_000:
        tags.append("quality:1080p")
    elif best_px >= 1280 * 720:
        tags.append("quality:720p")
    if has_hdr_name:
        tags.append("quality:hdr")

    best_audio_ch = 0
    audio_codecs: list[str] = []
    for st in streams:
        if _stream_row_kind(st) not in ("audio", "aud"):
            continue
        layout = str(st.get("layout") or st.get("audio_type") or "").lower()
        ch = st.get("channels")
        ic = int(ch) if isinstance(ch, int) and ch > 0 else 0
        if "7.1" in layout:
            ic = max(ic, 8)
        elif "5.1" in layout or "6.1" in layout:
            ic = max(ic, 6)
        elif "stereo" in layout or "2.0" in layout:
            ic = max(ic, 2)
        best_audio_ch = max(best_audio_ch, ic)
        c = st.get("codec_short") or st.get("codec_hint")
        if c:
            audio_codecs.append(str(c))
    if best_audio_ch >= 8:
        tags.append("audio:7.1")
    elif best_audio_ch >= 6:
        tags.append("audio:5.1")
    elif best_audio_ch >= 2:
        tags.append("audio:stereo")
    for c in audio_codecs:
        cl = str(c).lower()
        if cl and cl not in ("aac", "ac3", "mp3"):
            tags.append("audio:" + _normalize_tag_value(str(c)))
            break

    sub_langs: set[str] = set()
    n_sub = 0
    has_forced = False
    for st in streams:
        if _stream_row_kind(st) not in ("subtitles", "subtitle", "sub"):
            continue
        n_sub += 1
        lang = st.get("language_code") or st.get("language")
        if lang:
            sub_langs.add(str(lang).strip().lower())
        desc = str(st.get("description") or "").lower()
        if "forced" in desc:
            has_forced = True
    if has_forced:
        tags.append("subs:forced")
    if len(sub_langs) >= 2:
        tags.append("subs:multiple-languages")

    return tags


def _tags_from_metadata_scan_fallback(payload: dict[str, Any]) -> list[str]:
    """When streams are absent, derive minimal tags from metadata_scan."""
    tags: list[str] = []
    meta = payload.get("metadata_scan") or {}
    if not isinstance(meta, dict):
        return tags

    v = meta.get("video_hints") or {}
    if isinstance(v, dict):
        w = v.get("width") or v.get("Width")
        h = v.get("height") or v.get("Height")
        if w and h:
            if isinstance(w, (int, float)) and isinstance(h, (int, float)) and (w >= 3840 or h >= 2160):
                tags.append("quality:4k")
            elif isinstance(w, (int, float)) and isinstance(h, (int, float)) and (w >= 1920 or h >= 1080):
                tags.append("quality:1080p")
            elif isinstance(w, (int, float)) and isinstance(h, (int, float)) and (w >= 1280 or h >= 720):
                tags.append("quality:720p")
        pix_fmt = (v.get("pix_fmt") or "").lower()
        if "10" in pix_fmt or "10le" in pix_fmt or "10be" in pix_fmt:
            tags.append("quality:10-bit")
        ct = (v.get("color_transfer") or "").lower()
        if "pq" in ct or "smpte2084" in ct or "arib-std-b67" in ct or "hlg" in ct:
            tags.append("quality:hdr")

    audio_summary = meta.get("audio_summary") or []
    if isinstance(audio_summary, list) and audio_summary:
        best_channels = 0
        codecs: list[str] = []
        for a in audio_summary:
            if not isinstance(a, dict):
                continue
            ch = a.get("channels")
            if isinstance(ch, int) and ch > 0:
                best_channels = max(best_channels, ch)
            layout = (a.get("channel_layout") or "").lower()
            if "7.1" in layout or "7" in layout:
                best_channels = max(best_channels, 8)
            elif "5.1" in layout or "6" in layout:
                best_channels = max(best_channels, 6)
            c = a.get("codec_name")
            if c:
                codecs.append(c)
        if best_channels >= 8:
            tags.append("audio:7.1")
        elif best_channels >= 6:
            tags.append("audio:5.1")
        elif best_channels >= 2:
            tags.append("audio:stereo")
        for c in codecs:
            if c and c.lower() not in ("aac", "ac3", "mp3"):
                tags.append("audio:" + _normalize_tag_value(c))
                break

    subtitle_summary = meta.get("subtitle_summary") or []
    if isinstance(subtitle_summary, list):
        n_subs = len(subtitle_summary)
        has_forced = any(isinstance(s, dict) and s.get("forced") for s in subtitle_summary)
        if has_forced:
            tags.append("subs:forced")
        if n_subs >= 2:
            langs = {s.get("language") for s in subtitle_summary if isinstance(s, dict) and s.get("language")}
            if len(langs) >= 2:
                tags.append("subs:multiple-languages")

    return tags


def _tags_from_title_payload(payload: dict[str, Any]) -> list[str]:
    """Absolute tags: streams first, metadata_summary overlay, scan fallback if no streams."""
    seen: set[str] = set()
    ordered: list[str] = []

    def add(tag: str) -> None:
        if tag and tag not in seen:
            seen.add(tag)
            ordered.append(tag)

    stream_tags = _tags_from_streams(payload)
    for t in stream_tags:
        add(t)

    summary = payload.get("metadata_summary") or {}
    if isinstance(summary, dict):
        for hint in summary.get("quality_hints") or []:
            v = _normalize_tag_value(str(hint))
            if v:
                add("quality:" + v)
        for hint in summary.get("audio_hints") or []:
            v = _normalize_tag_value(str(hint))
            if v:
                add("audio:" + v)
        for hint in summary.get("subtitle_hints") or []:
            v = _normalize_tag_value(str(hint))
            if v:
                add("subs:" + v)

    if not stream_tags:
        for t in _tags_from_metadata_scan_fallback(payload):
            add(t)

    return ordered


def _normalize_segment_map(segment_map: Any) -> str | None:
    """Normalize a MakeMKV segment_map to a comma-separated token string.

    MakeMKV emits some playlists' segment_maps wrapped in parentheses
    (e.g. "(502,501,503)") to denote a sub-playlist grouping; functionally
    the clip IDs inside ARE the playlist's segments, so strip the
    wrapping characters so paren-wrapped and plain values normalize to
    the same key.
    """
    if segment_map is None:
        return None
    # Delegate tokenization to segment_reorder.parse_segment_map_tokens so
    # all readers share one canonical view of the input.
    from core.segment_reorder import parse_segment_map_tokens
    parts = parse_segment_map_tokens(segment_map)
    return ",".join(parts) if parts else None


def _attach_duplicate_info_for_group(
    titles_by_id: dict[str, dict[str, Any]],
    disc_id: str,
    normalized_seg: str,
    title_ids: list[str],
) -> None:
    seg_hash = hashlib.sha256(normalized_seg.encode()).hexdigest()[:12]
    group_id = f"disc:{disc_id}:{seg_hash}"
    group_size = len(title_ids)
    tags_per_tid: dict[str, list[str]] = {}
    metrics_per_tid: dict[str, dict[str, Any]] = {}
    diff_metrics_per_tid: dict[str, dict[str, Any]] = {}
    for tid in title_ids:
        payload = titles_by_id.get(tid)
        if not payload:
            continue
        tags_per_tid[tid] = _tags_from_title_payload(payload)
        metrics_per_tid[tid] = _comparative_metrics(payload, include_stream_fallback=True)
        diff_metrics_per_tid[tid] = _comparative_metrics_for_diff(payload)

    for tid in title_ids:
        payload = titles_by_id.get(tid)
        if not payload:
            continue
        same_as = [oid for oid in title_ids if oid != tid]
        my_tags = tags_per_tid.get(tid, [])
        my_dm = diff_metrics_per_tid.get(tid) or {}
        other_dm = [diff_metrics_per_tid.get(oid) or {} for oid in same_as]
        diff_tags = _comparative_diff_tags(my_dm, other_dm)
        my_full_m = metrics_per_tid.get(tid) or {}
        payload["duplicate_info"] = {
            "group_id": group_id,
            "group_size": group_size,
            "same_as": same_as,
            "tags": my_tags,
            "diff_tags": diff_tags,
            "metrics": _metrics_for_duplicate_info(my_dm),
            "confidence": "high",
        }

    if group_size > 1:
        _auto_select_primary(titles_by_id, title_ids, metrics_per_tid)


def _grouping_key(segment_map: Any) -> str | None:
    """Group titles by their sorted-segment-set so permutations collapse
    into one row in the UI left rail. Falls back to the order-preserved
    normalization for singleton/short segment_maps that `_segment_set_key`
    rejects (it returns None for < 2 tokens).

    Why sorted-set: the Midway 4K case has 175 mpls that are just
    re-orderings of the same 7 clips. They are functionally duplicates;
    the user expects one collapsed row + the siblings inside the right-
    editor's Duplicate group panel. Order-preserved keying buckets each
    permutation into its own group_id, defeating the collapse.
    """
    # Late import — segment_reorder pulls in core.bd_mpls + ffmpeg-adjacent
    # constants; deferring keeps module-import time low for callers that
    # only need the chip-side helpers from duplicate_info.
    from core.segment_reorder import _segment_set_key
    key = _segment_set_key(segment_map)
    if key is not None:
        return key
    # Singletons + paren-only / non-parseable inputs fall back to the
    # original order-preserved key so the V-for-Vendetta case (mpls + m2ts
    # share segment_map "61") still groups.
    return _normalize_segment_map(segment_map)


def attach_duplicate_info(titles_by_id: dict[str, dict[str, Any]], disc_id: str) -> None:
    """
    Attach duplicate_info to every title with a normalized segment_map (including singletons)
    so absolute tags are always available on the label step.

    Grouping uses the **sorted-segment-set** key so permutation siblings
    (e.g. Midway 4K's 175 mpls that re-order the same 7 clips) collapse
    into one row in the left rail. The right-editor's Duplicate group
    panel then surfaces the siblings inside the canonical's row.

    Subsumed m2ts (titles whose `subsumed_by_title_id` points to a wrapping
    mpls on the same disc) are folded into the wrapper's group so they
    collapse into the wrapper's left-rail row alongside any sorted-segment-
    set siblings. If the wrapper has no segment_map of its own (singleton
    mpls), a synthetic group is seeded on the wrapper's title_id so the
    m2ts still has a parent to collapse under.
    """
    if not disc_id or not titles_by_id:
        return

    groups: dict[str, list[str]] = {}
    title_to_key: dict[str, str] = {}
    for tid, payload in titles_by_id.items():
        # Per-title escape hatch: when the user has called Ungroup on a
        # title, the backend stamps `force_independent_group=True` and
        # `attach_duplicate_info` must skip it so the row renders as its
        # own left-rail entry instead of collapsing into a group.
        if payload.get("force_independent_group"):
            continue
        seg = payload.get("segment_map")
        key = _grouping_key(seg)
        if key is None:
            continue
        groups.setdefault(key, []).append(tid)
        title_to_key[tid] = key

    # Absorb subsumed m2ts into the wrapper's group. Pass after the initial
    # segment-set grouping so we know which group each wrapper lives in.
    for tid, payload in list(titles_by_id.items()):
        wrapper_tid = payload.get("subsumed_by_title_id")
        if not wrapper_tid:
            continue
        wrapper_tid = str(wrapper_tid)
        if wrapper_tid == tid:
            continue
        wrapper_key = title_to_key.get(wrapper_tid)
        if wrapper_key is None:
            # Wrapper has no segment_map of its own — synthesize a group
            # keyed on the wrapper's title_id so the group_id is
            # deterministic across re-runs. Tagged with a non-comma prefix
            # so it can't collide with a real sorted-segment-set key.
            wrapper_key = f"__subsumed_wrapper__:{wrapper_tid}"
            groups.setdefault(wrapper_key, [wrapper_tid])
            title_to_key[wrapper_tid] = wrapper_key
        # If the subsumed m2ts was placed in its own singleton group
        # (because it has a segment_map for its own clip id), evict it
        # so it doesn't render as its own row.
        old_key = title_to_key.get(tid)
        if old_key and old_key != wrapper_key:
            groups[old_key] = [x for x in groups[old_key] if x != tid]
            if not groups[old_key]:
                del groups[old_key]
        if tid not in groups[wrapper_key]:
            groups[wrapper_key].append(tid)
        title_to_key[tid] = wrapper_key

    for normalized_seg, title_ids in groups.items():
        _attach_duplicate_info_for_group(titles_by_id, disc_id, normalized_seg, title_ids)


def _auto_select_primary(
    titles_by_id: dict[str, dict[str, Any]],
    group_title_ids: list[str],
    metrics_per_tid: dict[str, dict[str, Any]],
) -> None:
    """Pick the best title as primary if no title in the group is already active."""
    has_active = any(
        titles_by_id.get(tid, {}).get("active") is True for tid in group_title_ids
    )
    if has_active:
        return

    def _score(tid: str) -> tuple[float, int]:
        m = metrics_per_tid.get(tid) or {}
        p = titles_by_id.get(tid) or {}
        audio = (m.get("audio_score") or 0) * 3
        chapters = m.get("chapters_count") or 0
        size = p.get("size") or p.get("mkv_size") or 0
        size_gb = size / (1024**3) if size else 0
        px = m.get("video_pixels") or 0
        return (audio + chapters + size_gb + px / 1e10, size)

    best_tid = max(group_title_ids, key=_score)
    for tid in group_title_ids:
        payload = titles_by_id.get(tid)
        if payload:
            payload["active"] = tid == best_tid
