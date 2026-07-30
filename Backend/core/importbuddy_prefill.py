import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


def parse_copy_log(log_path: Path) -> Dict[str, Any]:
    """
    Parse MakeMKV makemkv/copy log to extract disc label/mount, saved titles, and summary counts.
    """
    if not log_path.exists():
        return parse_copy_log_text(None)
    return parse_copy_log_text(log_path.read_text(encoding="utf-8", errors="ignore"))


def parse_copy_log_text(text: Optional[str]) -> Dict[str, Any]:
    """Same parse over log text instead of a file path.

    The scan persists the raw ``info … -r`` output on the disc row, which
    outlives job-artifact cleanup — this entry point lets callers parse that
    stored copy.
    """
    data: Dict[str, Any] = {
        "disc_label": None,
        "mount_point": None,
        "titles": [],  # list of {index, source_file, segment_map, duration, size, display_size, tracks, chapters}
        "total_titles_saved": None,
        "skipped_titles": [],
    }
    if not text:
        return data

    # `(?:\(\d+\))?` — MakeMKV names an angle copy `00312.mpls(2)`; without it
    # the second copy never parses and its title exports with no tracks.
    pat_saved = re.compile(r'^MSG:3307,\d+,\d+,"File (\d{5}\.(?:mpls|m2ts)(?:\(\d+\))?) was added as title #(\d+)"')
    pat_total = re.compile(r'^MSG:5036,\d+,\d+,"Copy complete\. (\d+) titles saved\.')
    pat_drive = re.compile(r'^DRV:\d+,\d+,\d+,\d+,"[^"]*","([^"]*)","([^"]*)"')
    pat_skipped = re.compile(r'^MSG:(3309|3025),\d+,\d+,"(?:Title|File) #?(\d{5}\.(?:mpls|m2ts)(?:\(\d+\))?) .*skipped"')
    pat_tinfo = re.compile(r"^TINFO:(\d+),(\d+),(\d+),\"(.*)\"$")
    pat_sinfo = re.compile(r"^SINFO:(\d+),(\d+),(\d+),(\d+),\"(.*)\"$")

    titles: List[Dict[str, Any]] = []
    tinfo_map: Dict[int, Dict[str, Any]] = {}
    sinfo_map: Dict[int, List[Dict[str, Any]]] = {}
    skipped: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        m = pat_saved.match(line)
        if m:
            fn, tid = m.groups()
            titles.append({"index": int(tid), "source_file": fn})
            continue
        m = pat_tinfo.match(line)
        if m:
            idx = int(m.group(1))
            code = int(m.group(2))
            message = m.group(4)
            entry = tinfo_map.setdefault(idx, {})
            entry.setdefault("index", idx)
            match code:
                case 8:
                    entry["chapters"] = entry.get("chapters") or []
                    try:
                        entry["chapter_count"] = int(message)
                    except Exception:
                        pass
                case 9:
                    entry["duration"] = message
                case 10:
                    entry["display_size"] = message
                case 11:
                    try:
                        entry["size"] = int(message)
                    except Exception:
                        entry["size"] = message
                case 16:
                    entry["playlist"] = message
                case 26:
                    entry["segment_map"] = message
                case 27:
                    entry["comment"] = message
                case 49:
                    entry["java_comment"] = message
                case 24:
                    entry["playlist"] = message
            continue
        m = pat_sinfo.match(line)
        if m:
            track_idx = int(m.group(1))
            code = int(m.group(3))
            msg = m.group(5)
            segs = sinfo_map.setdefault(track_idx, [])
            seg_entry: Dict[str, Any] = segs[-1] if segs else {}
            if code == 1:
                seg_entry = {"index": int(m.group(2)), "type": msg}
                segs.append(seg_entry)
            elif code == 7:
                seg_entry["name"] = msg
            elif code == 2:
                seg_entry["audio_type"] = msg
            elif code == 3:
                seg_entry["language_code"] = msg
            elif code == 4:
                seg_entry["language"] = msg
            elif code == 19:
                seg_entry["resolution"] = msg
            elif code == 20:
                seg_entry["aspect_ratio"] = msg
            continue

        m = pat_total.match(line)
        if m:
            try:
                data["total_titles_saved"] = int(m.group(1))
            except ValueError:
                pass
            continue

        m = pat_skipped.match(line)
        if m:
            skipped.append(m.group(2))
            continue

        m = pat_drive.match(line)
        if m and not data["mount_point"]:
            label, mount = m.groups()
            data["disc_label"] = label or None
            data["mount_point"] = mount or None

    # merge tinfo/sinfo into titles
    merged: List[Dict[str, Any]] = []
    for t in titles:
        idx = t.get("index")
        enriched = {**t, **tinfo_map.get(idx, {})}
        enriched["tracks"] = sinfo_map.get(idx, [])
        merged.append(enriched)

    data["titles"] = sorted(merged, key=lambda t: t.get("index", 0))
    data["skipped_titles"] = skipped
    if data["total_titles_saved"] is None:
        data["total_titles_saved"] = len(titles) if titles else None
    return data


