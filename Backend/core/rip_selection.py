"""
Selective rip set construction for the segment-reorder workstream.

Used only by the Phase 2 Path A flow (Midway-class obfuscated discs where the
default `mkv DEV all OUT` would write 200+ duplicates of the same content).
For all other discs, `Backend/core/disc.py:rip()` continues to invoke
makemkvcon with `all` as before — `build_rip_title_set` is not called.

Architecture: after the user identifies a canonical playlist via
segment-reorder, we still want to rip everything *except* the unmatched
same-sorted-segment-map siblings. The result is a list of MakeMKV title
indexes that get fed to a per-title rip loop.

Spike data (Midway, 2026-05-03): per-title `makemkvcon mkv DEV INDEX OUT`
incurs ~1 min of disc enumeration overhead per call (`--noscan` is a flag
MakeMKV accepts but does not honor for `mkv` mode). For Midway with
~22 ripped titles and ~50 GB of data, the loop takes ~55 min total
(~22 min enum overhead + ~33 min byte copy). Acceptable on this code
path because (a) it only fires on heavily-obfuscated discs, (b) without
it Midway would need to write 7.4 TB of duplicates, (c) the user has
explicitly opted in via the threshold modal.
"""
from __future__ import annotations

from typing import Iterable


def build_rip_title_set(
    all_title_indexes: Iterable[int],
    canonical_title_index: int,
    duplicate_group_member_indexes: Iterable[int],
) -> list[int]:
    """Return the per-title rip set: all titles minus the unmatched duplicate-group siblings.

    Args:
        all_title_indexes: every MakeMKV title index on the disc (including the
            duplicate group AND every other unique title we'd rip via `all` today).
        canonical_title_index: the duplicate-group member the user identified
            via segment-reorder. Always retained.
        duplicate_group_member_indexes: every member of the canonical's duplicate
            group (titles sharing the same sorted segment map). All are dropped
            from the rip set EXCEPT the canonical.

    Returns:
        A sorted list of title indexes to rip (canonical + all non-group titles).

    Raises:
        ValueError: if `canonical_title_index` is not present in
            `all_title_indexes`, or if it is missing from
            `duplicate_group_member_indexes`. Both indicate a caller bug — the
            duplicate group must contain the canonical, and the canonical must
            be a real title on the disc.
    """
    all_set = set(all_title_indexes)
    group_set = set(duplicate_group_member_indexes)

    if canonical_title_index not in all_set:
        raise ValueError(
            f"canonical_title_index={canonical_title_index} not in all_title_indexes"
        )
    if canonical_title_index not in group_set:
        raise ValueError(
            f"canonical_title_index={canonical_title_index} not in "
            f"duplicate_group_member_indexes={sorted(group_set)}"
        )

    # Drop the unmatched siblings; keep everything else (canonical + non-group titles).
    decoys = group_set - {canonical_title_index}
    return sorted(all_set - decoys)
