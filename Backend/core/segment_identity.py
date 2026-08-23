"""Does a disc's MakeMKV segment map identify *content*? (#831)

Every duplicate / dedupe layer in the backend keys titles by their segment
map — ``duplicate_group_sync`` (exact map → primary + demoted secondaries),
``path_b_dedupe.compute_dedupe_groups`` (sorted segment set + duration →
representative + ``segment_set_sibling``), ``duplicate_info.attach_duplicate_info``
(the left-rail group chip) and the Path A trigger. All of them assume what
is true on Blu-ray: the map lists disc-global ``.m2ts`` clip IDs
(``00045,00046``), so "same segments" means "same video".

On a DVD MakeMKV reports the title's cell list *relative to its own program
chain*. Every 6-cell episode is ``1-5,6``; every 2-cell extra is ``1,2``. The
map describes the title's shape, not its content — six different episodes
of a TV box set carry the identical map and collapse into one "duplicate"
group with five hidden siblings (Star Wars Rebels Season Two discs 2–4 on
prod, and every other DVD box set). MakeMKV also emits no source filename
for DVD titles (``title-N``), so there is nothing else to key on.

This module is the single place that encodes that distinction. Callers ask
``segment_maps_identify_content(disc.format)`` and, when it is False, skip
segment-map grouping entirely. Nothing real is lost: MakeMKV's own DVD scan
already collapses true duplicate PGCs, and shape-keyed grouping never
caught decoy-title DVDs either (decoys and real titles collide equally).
"""
from __future__ import annotations

from typing import Any


def segment_maps_identify_content(disc_format: Any) -> bool:
    """True when ``disc_format`` is one whose segment maps carry identity.

    Blu-ray / UHD → True. DVD → False. Unknown / blank → True, so a disc
    whose format was never recorded keeps the legacy behaviour rather than
    silently losing duplicate detection.
    """
    if disc_format is None:
        return True
    text = str(disc_format).strip().upper()
    if not text:
        return True
    return "DVD" not in text
