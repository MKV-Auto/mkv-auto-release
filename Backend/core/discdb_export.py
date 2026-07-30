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
  3. Delete this README.txt — it is instructions for you, not part of the
     submission — then check `git status`: {git_expect}.
  4. Commit, push, and open a pull request.

Submission directory
--------------------
{target}

Files
-----
{files}

{update_block}{notes}
Generated by MKV-Auto on {generated}.
"""

# macOS Finder's drag-and-drop *replaces* a folder's entire contents instead of
# merging, which on an update would delete the files upstream already has next
# to ours. Said wherever an update appears, because it is unrecoverable short
# of re-cloning.
MERGE_WARNING = """Merge, don't replace: the `unzip` command and Windows Explorer both merge
folders into place. macOS Finder does NOT — dragging a folder onto an existing
one replaces its entire contents, deleting the files upstream already has
beside these. On a Mac, unzip from a terminal."""


def _update_change_summary(ours: Dict[str, Any], theirs: Dict[str, Any]) -> list[str]:
    """What this update actually changes, as commit-message bullet lines.

    Computed against upstream's committed file — the same diff the pull
    request will show — so the contributor can paste an honest message
    instead of reverse-engineering their own change from ``git diff``.
    """
    # Compare what will actually be serialized: our in-memory dicts carry
    # null-valued keys that never reach the file, and counting those as
    # changes would report differences the PR diff does not show.
    ours = _without_nulls(ours)
    theirs = _without_nulls(theirs)
    lines: list[str] = []
    for key in ("Name", "Format", "Slug", "GlobalDiscId"):
        if key in ours and key in theirs and ours[key] != theirs[key]:
            lines.append(f'{key}: "{theirs[key]}" -> "{ours[key]}"')
        elif key in ours and key not in theirs:
            lines.append(f'{key} added: "{ours[key]}"')

    theirs_by_index = {
        t.get("Index"): t for t in theirs.get("Titles") or [] if isinstance(t, dict)
    }
    ours_titles = [t for t in ours.get("Titles") or [] if isinstance(t, dict)]
    # Continuation lines (see renderers) start with two spaces. Nothing is
    # capped or aggregated away: the prior value being replaced is the whole
    # point of the message, for every field it applies to.
    comment_changes: list[str] = []
    detail: list[str] = []
    for t in ours_titles:
        up = theirs_by_index.get(t.get("Index"))
        if up is None:
            detail.append(f"title {t.get('Index')} added ({t.get('SourceFile')})")
            continue
        if t.get("Comment") != up.get("Comment"):
            if up.get("Comment"):
                # Unreachable through the merge (their filename always wins),
                # but this function also reports on raw, unmerged dicts.
                comment_changes.append(
                    f'  title {t.get("Index")}: "{up.get("Comment")}" -> "{t.get("Comment")}"'
                )
            else:
                detail.append(f'title {t.get("Index")}: comment added "{t.get("Comment")}"')
        our_item = t.get("Item") or {}
        up_item = up.get("Item") or {}
        for field, label in (("Type", "type"), ("Title", "name"),
                             ("Season", "season"), ("Episode", "episode")):
            a, b = up_item.get(field), our_item.get(field)
            if a != b and (a or b):
                detail.append(f'title {t.get("Index")}: {label} "{a or "—"}" -> "{b or "—"}"')
        if (our_item.get("Chapters") or []) != (up_item.get("Chapters") or []):
            detail.append(
                f"title {t.get('Index')}: chapters updated "
                f"({len(up_item.get('Chapters') or [])} -> {len(our_item.get('Chapters') or [])})"
            )
        if t.get("Description") != up.get("Description") and (t.get("Description") or up.get("Description")):
            detail.append(
                f'title {t.get("Index")}: description "{up.get("Description") or "—"}" '
                f'-> "{t.get("Description") or "—"}"'
            )
        if (t.get("Tracks") or []) != (up.get("Tracks") or []):
            detail.append(f"title {t.get('Index')}: tracks updated")
    our_indices = {t.get("Index") for t in ours_titles}
    for idx in theirs_by_index:
        if idx not in our_indices:
            detail.append(f"title {idx} removed")

    if comment_changes:
        plural = "s" if len(comment_changes) != 1 else ""
        lines.append(f"{len(comment_changes)} title comment{plural} corrected:")
        lines.extend(comment_changes)
    lines.extend(detail)
    return lines


def _render_update_entry(update: Dict[str, Any]) -> str:
    """One update's manifest lines: target and its `replaces:` list. The
    field-level story lives in the suggested commit message, not here."""
    return (f"  {update['target']}\n"
            f"      replaces: {', '.join(update['files'])}")


MKV_AUTO_URL = "https://github.com/MKV-Auto/mkv-auto-release"


def _suggested_commit_message(update: Dict[str, Any]) -> str:
    """The full story of the update: who produced it, which files replace
    upstream's, and every correction with the prior value it replaces."""
    subject = update.get("subject") or "Update"
    bullets: list[str] = []
    for c in update.get("changes") or []:
        if c.startswith("  "):
            bullets.append(f"      {c.lstrip()}")  # continuation under its bullet
        else:
            bullets.append(f"  - {c}")
    lines = [
        subject,
        "",
        f"Update provided by MKV-Auto ({MKV_AUTO_URL})",
        "",
        f"Replacing: {update.get('target')}",
        f"  {', '.join(update.get('files') or [])}",
    ]
    if bullets:
        lines += ["", "Corrections:"] + bullets
    return "\n".join(lines)