def probe_media(path: Path) -> Dict[str, Any]:
    """
    Run ffprobe to extract duration, size, and streams (minimal).
    """
    info: Dict[str, Any] = {
        "size": path.stat().st_size if path.exists() else None,
        "duration": None,
        "streams": {"video": [], "audio": [], "subtitle": []},
    }
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(proc.stdout or "{}")
        fmt = payload.get("format") or {}
        dur = fmt.get("duration")
        if dur:
            try:
                info["duration"] = float(dur)
            except ValueError:
                pass
        for stream in payload.get("streams") or []:
            entry = {
                "codec": stream.get("codec_name"),
                "codec_type": stream.get("codec_type"),
                "channels": stream.get("channels"),
                "channel_layout": stream.get("channel_layout"),
                "language": stream.get("tags", {}).get("language") if isinstance(stream.get("tags"), dict) else None,
                "width": stream.get("width"),
                "height": stream.get("height"),
            }
            ctype = stream.get("codec_type")
            if ctype == "video":
                info["streams"]["video"].append(entry)
            elif ctype == "audio":
                info["streams"]["audio"].append(entry)
            elif ctype in ("subtitle", "subtitles"):
                info["streams"]["subtitle"].append(entry)
    except Exception:
        # ffprobe unavailable or failed; keep best-effort size only
        pass
    return info


def build_prefill(base_dir: Path) -> Dict[str, Any]:
    """
    Build a prefill payload for manual labeling using copy.log + ffprobe + disc_info.json if present.
    """
    payload: Dict[str, Any] = {
        "disc_num": None,
        "mount_point": None,
        "disc_label": None,
        "disc_hash": None,
        "titles": [],
        "total_titles_saved": None,
    }
    copy_log = base_dir / "copy.log"
    makemkv_log = base_dir / "makemkv_progress.log"
    info_log = base_dir / "makemkv_info.log"
    if info_log.exists():
        log_path = info_log
    elif copy_log.exists():
        log_path = copy_log
    else:
        raise FileNotFoundError(f"makemkv_info.log not found under {base_dir}")
    parsed = parse_copy_log(log_path)
    payload.update(
        {
            "disc_label": parsed.get("disc_label"),
            "mount_point": parsed.get("mount_point"),
            "total_titles_saved": parsed.get("total_titles_saved"),
        }
    )

    # Optional disc_info.json enrichment
    disc_info_path = base_dir / "disc_info.json"
    if disc_info_path.exists():
        try:
            info = json.loads(disc_info_path.read_text())
            payload["disc_num"] = info.get("disc_num")
            payload["disc_hash"] = info.get("disc_hash")
        except Exception:
            pass

    # Collect mkv outputs and map by _tXX index
    outputs: Dict[int, Dict[str, Any]] = {}
    for mkv in base_dir.glob("*.mkv"):
        name = mkv.name
        match = re.search(r"_t(\d+)\.mkv$", name)
        if not match:
            continue
        idx = int(match.group(1))
        meta = probe_media(mkv)
        outputs[idx] = {
            "output_file": name,
            "probe": meta,
            "size": meta.get("size"),
            "duration": meta.get("duration"),
        }

    # Merge source files from log with outputs
    titles: List[Dict[str, Any]] = []
    for t in parsed.get("titles", []):
        idx = t.get("index")
        entry: Dict[str, Any] = {
            "index": idx,
            "track_id": t.get("source_file"),
            "source_file": t.get("source_file"),
            "output_file": outputs.get(idx, {}).get("output_file"),
            "size": outputs.get(idx, {}).get("size"),
            "duration": outputs.get(idx, {}).get("duration"),
            "probe": outputs.get(idx, {}).get("probe"),
            "content": t.get("source_file") not in (parsed.get("skipped_titles") or []),
        }
        titles.append(entry)

    # Append any extra outputs not found in log (best effort)
    for idx, meta in outputs.items():
        if any(t.get("index") == idx for t in titles):
            continue
        titles.append(
            {
                "index": idx,
                "track_id": meta.get("output_file"),
                "source_file": None,
                "output_file": meta.get("output_file"),
                "size": meta.get("size"),
                "duration": meta.get("duration"),
                "probe": meta.get("probe"),
                "content": True,
            }
        )

    payload["titles"] = sorted(titles, key=lambda t: t.get("index", 0))
    return payload
