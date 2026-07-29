"""Build a TheDiscDB submission as a zip laid out like the upstream repository.

The old export returned one JSON blob. Upstream wants a directory of files, so a
contributor had to split our blob into ``release.json`` / ``discNN.json`` /
``discNN-summary.txt`` by hand, source the MakeMKV log separately and redact it
themselves, find cover art, and work out the target path. This module emits the
directory instead: unzip it into a fork of ``TheDiscDb/data`` and open a PR.

    data/{movie|series|sets}/{Title (Year)}/{release-slug}/
        release.json
        discNN.json
        discNN-summary.txt
        discNN.txt          <- raw MakeMKV robot log, redacted
        front.jpg           <- when we have cover art
    README.txt

The redaction is the part that matters. A raw MakeMKV log names the drive
hardware, its serial number, and the device path; upstream's own copies replace
those with ``***`` and so must ours. See :func:`redact_makemkv_log`.
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# `DRV:index,flags,a,b,"drive_hardware_name","volume_label","device_path"`
# The three quoted fields carry the drive model, its serial (it is part of the
# hardware name MakeMKV reports) and the device path. Upstream redacts all three.
_DRV_LINE = re.compile(r'^(DRV:\d+(?:,[^,"]*){3},)(".*")\s*$')

# `MSG:1004,...,"Debug logging enabled, log will be saved as /home/someone/..."`
# The path can carry a username. Upstream ships this line already reduced to `***`.
_LOG_PATH_MSG = re.compile(r'^(MSG:1004,.*)$')

# `Using LibreDrive mode (v02.1 id=DFE22909F92F)` — a drive identifier. Upstream
# leaves it; we do not. Redacting costs nothing and nothing downstream reads it.
_LIBREDRIVE_ID = re.compile(r'(id=)[0-9A-Fa-f]{6,}')

# A MakeMKV registration key must never reach a public pull request. MakeMKV does
# not normally echo it, so this is belt-and-braces against a version that does.
_MAKEMKV_KEY = re.compile(r'\bT-[A-Za-z0-9@_+\-]{20,}')

# A home directory names its owner. Container paths are `/data/...` so this
# normally matches nothing, but MKVAUTO_ROOT defaults to `~/MakeMKV-Auto` when
# the app runs outside Docker, and that log would carry the account name.
_HOME_DIR = re.compile(r'((?:/home|/Users)/|[A-Za-z]:\\Users\\)([^/\\"\s]+)')


def redact_makemkv_log(text: str) -> str:
    """Strip drive identity from a MakeMKV robot-mode log.

    Matches what upstream's committed logs look like: ``DRV:`` lines keep their
    numeric fields and lose every quoted one. Empty drive slots
    (``DRV:5,256,999,0,"","",""``) are left alone — there is nothing in them to
    leak, and rewriting them would make our logs differ from upstream's for no
    reason.
    """
    out = []
    for line in text.splitlines():
        m = _DRV_LINE.match(line)
        if m:
            quoted = m.group(2)
            # An all-empty slot carries nothing; leave it byte-identical.
            if quoted.replace('"', '').replace(',', '').strip():
                fields = quoted.count(',') + 1
                line = m.group(1) + ",".join(['"***"'] * fields)
        elif _LOG_PATH_MSG.match(line):
            # The path appears twice — in the rendered message and again as the
            # format argument — so redact every quoted field that looks like one.
            line = _redact_log_path(line)
        line = _LIBREDRIVE_ID.sub(r'\1***', line)
        line = _MAKEMKV_KEY.sub('T-***', line)
        line = _HOME_DIR.sub(r'\1***', line)
        out.append(line)
    # Preserve a trailing newline if the source had one.
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _redact_log_path(line: str) -> str:
    """Replace filesystem paths inside a quoted MSG field with ``***``."""
    def sub(m: re.Match) -> str:
        inner = m.group(1)
        if "/" in inner or "\\" in inner:
            return '"***"'
        return m.group(0)

    return re.sub(r'"([^"]*)"', sub, line)


def _upstream_kind(release_type: str | None) -> str:
    """Upstream's top-level directory for this release type."""
    t = (release_type or "").strip().lower()
    if t in ("series", "tv", "show"):
        return "series"
    if t in ("boxset", "set", "sets", "collection"):
        return "sets"
    return "movie"


def _sanitize_dir(name: str) -> str:
    """Make a title safe as a directory component without mangling it.

    Upstream directory names are human titles (``Cinderella Man (2005)``), so
    only characters a filesystem or zip cannot carry are replaced.
    """
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).strip().rstrip(".")
    return cleaned or "Unknown"


