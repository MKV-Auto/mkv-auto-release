"""Human-readable labels for pipeline job toasts (movie/release name + optional Disc #).

Disc index prefers ``disc.disc_number`` (``discs`` row) when set; otherwise ``job.disc_num``.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple


def _job_notification_parts(job: Any, disc: Any | None = None) -> Tuple[str, Optional[str]]:
    """
    Resolve display name and optional disc index string for "Disc #…" suffix.

    Name order matches awaiting_labeling: movie.name → release.name → disc.info_title → 'this disc'.
    Disc index: ``disc.disc_number`` when the disc row has a value, else ``job.disc_num``.
    """
    if disc is None:
        disc = getattr(job, "disc", None)
    work: Optional[str] = None
    if disc is not None:
        rel = getattr(disc, "release", None)
        if rel is not None:
            movie = getattr(rel, "movie", None)
            work = (getattr(movie, "name", None) if movie else None) or getattr(rel, "name", None)
        if not work:
            work = getattr(disc, "info_title", None)
    name = (work or "this disc").strip() or "this disc"
    disc_row_num = getattr(disc, "disc_number", None) if disc is not None else None
    if disc_row_num is not None:
        dn = str(disc_row_num)
    else:
        disc_num = getattr(job, "disc_num", None)
        dn = str(disc_num).strip() if disc_num is not None and str(disc_num).strip() else None
    return name, dn


def job_notification_work_name(job: Any, disc: Any | None = None) -> str:
    """Base work title (no Disc # suffix), for envelope short titles."""
    name, _ = _job_notification_parts(job, disc)
    return name


def job_audience_label(job: Any, disc: Any | None = None) -> str:
    """
    Full user-facing subject: 'Movie Name' or 'Movie Name Disc #2' when a disc index is available.
    """
    name, dn = _job_notification_parts(job, disc)
    if dn:
        return f"{name} Disc #{dn}"
    return name


def job_notification_short_envelope_title(work_name: str) -> str:
    """Truncate work name for notification envelope title (max 100 chars)."""
    return work_name if len(work_name) <= 100 else (work_name[:97] + "...")
