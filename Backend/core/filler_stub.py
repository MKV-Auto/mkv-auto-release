"""Filler-stub auto-ignore: titles too short *and* too small to be content (#892).

Some discs carry dozens of tiny titles — studio logos, menu stubs, zero-length
playlists (RE: Apocalypse UHD has ~115). Each had to be ignored by hand.

The rule needs both conditions, and the evidence for it is the prod library
as labelled by hand (2026-10-11):

- 1,661 titles ≤ 10 s were ignored; **5 were kept** (4 backdrops, 1 extra) —
  so duration alone would hide real content. The corpus study found short
  stubs unsafe on duration even under 30 s.
- None of those 5 is ≤ 1 MB. The smallest title ever kept, at any length, is
  3.05 MB (5 s Matrix backdrops).
- "≤ 10 s **and** ≤ 1 MiB" matches 969 titles: 965 ignored by hand, 4 not yet
  labelled, **0 kept**.

Sizes and durations are MakeMKV's scan values. A title with an unknown size
or duration never qualifies.

What a match gets: ``auto_type='ignore'`` and ``obfuscation_reason=
'filler_stub'`` (a neutral "Filler" badge). It is only a label — the title is
still ripped (#880's accuracy rule) — and it never overrides a title the user
typed, or one DiscDB / earlier automation typed as something other than
ignore. A stronger reason already on the row (segment sibling, Path A decoy,
play-all, ffprobe corrections) is kept. If a rescan moves a title out of
range, the mark and the ignore it caused are cleared. Idempotent.
"""
from __future__ import annotations

from typing import Any

FILLER_STUB_REASON = "filler_stub"
MAX_STUB_SECONDS = 10.0
MAX_STUB_BYTES = 1024 * 1024

# Reasons this pass may replace: none, or MakeMKV's own weak per-title hint.
_REPLACEABLE_REASONS = {None, "", "makemkv_msg3307", FILLER_STUB_REASON}


def _positive_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


def is_filler_stub(duration: Any, size: Any) -> bool:
    """True when both the duration and the size are known and tiny."""
    secs = _positive_number(duration)
    nbytes = _positive_number(size)
    if secs is None or nbytes is None:
        return False
    return secs <= MAX_STUB_SECONDS and nbytes <= MAX_STUB_BYTES


def apply_filler_stub_marks(db: Any, rows: list[Any]) -> tuple[int, int]:
    """Persist filler-stub marks on ORM ``DiscTitle`` rows.

    Returns ``(marked, cleared)``. Does not commit.
    """
    from api.crud import set_title_type

    marked = 0
    cleared = 0
    for r in rows:
        reason = getattr(r, "obfuscation_reason", None)
        user_type = (str(getattr(r, "user_type", None) or "")).strip().lower()
        auto_type = (str(getattr(r, "auto_type", None) or "")).strip().lower()
        if is_filler_stub(getattr(r, "duration", None), getattr(r, "size", None)):
            if user_type:
                continue  # the user decided
            current = (str(getattr(r, "type", None) or "")).strip().lower()
            if current and current != "ignore":
                continue  # DiscDB / earlier automation typed it
            changed = False
            if auto_type != "ignore":
                set_title_type(r, "ignore", source="auto")
                changed = True
            if reason in _REPLACEABLE_REASONS and reason != FILLER_STUB_REASON:
                r.obfuscation_reason = FILLER_STUB_REASON
                changed = True
            if changed:
                marked += 1
        elif reason == FILLER_STUB_REASON:
            r.obfuscation_reason = None
            if not user_type and auto_type == "ignore":
                set_title_type(r, None, source="auto")
            cleared += 1
    return marked, cleared