def upstream_dir(title: str | None, year: Any, release_type: str | None, release_slug: str) -> str:
    """`data/movie/Cinderella Man (2005)/2025-4k` — where this entry belongs."""
    name = _sanitize_dir((title or "Unknown").strip())
    if year:
        name = f"{name} ({year})"
    return f"data/{_upstream_kind(release_type)}/{name}/{_sanitize_dir(release_slug)}"


README = """MKV-Auto — TheDiscDB submission
================================

This zip is laid out exactly like the TheDiscDb/data repository, so you can drop
it straight in.

  1. Fork and clone https://github.com/TheDiscDb/data
  2. Unzip this file into the root of your clone. The `data/...` directory in the
     zip lines up with the one in the repo, so the files land in the right place.
  3. Check `git status` — you should see only new files under the release
     directory listed below.
  4. Commit, push, and open a pull request.

Submission directory
--------------------
{target}

Files
-----
{files}

{notes}
Generated by MKV-Auto on {generated}.
"""


def _build_readme(target: str, files: list[str], notes: list[str]) -> str:
    return README.format(
        target=target,
        files="\n".join(f"  {f}" for f in files),
        notes=("Notes\n-----\n" + "\n".join(f"  - {n}" for n in notes) + "\n\n") if notes else "",
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


def _find_info_log(job_id: str) -> Optional[Path]:
    from core.job_paths import JobPaths

    paths = JobPaths.for_id(str(job_id))
    for cand_dir in (paths.raw, paths.metadata):
        cand = cand_dir / "makemkv_info.log"
        if cand.exists():
            return cand
    return None


def _stored_info_log(disc_id: str, db: Any) -> Optional[str]:
    """The scan's own copy of the MakeMKV info log, off ``disc.disc_info``.

    The job artifact is the *first* place to look but a poor place to rely on:
    it is written only when a rip had no cached title map, and job artifacts are
    cleaned up after a while. The scan persists the same ``info … -r`` output on
    the disc row, where it survives both — so in practice every scanned disc has
    a log available, including ones ripped long ago.
    """
    if db is None or not disc_id:
        return None
    try:
        from api import models as db_models

        disc = db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first()
        info = getattr(disc, "disc_info", None) if disc else None
        if not isinstance(info, dict):
            return None
        raw = info.get("raw_info_log") or info.get("info_log")
        if isinstance(raw, list):
            raw = "\n".join(raw)
        return raw if isinstance(raw, str) and raw.strip() else None
    except Exception as exc:
        logger.warning("discdb export: could not read the stored info log: %s", exc)
        return None


def _fetch_image(url: str | None, attempts: int = 3) -> Optional[bytes]:
    """Best-effort cover art. Never fatal — a failed fetch becomes a README note.

    Retried, unlike most best-effort fetches here: creating a release *requires* a
    front cover URL, so the image is known to exist and a single timeout is a poor
    reason to ship a submission without the file upstream expects.
    """
    if not url or not str(url).lower().startswith(("http://", "https://")):
        return None

    import requests

    last: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            # A 200 that is not an image — an error page, a login redirect — must
            # not be written out as front.jpg and submitted as cover art.
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype and not ctype.startswith("image/"):
                logger.info("discdb export: %s returned %s, not an image", url, ctype)
                return None
            return resp.content or None
        except Exception as exc:
            last = exc
            logger.info(
                "discdb export: cover art fetch %d/%d failed for %s: %s",
                attempt, attempts, url, exc,
            )
    logger.warning("discdb export: giving up on cover art %s: %s", url, last)
    return None


def _write_disc_entry(zf: zipfile.ZipFile, bundle: Dict[str, Any], job_id: str, db: Any,
                      written: set) -> Tuple[str, list, list]:
    """Write one disc's submission files into an open zip.

    Shared by the single-disc and export-all paths so the two can never drift
    into producing differently-shaped entries. ``written`` carries paths already
    in the archive across calls: discs of the same release land in one directory
    and would otherwise re-fetch and re-write the same cover art per disc.

    Returns ``(target_dir, files_written, notes)``.
    """
    release_json: Dict[str, Any] = bundle["release"]
    disc_json: Dict[str, Any] = bundle["disc"]
    disc_number = bundle.get("disc_number") or 1
    stem = f"disc{int(disc_number):02d}"

    target = upstream_dir(
        bundle.get("film_title") or release_json.get("Title"),
        bundle.get("film_year") or release_json.get("Year"),
        bundle.get("release_type"),
        bundle.get("release_slug") or "release",
    )

    files: list = []
    notes: list = []

    def write(rel: str, data: bytes | str) -> None:
        path = f"{target}/{rel}"
        if path in written:
            return
        zf.writestr(path, data.encode("utf-8") if isinstance(data, str) else data)
        written.add(path)
        files.append(rel)

    write("release.json", json.dumps(release_json, indent=2) + "\n")
    write(f"{stem}.json", json.dumps(disc_json, indent=2) + "\n")
    write(f"{stem}-summary.txt", bundle.get("summary") or "")

    # Job artifact first — contemporaneous with this rip — then the copy the
    # scan persisted on the disc row, which outlives artifact cleanup.
    raw_log: Optional[str] = None
    log_path = _find_info_log(job_id)
    if log_path:
        try:
            raw_log = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("discdb export: could not read %s: %s", log_path, exc)
    if raw_log is None:
        raw_log = _stored_info_log(str(bundle.get("disc_id") or ""), db)

    if raw_log:
        write(f"{stem}.txt", redact_makemkv_log(raw_log))
    else:
        notes.append(
            f"{stem}.txt is missing — no MakeMKV log is stored for this disc. "
            "Re-scanning the disc will capture one. Upstream entries include it."
        )

    # Upstream names these exactly `front.jpg` / `back.jpg` — there is not a
    # single non-jpg cover in TheDiscDb/data — so the names are fixed and a
    # non-image response is rejected rather than written under a jpg name.
    for label, url_key in (("front", "ImageUrl"), ("back", "BackImageUrl")):
        path = f"{target}/{label}.jpg"
        if path in written:
            continue  # a sibling disc of this release already fetched it
        url = release_json.get(url_key)
        if not url:
            # A front cover URL is required to create a release, so its absence
            # means a release predating that rule, not a normal state.
            notes.append(
                f"{label}.jpg is not included — this release has no {label} cover URL."
                if label == "back"
                else "front.jpg is not included — this release has no cover URL, which is "
                     "unusual. Add one in the UI and export again, or attach the art by hand."
            )
            continue
        art = _fetch_image(url)
        if art:
            write(f"{label}.jpg", art)
        else:
            # We know the URL; handing it over beats telling someone to go find
            # cover art they have already supplied once.
            notes.append(
                f"{label}.jpg could not be downloaded. Save it yourself from {url} "
                f"(or re-run the export — the failure may be transient)."
            )

    if not disc_json.get("GlobalDiscId"):
        notes.append(
            "GlobalDiscId is absent. It is the AACS disc ID (SHA1 of AACS/Unit_Key_RO.inf) "
            "and upstream treats it as optional — it cannot be derived from an MKV-only rip."
        )

    return target, files, notes


def build_discdb_zip(job_id: str, db: Any) -> Tuple[str, bytes]:
    """Return ``(filename, zip_bytes)`` for a single job's disc.

    Reuses :func:`core.discdb_finalize.generate_discdb_bundle` for the data so
    the zip and the JSON response can never disagree about content.
    """
    from core.discdb_finalize import generate_discdb_bundle

    bundle = generate_discdb_bundle(str(job_id), db)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        target, files, notes = _write_disc_entry(zf, bundle, str(job_id), db, set())
        zf.writestr("README.txt", _build_readme(target, files, notes))

    disc_number = bundle.get("disc_number") or 1
    base = _sanitize_dir(
        bundle.get("film_title") or bundle["release"].get("Title") or "discdb"
    )
    return f"{base}-disc{int(disc_number):02d}-thediscdb.zip", buf.getvalue()


BULK_README = """MKV-Auto — TheDiscDB submission ({count} disc{plural})
================================================

Every eligible disc in your library, laid out exactly like the TheDiscDb/data
repository so the whole set can go in at once.

  1. Fork and clone https://github.com/TheDiscDb/data
  2. Unzip this file into the root of your clone. The `data/...` tree lines up
     with the repo, so every release lands in its own directory.
  3. Check `git status` — you should see only new directories under `data/`.
  4. Commit, push, and open a pull request.

Upstream reviews these by hand. A pull request adding many releases at once is
harder to review than a few; if you have a large library, consider splitting it
across several PRs by removing directories from your clone before committing.

Included
--------
{included}
{skipped}{notes}
Generated by MKV-Auto on {generated}.
"""


def _eligible_contribution_discs(db: Any) -> list:
    """Discs worth submitting: finished job, labelled, not already upstream.

    ``job_status == "completed"`` is the gate rather than ``rip_state``: that is
    what "Finish" sets, and finishing is only offered once the whole workflow —
    rip, post-processing, transfer — is done. Exporting mid-workflow would
    submit data that is still moving.

    ``discdb_disc_num`` is only ever set on a TheDiscDB match, so a non-null
    value means the disc is already upstream and re-submitting it would open a
    duplicate.
    """
    from api import models as db_models

    # EXISTS rather than JOIN + DISTINCT. A disc can have several jobs, so the
    # join duplicates rows — but DISTINCT over the whole entity makes Postgres
    # compare every column for equality, and `discs` has `json` columns
    # (label_payload, disc_info, ...) which have no equality operator. That
    # fails at runtime with "could not identify an equality operator for type
    # json". SQLite has no such restriction, which is why the tests missed it.
    has_finished_job = (
        db.query(db_models.Job)
        .filter(
            db_models.Job.disc_id == db_models.Disc.id,
            db_models.Job.job_status == "completed",
        )
        .exists()
    )
    return (
        db.query(db_models.Disc)
        .filter(
            has_finished_job,
            db_models.Disc.release_id.isnot(None),
            db_models.Disc.discdb_disc_num.is_(None),
        )
        .all()
    )


def _latest_finished_job_id(db: Any, disc_id: str) -> Optional[str]:
    from api import models as db_models

    job = (
        db.query(db_models.Job)
        .filter(db_models.Job.disc_id == disc_id, db_models.Job.job_status == "completed")
        .order_by(db_models.Job.created_at.desc())
        .first()
    )
    return str(job.id) if job else None


def build_discdb_bulk_zip(
    db: Any,
    dest: "Path | None" = None,
    progress: "Callable[[int, int, str], None] | None" = None,
    should_cancel: "Callable[[], bool] | None" = None,
) -> Tuple[str, Optional[bytes], Dict[str, Any]]:
    """Every eligible disc in one archive.

    Returns ``(filename, zip_bytes_or_None, summary)`` — ``dest`` streams to that
    path and returns ``None`` for the bytes, so a large library never has to be
    held in memory. ``progress(done, total, label)`` is called per disc, and
    ``should_cancel()`` is checked between discs so a cancelled run stops without
    waiting for the remaining cover-art fetches.

    One disc failing must not lose the rest: a disc that raises is recorded as
    skipped in the README and the archive is still produced. Discs of the same
    release share a directory, which is exactly upstream's layout — and the
    shared ``written`` set means their cover art is fetched once, not once per
    disc.
    """
    from core.discdb_finalize import generate_discdb_bundle

    discs = _eligible_contribution_discs(db)
    total = len(discs)
    written: set = set()
    included: list = []
    skipped: list = []
    all_notes: list = []
    exported_ids: list = []

    cancelled = False
    sink = dest.open("wb") if dest is not None else io.BytesIO()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as zf:
        for index, disc in enumerate(discs):
            label = disc.disc_name or disc.info_title or str(disc.id)
            if should_cancel is not None and should_cancel():
                cancelled = True
                skipped.extend(
                    f"{(d.disc_name or d.info_title or d.id)} — cancelled"
                    for d in discs[index:]
                )
                break
            if progress is not None:
                progress(index, total, label)
            job_id = _latest_finished_job_id(db, str(disc.id))
            if not job_id:
                skipped.append(f"{label} — no finished job")
                continue
            try:
                bundle = generate_discdb_bundle(job_id, db)
                target, files, notes = _write_disc_entry(zf, bundle, job_id, db, written)
            except Exception as exc:
                logger.warning("discdb bulk export: skipping disc %s: %s", disc.id, exc)
                skipped.append(f"{label} — {exc}")
                continue
            included.append(f"{target}  ({', '.join(files)})" if files else target)
            all_notes.extend(f"{target}: {n}" for n in notes)
            exported_ids.append(str(disc.id))
        if progress is not None:
            progress(len(included) + len(skipped), total, "")

        zf.writestr("README.txt", BULK_README.format(
            count=len(included),
            plural="" if len(included) == 1 else "s",
            included="\n".join(f"  {i}" for i in included) or "  (nothing eligible)",
            skipped=("\nSkipped\n-------\n" + "\n".join(f"  {s}" for s in skipped) + "\n")
                    if skipped else "",
            notes=("\nNotes\n-----\n" + "\n".join(f"  - {n}" for n in all_notes) + "\n")
                  if all_notes else "",
            generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        ))

    if dest is not None:
        sink.close()

    summary = {
        "included": len(included),
        "skipped": len(skipped),
        "skipped_detail": skipped,
        "disc_ids": exported_ids,
        "total": total,
        "cancelled": cancelled,
    }
    payload = None if dest is not None else sink.getvalue()
    return "thediscdb-submissions.zip", payload, summary
