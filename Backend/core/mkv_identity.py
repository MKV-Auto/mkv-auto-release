"""Read-only file-identity helpers backed by Matroska container metadata.

Every MKV produced by MakeMKV carries a random 128-bit Segment UID in its
container header. Reading it (``mkvmerge -J``) gives us a stable, intrinsic
identifier for the file that survives renames, moves, and library
reorganisations — unlike filename-based heuristics.

This module is the single read path. There is no write path by design: we
never modify files. Capture happens at postprocess time (when the file is
in its final muxed form and still on local storage) and is stored on
``DiscTitle.segment_uid``.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Mapping, Optional

log = logging.getLogger(__name__)


def read_segment_uid(mkv_path: str) -> Optional[str]:
    """Return the Matroska Segment UID (hex) for ``mkv_path``, or None.

    Invokes ``mkvmerge -J <path>`` and reads
    ``container.properties.segment_uid`` from the JSON response. Pure read —
    no side effects, no file modification.

    Returns None on any failure (missing ``mkvmerge`` binary, non-zero exit,
    JSON parse error, missing field, OS error). All failures are logged at
    WARNING but are non-fatal — a NULL UID just means downstream consumers
    fall back to heuristic match.
    """
    if not shutil.which("mkvmerge"):
        log.warning(
            "read_segment_uid: mkvmerge binary not on PATH; cannot read %s",
            mkv_path,
        )
        return None
    try:
        result = subprocess.run(
            ["mkvmerge", "-J", mkv_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("read_segment_uid: invocation failed for %s: %s", mkv_path, exc)
        return None
    if result.returncode != 0:
        log.warning(
            "read_segment_uid: mkvmerge -J exit=%d for %s: %s",
            result.returncode,
            mkv_path,
            result.stderr.strip(),
        )
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log.warning("read_segment_uid: JSON parse failed for %s: %s", mkv_path, exc)
        return None
    uid = (
        data.get("container", {})
        .get("properties", {})
        .get("segment_uid")
    )
    if not uid:
        log.warning(
            "read_segment_uid: no segment_uid in container properties for %s",
            mkv_path,
        )
        return None
    return str(uid)


def capture_segment_uids_for_titles(
    db: Any,
    disc_id: str,
    post_paths: Mapping[str, str],
    base_dir: str,
) -> int:
    """For each ``{title_id: rel_path}`` in ``post_paths``, read the Segment
    UID at ``base_dir/rel_path`` and write it onto the corresponding
    ``DiscTitle.segment_uid``. Returns the number of rows updated.

    Wraps :func:`read_segment_uid`. Skips silently when the read returns
    None (already logged at WARNING inside the read), so per-file failures
    never abort the loop. ``db.flush`` is the caller's responsibility — keep
    transaction boundaries with the surrounding workflow.
    """
    from api import models as db_models  # local import to avoid cycles

    updated = 0
    for title_id, rel_path in post_paths.items():
        abs_path = os.path.join(str(base_dir), rel_path)
        uid = read_segment_uid(abs_path)
        if not uid:
            continue
        title_row = (
            db.query(db_models.DiscTitle)
            .filter(db_models.DiscTitle.id == title_id)
            .first()
        )
        if title_row is not None:
            title_row.segment_uid = uid
            updated += 1
    return updated