def _build_readme(target: str, files: list[str], notes: list[str],
                  update: "Dict[str, Any] | None") -> str:
    if update:
        git_expect = "the files listed below appear as *modified*"
        commit_block = ("Suggested commit message\n"
                        "------------------------\n"
                        f"{_suggested_commit_message(update)}\n\n")
        update_block = (
            "This is an UPDATE\n"
            "-----------------\n"
            "The directory above already exists in the repository; these files\n"
            "replace upstream's copies:\n\n"
            f"{_render_update_entry(update)}\n\n"
            f"{MERGE_WARNING}\n\n"
            f"{commit_block}"
        )
    else:
        git_expect = "only the release directory listed below should appear"
        update_block = ""
    return README.format(
        target=target,
        git_expect=git_expect,
        files="\n".join(f"  {f}" for f in files),
        update_block=update_block,
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


def _without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nulls(v) for v in value]
    return value


def _csharp_escapes(s: str) -> str:
    """Re-escape a dumped JSON text the way System.Text.Json does.

    Upstream's committed files write apostrophes as ``\\u0027`` and embedded
    quotes as ``\\u0022`` (the .NET default encoder); Python writes ``'`` and
    ``\\"``. Matching them keeps every *unchanged* string out of an update's
    diff. Only the two escapes actually observed in their files are applied.
    """
    out: list[str] = []
    in_str = False
    i = 0
    while i < len(s):
        c = s[i]
        if not in_str:
            if c == '"':
                in_str = True
            out.append(c)
            i += 1
            continue
        if c == "\\":
            nxt = s[i + 1]
            out.append("\\u0022" if nxt == '"' else c + nxt)
            i += 2
            continue
        if c == "'":
            out.append("\\u0027")
            i += 1
            continue
        if c == '"':
            in_str = False
        out.append(c)
        i += 1
    return "".join(out)


def _dump_json(obj: Any) -> str:
    """Serialize for the zip in upstream's committed form.

    TheDiscDB's repo files omit optional fields entirely rather than writing
    nulls, so a null-valued key here reads as diff noise on every title — and
    on an update, hundreds of lines of it drown the actual correction. They
    also end without a trailing newline.
    """
    return _csharp_escapes(json.dumps(_without_nulls(obj), indent=2))


