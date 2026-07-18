"""Fail-closed eligibility policy for multi-drive operations.

Given a target drive and the set of currently-attached drives, decide whether
a new rip is allowed. The policy is intentionally conservative: any drive
whose identity does not come from ``/dev/disk/by-id/`` is blocked from
operating while other drives are also attached, because its ``/dev/srN``
mount point is not stable enough to safely coexist with kernel renumbering.

See GitHub issue #540 for the diagnostic that motivated this. Decision
codes are stable strings so the frontend can render a contextual warning
banner per code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.drive_identity import DriveIdentity


# Stable string codes returned in ``Decision.code``. Keep in sync with the
# frontend's drive-card warning renderer.
CODE_UNIDENTIFIABLE = "drive_unidentifiable"
CODE_UNSAFE_WITH_OTHERS = "drive_unsafe_with_others"


@dataclass(frozen=True)
class Decision:
    """Outcome of an eligibility check.

    ``allowed`` is True iff the rip may proceed. When False, ``code`` carries
    a stable identifier the frontend can map to UI, and ``message`` carries
    a default human-readable string.
    """

    allowed: bool
    code: Optional[str] = None
    message: Optional[str] = None


_ALLOW = Decision(allowed=True)


def evaluate_drive_for_rip(
    target: DriveIdentity,
    *,
    all_drives: list[DriveIdentity],
) -> Decision:
    """Return a :class:`Decision` for starting a rip on ``target``.

    ``all_drives`` is the full list of currently-attached optical drives
    (typically the values from :func:`core.drive_identity.build_identity_map`).
    It MUST include ``target`` itself.
    """

    if target.identity_source == "unknown":
        return Decision(
            allowed=False,
            code=CODE_UNIDENTIFIABLE,
            message=(
                "This drive could not be identified. "
                "Try disconnecting and reconnecting it, or check the "
                "container's device passthrough configuration."
            ),
        )

    if not target.multi_drive_safe and _has_other_drives(target, all_drives):
        source = target.identity_source
        return Decision(
            allowed=False,
            code=CODE_UNSAFE_WITH_OTHERS,
            message=(
                "Multi-drive ripping is not supported for this drive — "
                f"we could not get a stable identifier from /dev/disk/by-id/ "
                f"(falling back to {source}). Please disconnect this drive "
                "OR all other drives before starting a rip, to prevent "
                "corrupted output."
            ),
        )

    return _ALLOW


def _has_other_drives(
    target: DriveIdentity,
    all_drives: list[DriveIdentity],
) -> bool:
    """True iff at least one drive in ``all_drives`` is not ``target``."""

    return any(d.by_id_serial != target.by_id_serial for d in all_drives)
