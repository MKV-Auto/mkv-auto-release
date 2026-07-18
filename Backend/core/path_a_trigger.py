"""
Path A trigger gate.

When the frontend POSTs /jobs/rip on a disc that has both:
  (1) at least one duplicate-segment-map group (Midway-class obfuscation), AND
  (2) projected rip > MKVAUTO_RIP_REVIEW_THRESHOLD_GB

we return 409 needs_user_choice instead of starting the rip. The frontend
shows the threshold modal; the user picks "Find canonical" (Path A) or
"Rip whole disc anyway" (force flag, future). On every other disc — the
vast majority — start_rip proceeds via the default all-mode path with
no behavioral change.

The 200 GB default threshold is configurable via env. Telemetry from
Phase 2 will confirm whether 200 is the right setting; a too-low value
would surface the modal on multi-cut UHDs where the user just wants a
plain rip.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from core.segment_reorder import (
    DuplicateGroup,
    detect_duplicate_segment_groups,
)
from core.utils import calculate_required_rip_space_bytes

logger = logging.getLogger(__name__)


DEFAULT_THRESHOLD_GB = 200
DEFAULT_BUFFER_MULTIPLIER = 1.3


def get_threshold_gb() -> int:
    """Resolve the threshold from env (advanced users / docker) with a 200 GB default."""
    raw = os.getenv("MKVAUTO_RIP_REVIEW_THRESHOLD_GB", "").strip()
    if not raw:
        return DEFAULT_THRESHOLD_GB
    try:
        v = int(raw)
        if v <= 0:
            logger.warning(
                "MKVAUTO_RIP_REVIEW_THRESHOLD_GB=%r is non-positive; using default %s",
                raw, DEFAULT_THRESHOLD_GB,
            )
            return DEFAULT_THRESHOLD_GB
        return v
    except ValueError:
        logger.warning(
            "MKVAUTO_RIP_REVIEW_THRESHOLD_GB=%r is not an integer; using default %s",
            raw, DEFAULT_THRESHOLD_GB,
        )
        return DEFAULT_THRESHOLD_GB


@dataclass
class PathADecision:
    """The verdict for one /jobs/rip request.

    `needs_user_choice` is the only field the API really gates on. The
    other fields are returned to the frontend so the modal can show
    accurate numbers (projected size, available disk) without a follow-up
    round-trip.

    Within a single duplicate-segment-map group, every member references
    the same underlying segments — picking the exploratory title is a
    backend concern, not a user concern. `auto_picked_exploratory_title_index`
    is the resolved pick by precedence (DiscDB classification > MakeMKV
    flag-clear > first member of the largest group). Frontend just hits
    /jobs/rip-with-segment-reorder; the endpoint reuses this pick when
    no exploratory_title_index is supplied.
    """

    needs_user_choice: bool
    projected_rip_bytes: int | None = None
    threshold_gb: int = DEFAULT_THRESHOLD_GB
    duplicate_groups: list[DuplicateGroup] = field(default_factory=list)
    auto_picked_exploratory_title_index: int | None = None
    reason: str = ""

    def to_409_payload(self, available_disk_bytes: int | None = None) -> dict:
        """Body shape returned with HTTP 409 to drive the threshold modal."""
        # Candidates retained for diagnostic display ("we found N similar
        # playlists"), but the modal does not let the user pick — the
        # backend's auto_picked_exploratory_title_index is authoritative.
        candidates: list[dict] = []
        for g in self.duplicate_groups:
            for idx in g.title_indexes:
                candidates.append({
                    "title_index": idx,
                    "duplicate_group_size": g.size,
                    "sorted_segment_key": g.sorted_segment_key,
                })
        return {
            "code": "needs_user_choice",
            "reason": self.reason,
            "threshold_gb": self.threshold_gb,
            "projected_rip_bytes": self.projected_rip_bytes,
            "available_disk_bytes": available_disk_bytes,
            "duplicate_group_count": len(self.duplicate_groups),
            "auto_picked_exploratory_title_index": self.auto_picked_exploratory_title_index,
            "candidates": candidates,
        }


def _auto_pick_exploratory(
    titles: dict[int, dict] | None,
    duplicate_groups: list[DuplicateGroup],
) -> int | None:
    """Pick which playlist the exploratory rip should target.

    All members of a duplicate-segment-map group reference the same
    physical segments, so the choice doesn't affect what the user sees
    in the previews — it just affects which mpls filename gets handed to
    the MPLS parser. Precedence:

      1. The largest group (most likely the obfuscated mass) is the focus.
      2. Within it, prefer a DiscDB-classified member (`type` is set and
         not 'ignore'/'junk') — its source_file is the canonical mpls.
      3. Else prefer a MakeMKV-flag-clear member (obfuscation_flag=False).
      4. Else the first member by title index.

    Returns None when there are no groups (caller doesn't need this
    field) or when titles dict is empty.
    """
    if not duplicate_groups or not titles:
        return None
    group = duplicate_groups[0]  # largest first per detect_duplicate_segment_groups
    members = list(group.title_indexes)
    if not members:
        return None

    def _meta(idx: int) -> dict:
        return titles.get(idx) or titles.get(str(idx)) or {}

    def _classified(idx: int) -> bool:
        t = (_meta(idx).get("type") or "").strip().lower()
        return bool(t) and t not in ("ignore", "junk")

    def _flag_clear(idx: int) -> bool:
        return not bool(_meta(idx).get("obfuscation_flag", False))

    for idx in members:
        if _classified(idx):
            return idx
    for idx in members:
        if _flag_clear(idx):
            return idx
    return members[0]


def evaluate_path_a_trigger(
    titles: dict[int, dict] | None,
    disc_size_bytes: int | None,
    *,
    threshold_gb: int | None = None,
    buffer_multiplier: float = DEFAULT_BUFFER_MULTIPLIER,
) -> PathADecision:
    """Decide whether a /jobs/rip request should be deferred to Path A.

    Both conditions must hold:
      1. The disc has at least one duplicate-segment-map group AND
      2. The projected rip exceeds the configured threshold.

    Returns:
        PathADecision. When needs_user_choice is False, the caller
        proceeds with the default all-mode rip exactly as today.
    """
    if threshold_gb is None:
        threshold_gb = get_threshold_gb()

    decision = PathADecision(
        needs_user_choice=False,
        threshold_gb=threshold_gb,
    )

    if not titles:
        decision.reason = "no_titles_to_evaluate"
        return decision

    groups = detect_duplicate_segment_groups(titles)
    if not groups:
        decision.reason = "no_duplicate_segment_groups"
        return decision

    decision.duplicate_groups = groups
    decision.auto_picked_exploratory_title_index = _auto_pick_exploratory(titles, groups)
    projected = calculate_required_rip_space_bytes(
        titles, disc_size_bytes, buffer_multiplier=buffer_multiplier,
    )
    decision.projected_rip_bytes = projected

    if projected is None:
        # We can't size the rip, so we can't gate. Fall through to default
        # behavior; user will discover the issue at MakeMKV time.
        decision.reason = "projected_size_unknown"
        return decision

    threshold_bytes = threshold_gb * (1024 ** 3)
    if projected <= threshold_bytes:
        decision.reason = "below_threshold"
        return decision

    decision.needs_user_choice = True
    decision.reason = "obfuscation_and_over_threshold"
    return decision
