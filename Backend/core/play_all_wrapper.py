"""Play-all wrapper detection for discs without clip identity (#831).

On Blu-ray a "Play All" playlist is caught structurally: its ``.mpls``
segment map lists the ``.m2ts`` clips of the parts, so the m2ts ⊆ mpls fold
(``path_b_dedupe.apply_subsumption_marks``) knows exactly which titles it
wraps. On a DVD there is no such identity (see ``core.segment_identity``),
but the arithmetic is just as telling: the Rebels Season Two disc 2 "Play
All" is 2155 s, and the six Rebels Recon shorts that follow it are
339+353+341+316+453+353 = 2155 s. A title whose duration is the sum of a
run of ≥ 2 *consecutive* other titles' durations is a play-all of them.

Deliberately conservative:

- **Contiguous by title index only.** MakeMKV lists DVD titles in disc
  order and a play-all PGC sits next to its parts. Arbitrary subsets would
  be a subset-sum lottery on a 30-title disc.
- **Tolerance scales with the part count** (MakeMKV rounds each duration
  to whole seconds, so a 6-part sum can legitimately be off by ±3 s):
  ``1 + 0.5 × parts`` seconds.
- **Every part is a real part**: ≥ 30 s and ≥ 5 % of the wrapper, so a
  1 s junk title can't top up an episode to match its neighbour.
- **Parts are claimed once.** Wrappers are resolved longest-first and a
  title already claimed as a part cannot be another wrapper's part, so a
  season play-all that wraps episode play-alls resolves sanely.
- **DVD only at the call site.** On Blu-ray the clip-ID fold is strictly
  better evidence, and 175-playlist obfuscated discs are exactly where a
  duration coincidence would bite.

What the caller does with a match: ``auto_type='ignore'`` with
``obfuscation_reason='play_all_wrapper'`` on the wrapper, so it stays
visible as an auto-ignored-awaiting-review row with a "Play All" badge,
and the individually named parts are what gets ripped. The user flips it
by giving the wrapper a type. The parts' indexes are surfaced on the
workflow context as ``play_all_of`` for the badge text.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

PLAY_ALL_WRAPPER_REASON = "play_all_wrapper"

MIN_PARTS = 2
# A wrapper must be at least this long; guards against two 30 s logos
# "summing" to a 60 s menu loop.
MIN_WRAPPER_SECONDS = 300
# Every part must be a real piece of the wrapper. Without this a 1 s junk
# title plus a 1323 s episode "sums" to the 1324 s episode next to it (seen
# on the very fixture this was written for).
MIN_PART_SECONDS = 30
MIN_PART_FRACTION_OF_WRAPPER = 0.05


@dataclass(frozen=True)
class PlayAllMatch:
    wrapper_id: str
    wrapper_index: int
    part_ids: tuple[str, ...]
    part_indexes: tuple[int, ...]


def _duration_seconds(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def tolerance_seconds(part_count: int) -> float:
    return 1.0 + 0.5 * part_count


def detect_play_all_wrappers(
    titles: Iterable[tuple[str, int | None, Any]],
) -> list[PlayAllMatch]:
    """``titles`` is an iterable of ``(title_id, index, duration_seconds)``.

    Returns the wrappers found, each with the contiguous run of parts it
    wraps. Titles with no index or no positive duration are ignored.
    """
    rows: list[tuple[str, int, float]] = []
    for tid, idx, dur in titles:
        d = _duration_seconds(dur)
        if tid is None or idx is None or d is None:
            continue
        try:
            rows.append((str(tid), int(idx), d))
        except (TypeError, ValueError):
            continue
    if len(rows) < MIN_PARTS + 1:
        return []
    rows.sort(key=lambda r: r[1])
    n = len(rows)
    prefix = [0.0]
    for _, _, d in rows:
        prefix.append(prefix[-1] + d)

    claimed: set[int] = set()  # positions already used as parts or wrappers
    matches: list[PlayAllMatch] = []
    # Longest first so a season play-all claims its episode play-alls'
    # parts before those play-alls are considered.
    for wpos in sorted(range(n), key=lambda p: -rows[p][2]):
        if wpos in claimed:
            continue
        wid, widx, wdur = rows[wpos]
        if wdur < MIN_WRAPPER_SECONDS:
            continue
        best: tuple[float, int, int, int] | None = None  # (|diff|, distance, start, end)
        # Contiguous windows [start, end) of ≥ MIN_PARTS rows excluding wpos
        # and any claimed row.
        min_part = max(MIN_PART_SECONDS, MIN_PART_FRACTION_OF_WRAPPER * wdur)
        for start in range(n):
            if start == wpos or start in claimed or rows[start][2] < min_part:
                continue
            for end in range(start + MIN_PARTS, n + 1):
                span = range(start, end)
                if wpos in span:
                    break
                if (end - 1) in claimed or rows[end - 1][2] < min_part:
                    break
                total = prefix[end] - prefix[start]
                parts = end - start
                if total > wdur + tolerance_seconds(parts):
                    break  # durations are positive; longer windows only grow
                diff = abs(total - wdur)
                if diff <= tolerance_seconds(parts):
                    distance = min(abs(start - wpos), abs(end - 1 - wpos))
                    cand = (diff, distance, start, end)
                    if best is None or cand < best:
                        best = cand
        if best is None:
            continue
        _, _, start, end = best
        part_positions = list(range(start, end))
        claimed.update(part_positions)
        claimed.add(wpos)
        matches.append(PlayAllMatch(
            wrapper_id=wid,
            wrapper_index=widx,
            part_ids=tuple(rows[p][0] for p in part_positions),
            part_indexes=tuple(rows[p][1] for p in part_positions),
        ))
    matches.sort(key=lambda m: m.wrapper_index)
    return matches


def apply_play_all_wrapper_marks(db: Any, rows: list[Any]) -> tuple[int, int]:
    """Persist wrapper marks on ORM ``DiscTitle`` rows. Returns
    ``(wrappers_marked, stale_marks_cleared)``.

    - A detected wrapper with no ``user_type`` and an empty / ignore type
      gets ``auto_type='ignore'`` + ``obfuscation_reason='play_all_wrapper'``.
      A wrapper the user or DiscDB already typed is left alone — they know
      what it is.
    - A row carrying ``play_all_wrapper`` that is no longer detected as one
      (titles changed, durations corrected by ffprobe) has the reason
      cleared and, if its ignore came only from this pass, its auto ignore
      cleared too.
    Idempotent.
    """
    from api.crud import set_title_type

    matches = detect_play_all_wrappers(
        (str(r.id), getattr(r, "index", None), getattr(r, "duration", None)) for r in rows
    )
    wrapper_ids = {m.wrapper_id for m in matches}
    marked = 0
    cleared = 0
    for r in rows:
        rid = str(r.id)
        reason = getattr(r, "obfuscation_reason", None)
        user_type = (str(getattr(r, "user_type", None) or "")).strip().lower()
        if rid in wrapper_ids:
            if user_type:
                continue
            current = (str(getattr(r, "type", None) or "")).strip().lower()
            if current and current != "ignore":
                continue  # DiscDB / earlier automation typed it — respect that
            changed = False
            if (str(getattr(r, "auto_type", None) or "")).strip().lower() != "ignore":
                set_title_type(r, "ignore", source="auto")
                changed = True
            if reason != PLAY_ALL_WRAPPER_REASON:
                r.obfuscation_reason = PLAY_ALL_WRAPPER_REASON
                r.obfuscation_flag = True
                changed = True
            if changed:
                marked += 1
        elif reason == PLAY_ALL_WRAPPER_REASON:
            r.obfuscation_reason = None
            r.obfuscation_flag = False
            if not user_type and (str(getattr(r, "auto_type", None) or "")).strip().lower() == "ignore":
                set_title_type(r, None, source="auto")
            cleared += 1
    return marked, cleared


def annotate_play_all_of(titles_by_id: dict[str, dict]) -> None:
    """Pure compute for the workflow context: stamp ``play_all_of`` (the
    parts' title indexes) on each detected wrapper's payload dict."""
    matches = detect_play_all_wrappers(
        (tid, p.get("index"), p.get("duration"))
        for tid, p in titles_by_id.items() if isinstance(p, dict)
    )
    for m in matches:
        payload = titles_by_id.get(m.wrapper_id)
        if isinstance(payload, dict):
            payload["play_all_of"] = list(m.part_indexes)
