#!/usr/bin/env python3
"""#607 one-shot back-fill: heal DiscTitle.file_path / file_path_stage for
completed transfers whose post-transfer writer silently no-op'd due to
the Path().rglob() filesystem-walking bug.

The pre-#607 implementation of
``_update_title_file_paths_after_transfer`` walked the destination via
``pathlib.Path(dest_root).rglob('*.mkv')`` — only works on a local
filesystem. For SMB / rsync / NFS the ``dest_path`` is a URI string that
``Path`` can't walk, so the title map stayed empty and the rows never
flipped to ``file_path_stage='transfer'``. The Library disc drawer then
rendered "In transient" for files that had actually been transferred.

This script replays the (now-fixed) writer against every completed
transfer in the DB so the rows catch up. Idempotent: re-running is safe
because the writer is a deterministic concat of ``dest_root`` +
``post_paths[tid]`` and only overwrites with the same value on a second
pass.

Run inside the mkv-auto container:

    docker exec -it mkv-auto python /opt/mkv-auto/scripts/backfill_transfer_file_paths.py

Or from a dev checkout with ``Backend`` on the path:

    cd Backend && .venv/bin/python ../scripts/backfill_transfer_file_paths.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


# Make the Backend package importable when invoked from anywhere.
_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent / "Backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def main() -> int:
    from api import database, models
    from api.routers.jobs import _update_title_file_paths_after_transfer

    db = database.SessionLocal()
    try:
        candidates = (
            db.query(models.Job)
            .filter(
                models.Job.transfer_state == "completed",
                models.Job.transfer_paths.isnot(None),
                models.Job.post_paths.isnot(None),
            )
            .all()
        )
        if not candidates:
            print("#607 back-fill: no completed transfers found; nothing to do.")
            return 0

        applied = 0
        skipped = 0
        for job in candidates:
            transfer_paths = job.transfer_paths or []
            if not transfer_paths:
                skipped += 1
                continue
            # The writer is idempotent — re-running on rows already at
            # 'transfer' just re-writes the same value. Track count of
            # jobs touched, not row deltas.
            try:
                _update_title_file_paths_after_transfer(
                    job, db, list(transfer_paths)
                )
                applied += 1
            except Exception as exc:
                skipped += 1
                print(
                    f"#607 back-fill: job {job.id} failed ({exc!s}); skipped"
                )
        db.commit()
        print(
            f"#607 back-fill: applied to {applied} job(s); skipped {skipped}."
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