def _fetch_upstream_disc_json(target: str, stem: str) -> Optional[Dict[str, Any]]:
    """Upstream's committed copy of the disc file an update replaces.

    Best-effort: any failure just means the update ships our data alone. Only
    the canonical GitHub data repo (or a fork of it) has a raw endpoint we can
    derive; anything else configured via THEDISCDB_REPO is skipped.
    """
    from core.utils import get_discdb_repo_branch, get_discdb_repo_url

    m = re.match(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", get_discdb_repo_url())
    if not m:
        return None
    url = (
        f"https://raw.githubusercontent.com/{m.group(1)}/"
        f"{get_discdb_repo_branch()}/{target}/{stem}.json"
    )
    try:
        import requests

        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.info("discdb export: could not fetch upstream disc json %s: %s", url, exc)
        return None


def _merge_title_onto_upstream(ours: Dict[str, Any], theirs: Any) -> Dict[str, Any]:
    if not isinstance(theirs, dict):
        return ours
    merged = {**theirs, **ours}
    # Comment is the MakeMKV output filename — environment data that differs
    # between any two rips without upstream being wrong. Never "correct" it;
    # ours only fills in where upstream has none.
    if theirs.get("Comment"):
        merged["Comment"] = theirs["Comment"]
    if not ours.get("Tracks") and theirs.get("Tracks"):
        merged["Tracks"] = theirs["Tracks"]
    our_item, their_item = ours.get("Item"), theirs.get("Item")
    if isinstance(our_item, dict) and isinstance(their_item, dict):
        item = {**their_item, **our_item}
        if not our_item.get("Chapters") and their_item.get("Chapters"):
            item["Chapters"] = their_item["Chapters"]
        merged["Item"] = item
    return merged


def _merge_update_onto_upstream(ours: Dict[str, Any], theirs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Start from upstream's committed file and lay our data over it.

    An update must read as a correction, not a rewrite: values upstream has
    that our record cannot produce — chapter names, per-title descriptions,
    fields we don't track — survive, and everything we do emit wins. Titles
    pair up by ``Index``. Without their copy this is the identity function.
    """
    if not isinstance(theirs, dict):
        return ours
    merged = {**theirs, **ours}
    theirs_by_index = {
        t.get("Index"): t for t in theirs.get("Titles") or [] if isinstance(t, dict)
    }
    if isinstance(ours.get("Titles"), list):
        merged["Titles"] = [
            _merge_title_onto_upstream(t, theirs_by_index.get(t.get("Index")))
            if isinstance(t, dict) else t
            for t in ours["Titles"]
        ]
    return merged


# MakeMKV names chapters "Chapter 1", "Chapter 2", … when the disc carries no
# real names. Upstream's committed files write `"Chapters": []` in that case.
_GENERIC_CHAPTER = re.compile(r"^Chapter \d+$")


def _upstream_title_form(title: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in title.items() if k not in ("ChapterCount", "Content")}
    item = out.get("Item")
    if isinstance(item, dict):
        if (item.get("Type") or "").strip().lower() == "ignore":
            # "ignore" is MKV-Auto vocabulary. Upstream's form for a title that
            # is not part of the release content is no Item at all.
            out.pop("Item", None)
        else:
            chapters = item.get("Chapters")
            if isinstance(chapters, list) and chapters and all(
                isinstance(c, dict) and _GENERIC_CHAPTER.match(str(c.get("Title") or ""))
                for c in chapters
            ):
                out["Item"] = {**item, "Chapters": []}
    return out


def _upstream_disc_form(disc_json: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a disc JSON the way upstream's committed files are shaped.

    Verified against TheDiscDb/data itself: no committed file carries
    ``ChapterCount``, ``Content``, an ``Item`` on an unwanted title, or
    placeholder chapter names — those are all internal to our pipeline and
    would read as noise (or as invented data) in a pull request.
    """
    out = dict(disc_json)
    titles = out.get("Titles")
    if isinstance(titles, list):
        out["Titles"] = [
            _upstream_title_form(t) if isinstance(t, dict) else t for t in titles
        ]
    return out


def _align_disc_json_with_upstream(disc_json: Dict[str, Any], coords: Dict[str, Any]) -> Dict[str, Any]:
    """An update must not clobber upstream's identity fields with local ones.

    ``Index`` is the disc's position within *their* release — ours reflects the
    local library and can differ. ``GlobalDiscId`` exists only on the physical
    disc, so a record scanned before AACS-ID capture has none; dropping the key
    would delete the value upstream already has.
    """
    aligned = dict(disc_json)
    if coords.get("disc_index") is not None:
        aligned["Index"] = int(coords["disc_index"])
    gdid = coords.get("global_disc_id")
    if gdid and not aligned.get("GlobalDiscId"):
        # Rebuild to keep the key where upstream files carry it: after ContentHash.
        rebuilt: Dict[str, Any] = {}
        for key, value in aligned.items():
            if key == "GlobalDiscId":
                continue
            rebuilt[key] = value
            if key == "ContentHash":
                rebuilt["GlobalDiscId"] = gdid
        if "GlobalDiscId" not in rebuilt:
            rebuilt["GlobalDiscId"] = gdid
        aligned = rebuilt
    return aligned


def _write_disc_entry(zf: zipfile.ZipFile, bundle: Dict[str, Any], job_id: str, db: Any,
                      written: set) -> Tuple[str, list, list, "Dict[str, Any] | None"]:
    """Write one disc's submission files into an open zip.

    Shared by the single-disc and export-all paths so the two can never drift
    into producing differently-shaped entries. ``written`` carries paths already
    in the archive across calls: discs of the same release land in one directory
    and would otherwise re-fetch and re-write the same cover art per disc.

    Returns ``(target_dir, files_written, notes, update_info)`` — the last
    is None for a new entry, or ``{target, files, subject, changes}`` for
    an update (the material for the README manifest and the UI).
    """
    release_json: Dict[str, Any] = bundle["release"]
    disc_json: Dict[str, Any] = bundle["disc"]
    disc_number = bundle.get("disc_number") or 1
    stem = f"disc{int(disc_number):02d}"

    files: list = []
    notes: list = []

    # #753: a hit is an UPDATE and must land on TheDiscDB's own path — their
    # film directory, their release slug, their disc stem — so the overlay
    # shows as modified files, not a duplicate sibling release. Coordinates
    # come from scan time; for hits scanned before capture existed, try a live
    # lookup once.
    coords = bundle.get("discdb_upstream")
    if bundle.get("is_discdb_hit") and not coords:
        coords = _resolve_upstream_coords(bundle.get("content_hash"))
    is_update = bool(coords)

    if is_update:
        target = upstream_dir(
            coords.get("film_title"),
            coords.get("film_year"),
            bundle.get("release_type"),
            coords.get("release_slug") or "release",
        )
        stem = f"disc{int(coords.get('disc_index') or disc_number):02d}"
        disc_json = _align_disc_json_with_upstream(disc_json, coords)
        # No note here: the README renders updates as their own section, with
        # a `replaces:` line naming these files.
    else:
        if bundle.get("is_discdb_hit"):
            notes.append(
                "This disc is already in TheDiscDB but its exact upstream location "
                "could not be determined, so it is exported as a new release "
                "directory. Check for an existing entry before opening the PR — "
                "if you find one, this directory duplicates it: copy these disc "
                "files over theirs and delete this new directory, rather than "
                "submitting both."
            )
        target = upstream_dir(
            bundle.get("film_title") or release_json.get("Title"),
            bundle.get("film_year") or release_json.get("Year"),
            bundle.get("release_type"),
            bundle.get("release_slug") or "release",
        )

    def write(rel: str, data: bytes | str) -> None:
        path = f"{target}/{rel}"
        if path in written:
            return
        zf.writestr(path, data.encode("utf-8") if isinstance(data, str) else data)
        written.add(path)
        files.append(rel)

    # An update corrects DISC data. release.json and the cover art belong to
    # upstream's entry — overwriting theirs with our thinner copies would
    # regress their repo, so updates ship only the disc files.
    update_info: Optional[Dict[str, Any]] = None
    if not is_update:
        write("release.json", _dump_json(release_json))
    disc_json = _upstream_disc_form(disc_json)
    if is_update:
        theirs = _fetch_upstream_disc_json(target, stem)
        disc_json = _merge_update_onto_upstream(disc_json, theirs)
        changes = _update_change_summary(disc_json, theirs) if theirs else []
        if theirs is not None and not changes:
            # Nothing left after the merge differs from upstream's committed
            # file — an "update" here would replace their disc files with
            # byte-equivalent data plus a fresh log: churn, not a correction.
            # Signalled explicitly: an empty files list alone also happens
            # when a sibling disc already wrote this release's shared files.
            notes.append(
                f"{target}/{stem} already matches TheDiscDB's current entry — "
                "nothing to submit for this disc."
            )
            return target, files, notes, {"target": target, "files": [],
                                          "matched_upstream": True}
        update_info = {
            "target": target,
            "files": files,  # the write() closure appends into this list
            # target is "data/{kind}/{Film (Year)}/{release-slug}".
            "subject": f"Update {target.split('/', 2)[-1]} {stem}",
            "changes": changes,
        }
    write(f"{stem}.json", _dump_json(disc_json))
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
    # Skipped for updates: the upstream release already has its art.
    for label, url_key in (() if is_update else (("front", "ImageUrl"), ("back", "BackImageUrl"))):
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

    # #754: every upstream film directory carries film-level files beside its
    # release dirs. Emit metadata.json + cover.jpg for NEW films only — an
    # update targets a film that already has them upstream, and overwriting
    # theirs from our thinner data would be a regression in their repo.
    if not is_update:
        _write_film_level_files(zf, bundle, target, written, files, notes)

    # #755: sets are an index upstream — boxset.json + art under data/sets/,
    # with the disc files under each member film (exactly where this entry
    # just wrote them). Membership refs come complete from our database.
    # Skipped for updates: a hit inside a collection means the set is very
    # likely already upstream, and our thinner boxset.json must not clobber it.
    if bundle.get("boxset") and not is_update:
        _write_boxset_files(zf, bundle["boxset"], written, files, notes)

    return target, files, notes, update_info


def _write_film_level_files(zf: zipfile.ZipFile, bundle: Dict[str, Any], target: str,
                            written: set, files: list, notes: list) -> None:
    """`metadata.json` + `cover.jpg` in the film directory (the parent of the
    release dir). Deduped via ``written`` — sibling releases share a film."""
    film_dir = target.rsplit("/", 1)[0]
    kind = film_dir.split("/")[1] if film_dir.count("/") >= 1 else "movie"

    meta_path = f"{film_dir}/metadata.json"
    if meta_path not in written:
        title = bundle.get("film_title") or bundle["release"].get("Title")
        year = bundle.get("film_year")
        slug = _film_slug(title, year)
        metadata = {
            "Title": title,
            "FullTitle": f"{title} ({year})" if year else title,
            "SortTitle": title,
            "Slug": slug,
            "Type": "Series" if kind == "series" else "Movie",
            "Year": year,
            # Mirrors upstream's site-path convention ("Movie/thor-2011/cover.jpg").
            "ImageUrl": f"{'Series' if kind == 'series' else 'Movie'}/{slug}/cover.jpg",
        }
        if bundle.get("film_tmdb_id"):
            metadata["ExternalIds"] = {"Tmdb": str(bundle["film_tmdb_id"])}
        zf.writestr(meta_path, _dump_json(metadata))
        written.add(meta_path)
        files.append("../metadata.json")
        notes.append(
            "metadata.json is generated from your library data. tmdb.json and "
            "imdb.json are not — upstream entries carry them, but they look like "
            "maintainer tooling output; mention it in your PR if asked."
        )

    cover_path = f"{film_dir}/cover.jpg"
    if cover_path not in written:
        url = bundle.get("film_cover_url")
        art = _fetch_image(url) if url else None
        if art:
            zf.writestr(cover_path, art)
            written.add(cover_path)
            files.append("../cover.jpg")
        else:
            notes.append(
                "Film-level cover.jpg is not included"
                + (f" — save it from {url} yourself." if url else
                   " — your library has no film cover URL for this title.")
            )


def _write_boxset_files(zf: zipfile.ZipFile, box: Dict[str, Any],
                        written: set, files: list, notes: list) -> None:
    """`data/sets/{Set (Year)}/boxset.json` + art, in upstream's reference
    shape: metadata plus ``Discs[]`` pointers at member films — never disc
    files, which live under each member film's own directory."""
    name = box.get("name") or "Boxset"
    set_dir = f"data/sets/{_sanitize_dir(name)}"
    if box.get("year"):
        set_dir = f"data/sets/{_sanitize_dir(name)} ({box['year']})"

    box_path = f"{set_dir}/boxset.json"
    if box_path in written:
        return

    slug = box.get("slug") or _film_slug(name, box.get("year"))
    boxset_json = {
        "Slug": slug,
        "Asin": box.get("asin"),
        "Upc": box.get("upc"),
        "Year": box.get("year"),
        "Locale": box.get("locale"),
        "RegionCode": box.get("region_code"),
        "Title": name,
        "SortTitle": box.get("sort_title") or name,
        "Type": "Movie",
        "ImageUrl": f"boxset/{slug}.jpg",
        "ReleaseDate": box.get("release_date"),
        "Discs": box.get("disc_refs") or [],
    }
    zf.writestr(box_path, _dump_json(boxset_json))
    written.add(box_path)
    files.append(f"{set_dir}/boxset.json")
    notes.append(
        f"{set_dir}/boxset.json is the set's reference file — its Discs[] point at "
        "the member films' directories, where the disc files themselves live."
    )

    for label, key in (("front", "cover_front_url"), ("back", "cover_back_url")):
        art_path = f"{set_dir}/{label}.jpg"
        if art_path in written:
            continue
        url = box.get(key)
        art = _fetch_image(url) if url else None
        if art:
            zf.writestr(art_path, art)
            written.add(art_path)
            files.append(f"{set_dir}/{label}.jpg")


def _film_slug(title: Any, year: Any) -> str:
    """Upstream's film slug: kebab title plus year — `predators-2010`."""
    base = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-") or "unknown"
    return f"{base}-{year}" if year else base


def _resolve_upstream_coords(content_hash: "str | None") -> "dict | None":
    """Live fallback for hits scanned before coordinate capture existed.

    Asks TheDiscDB where it keeps this content hash. Never fatal: offline just
    means the update exports the old way, with a README warning.
    """
    if not content_hash:
        return None
    try:
        from core.disc_manager import extract_upstream_coords
        from core.utils import retrieve_discdb_data

        raw = retrieve_discdb_data(content_hash)
        return extract_upstream_coords(raw, content_hash)
    except Exception as exc:  # noqa: BLE001 - network is optional here
        logger.info("Could not resolve upstream coords for %s: %s", content_hash, exc)
        return None


def build_discdb_zip(job_id: str, db: Any) -> Tuple[str, bytes]:
    """Return ``(filename, zip_bytes)`` for a single job's disc.

    Reuses :func:`core.discdb_finalize.generate_discdb_bundle` for the data so
    the zip and the JSON response can never disagree about content.
    """
    from core.discdb_finalize import generate_discdb_bundle

    bundle = generate_discdb_bundle(str(job_id), db)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        target, files, notes, update_info = _write_disc_entry(zf, bundle, str(job_id), db, set())
        if update_info and update_info.get("matched_upstream"):
            update_info = None  # README-only zip; the note explains why
        zf.writestr("README.txt", _build_readme(target, files, notes, update_info))

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
  3. Delete this README.txt — it is instructions for you, not part of the
     submission — then check `git status`: new entries appear as untracked
     directories under `data/`, updates as *modified* files.
  4. Commit, push, and open a pull request.

Upstream reviews these by hand. A pull request adding many releases at once is
harder to review than a few; if you have a large library, consider splitting it
across several PRs by removing directories from your clone before committing.
{new_entries}{updates}{skipped}{notes}
Generated by MKV-Auto on {generated}.
"""


def _eligible_contribution_discs(db: Any, disc_ids: "list[str] | None" = None) -> list:
    """Discs worth submitting: finished job, labelled, and new-or-corrected.

    ``job_status == "completed"`` is the gate rather than ``rip_state``: that is
    what "Finish" sets, and finishing is only offered once the whole workflow —
    rip, post-processing, transfer — is done. Exporting mid-workflow would
    submit data that is still moving.

    A disc already in TheDiscDB (``discdb_disc_num`` set — only ever written on
    a match) is normally excluded, since re-submitting duplicates upstream. The
    exception is a **dirty hit**: ``user_edited_at`` is stamped only by the
    human edit paths, so a hit carrying it means the user corrected data that
    upstream got wrong — exactly the case worth submitting as an update.

    A scoped request (explicit ``disc_ids``) skips the hit exclusion entirely:
    a human picked those discs, and "upstream is stale in ways we cannot
    detect" is a judgement the detection must not override. The finished-job
    and release-link rules still always apply.
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
    from sqlalchemy import or_

    q = (
        db.query(db_models.Disc)
        .filter(
            has_finished_job,
            db_models.Disc.release_id.isnot(None),
        )
    )
    if disc_ids is not None:
        # Explicit human selection: hits allowed, job/release rules still apply.
        q = q.filter(db_models.Disc.id.in_(disc_ids))
    else:
        # Automatic set: misses, plus hits the user has since corrected.
        q = q.filter(
            or_(
                db_models.Disc.discdb_disc_num.is_(None),
                db_models.Disc.user_edited_at.isnot(None),
            )
        )
    return q.all()


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
    disc_ids: "list[str] | None" = None,
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

    discs = _eligible_contribution_discs(db, disc_ids=disc_ids)
    total = len(discs)
    written: set = set()
    new_entries: list = []
    update_entries: list = []  # (target, files) — rendered with a `replaces:` line
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
                target, files, notes, update_info = _write_disc_entry(zf, bundle, job_id, db, written)
            except Exception as exc:
                logger.warning("discdb bulk export: skipping disc %s: %s", disc.id, exc)
                skipped.append(f"{label} — {exc}")
                continue
            if update_info and update_info.get("matched_upstream"):
                # Nothing differed from upstream's committed file — nothing
                # was written, so nothing was exported or stamped.
                skipped.append(f"{label} — already matches TheDiscDB's current entry")
                all_notes.extend(f"{target}: {n}" for n in notes)
                continue
            if update_info:
                update_entries.append(update_info)
            else:
                # files can be empty when a sibling disc already wrote this
                # release's shared directory — still an included disc.
                new_entries.append(f"{target}  ({', '.join(files)})" if files else target)
            all_notes.extend(f"{target}: {n}" for n in notes)
            exported_ids.append(str(disc.id))
        included_count = len(new_entries) + len(update_entries)
        if progress is not None:
            progress(included_count + len(skipped), total, "")

        zf.writestr("README.txt", BULK_README.format(
            count=included_count,
            plural="" if included_count == 1 else "s",
            new_entries=("\nNew entries\n-----------\n"
                         + "\n".join(f"  {i}" for i in new_entries) + "\n")
                        if new_entries else
                        ("" if update_entries else
                         "\nNew entries\n-----------\n  (nothing eligible)\n"),
            updates=("\nUpdates — these files REPLACE upstream's copies\n"
                     "-----------------------------------------------\n"
                     + "\n".join(_render_update_entry(u) for u in update_entries) + "\n\n"
                     + MERGE_WARNING + "\n\n"
                     + "Suggested commit message" + ("s" if len(update_entries) > 1 else "")
                     + "\n------------------------\n"
                     + "\n\n---\n\n".join(_suggested_commit_message(u)
                                          for u in update_entries) + "\n")
                    if update_entries else "",
            skipped=("\nSkipped\n-------\n" + "\n".join(f"  {s}" for s in skipped) + "\n")
                    if skipped else "",
            notes=("\nNotes\n-----\n" + "\n".join(f"  - {n}" for n in all_notes) + "\n")
                  if all_notes else "",
            generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        ))

    if dest is not None:
        sink.close()

    summary = {
        "included": included_count,
        # For the UI: which entries overwrite upstream files, and what changed
        # — the same material the README's update section is rendered from.
        "updates": update_entries,
        "skipped": len(skipped),
        "skipped_detail": skipped,
        "disc_ids": exported_ids,
        "total": total,
        "cancelled": cancelled,
    }
    payload = None if dest is not None else sink.getvalue()
    return "thediscdb-submissions.zip", payload, summary
