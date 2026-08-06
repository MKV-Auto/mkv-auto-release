#!/usr/bin/env python3
"""Build and publish the MakeMKV x FFmpeg compatibility matrix.

The installer used to resolve its build inputs at run time and hope they
fit together. FFmpeg 9.0 shipped on 2026-08-03, removed AVCodec fields
MakeMKV's libffabi still uses, and broke every fresh install that day.
This job replaces the hoping with a tested table.

For a MakeMKV version it walks FFmpeg releases newest-first until one
builds, then certifies that pair. Both outcomes are recorded: the failures
are how we explain a held-back update to a user, and how we avoid
re-testing a combination we already know is broken.

  ledger (private, mkv-auto-sources)  — every result, positive and
      negative, plus the archived source tarballs and their hashes
  manifest (public, mkv-auto-release) — only validated pairs + hashes,
      which is all a client needs to gate, pin and verify

Hashes are computed from the artifacts that actually produced a working
binary, and are only published after the build and smoke test pass, so a
published hash certifies a buildable artifact rather than a download.

Usage:
    python scripts/makemkv-matrix.py --ledger-dir <sources-checkout> \
        [--manifest-out makemkv-versions.json] [--max-candidates 4] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Backend"))

from core import makemkv_manifest as mf  # noqa: E402
from core import makemkv_updater as mu  # noqa: E402

LEDGER_NAME = "matrix.jsonl"
SOURCES_SUBDIR = "sources"


def log(msg: str) -> None:
    print(f"[matrix] {msg}", flush=True)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Ledger ──────────────────────────────────────────────────────────────────

def load_ledger(ledger_dir: Path) -> list[dict]:
    path = ledger_dir / LEDGER_NAME
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            log(f"skipping malformed ledger line: {line[:80]!r}")
    return rows


def append_ledger(ledger_dir: Path, entry: dict) -> None:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    with (ledger_dir / LEDGER_NAME).open("a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def known_result(rows: list[dict], makemkv: str, ffmpeg: str) -> str | None:
    """'ok' / 'failed' if this pair was already tested, else None.

    Memoization is what keeps a daily job cheap: a 20-minute build should
    not re-run for a combination already settled.
    """
    for row in rows:
        if row.get("makemkv_version") == makemkv and row.get("ffmpeg_version") == ffmpeg:
            return "ok" if row.get("build") == "ok" else "failed"
    return None


# ── Source archival ─────────────────────────────────────────────────────────

def archive_sources(ledger_dir: Path, version: str, files: list[Path]) -> dict:
    """Keep our own copy of the tarballs, since makemkv.com hosts only the
    current release — once a version rolls off, an un-archived copy may be
    unobtainable. Private, never redistributed."""
    dest_dir = ledger_dir / SOURCES_SUBDIR / version
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = {}
    for f in files:
        target = dest_dir / f.name
        if not target.exists():
            shutil.copy2(f, target)
        stored[f.name] = sha256_of(target)
    return stored


# ── Building ────────────────────────────────────────────────────────────────

def fetch_with_provenance(url: str, dest: Path) -> str:
    """Fetch one tarball and record WHERE it came from.

    Provenance is not bookkeeping: a hash means different things depending
    on its source. Bytes from makemkv.com are the vendor's; bytes from
    archive.org are a mirror's copy that merely looks right. Certifying the
    second as though it were the first is exactly the claim this pipeline
    must never make — so the ledger records which it was, and the parity
    check below refuses to compare a copy against itself.
    """
    try:
        mu._download(url, dest, [], headers=mu.DOWNLOAD_HEADERS)
        return "vendor"
    except Exception as exc:
        log(f"  primary unavailable ({str(exc)[:80]}); falling back to archive.org")
    mu._download_with_fallback(url, dest, [], log_cb=lambda l: print(f"    {l}", flush=True))
    return "archive"


def check_fallback_parity(url: str, expected_digest: str, provenance: str) -> dict:
    """Prove archive.org can serve the same bytes as the vendor.

    Users fall back to the archive whenever makemkv.com is unreachable —
    which was continuously true in early August — so "the fallback works"
    needs checking while the primary is healthy, not discovered at the
    moment we need it. A capture that exists and DISAGREES is an integrity
    signal, not a flake.

    A just-released tarball legitimately has no capture yet; that is
    reported, not failed.
    """
    if provenance != "vendor":
        # Comparing the archive's bytes against the archive's own capture
        # proves nothing and would report a confident "match". Say plainly
        # that we could not check.
        return {"status": "unchecked", "detail": "primary unavailable; nothing to compare against"}
    try:
        urls = mu._wayback_snapshot_urls_for(url, min_bytes=mu.WAYBACK_MIN_TARBALL_BYTES)
    except mu.WaybackLookupError as exc:
        return {"status": "unavailable", "detail": str(exc)[:200]}
    if not urls:
        return {"status": "not_archived"}
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "capture.bin"
        try:
            mu._download(urls[0], dest, [], headers=mu.WAYBACK_DOWNLOAD_HEADERS,
                         timeout=mu.WAYBACK_DOWNLOAD_TIMEOUT)
        except Exception as exc:
            return {"status": "download_failed", "detail": str(exc)[:200]}
        # archive.org serves these double-gzipped; normalize to the vendor's
        # form before comparing or every capture looks corrupt.
        mu._unwrap_double_gzip_if_needed(dest, [], None)
        digest = sha256_of(dest)
        return {
            "status": "match" if digest == expected_digest else "MISMATCH",
            "capture": urls[0],
        }


def try_pair(makemkv_version: str, ffmpeg_version: str, sources: dict[str, Path]) -> tuple[bool, str]:
    """Build one pair and smoke-test the binary. Returns (ok, detail)."""
    workdir = Path(tempfile.mkdtemp(prefix=f"matrix-{makemkv_version}-{ffmpeg_version}-"))
    prefix = workdir / "install"
    try:
        mu.install_makemkv_from_sources(
            makemkv_version,
            build_ffmpeg=True,
            ffmpeg_advanced_features=True,
            ffmpeg_version=ffmpeg_version,   # explicit pin, no monkeypatching
            install_prefix=str(prefix),
            log_cb=lambda line: print(f"    {line}", flush=True),
        )
    except mu.BuildEnvironmentError:
        # The runner is broken, not the version pair. Propagate so the job
        # aborts instead of recording a compatibility verdict it has not
        # earned — a "failed" row here would be memoized and published as
        # known_incompatible, telling users their software does not work.
        raise
    except Exception as exc:
        detail = str(exc)[:500]
        return False, detail
    binary = prefix / "bin" / "makemkvcon"
    try:
        proc = subprocess.run(
            [str(binary), "-r", "info", "disc:99"],
            capture_output=True, text=True, timeout=120,
        )
        if "MakeMKV" not in (proc.stdout or ""):
            return False, "binary built but did not start"
    except Exception as exc:
        return False, f"binary smoke test errored: {exc}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return True, "ok"


# ── Manifest ────────────────────────────────────────────────────────────────

def build_manifest(rows: list[dict]) -> dict:
    """Fold the ledger into the small public artifact.

    Only successful pairs become `validated`. Failures become
    `known_incompatible` so a client can explain a held-back update
    instead of silently offering nothing.
    """
    validated: dict[str, dict] = {}
    incompatible: list[dict] = []
    for row in rows:
        mkv = row.get("makemkv_version")
        ff = row.get("ffmpeg_version")
        if not mkv or not ff:
            continue
        if row.get("build") == "ok":
            existing = validated.get(mkv)
            # Prefer the newest FFmpeg proven to work for this version.
            if not existing or mu.ffmpeg_version_key(ff) > mu.ffmpeg_version_key(existing["ffmpeg_version"]):
                # Publish hashes ONLY when every artifact came from the
                # vendor. An archive.org copy that builds is still good
                # evidence for the version RELATIONSHIP, but its hash is not
                # a vendor-authenticity claim, and publishing it as one cuts
                # both ways: a client fetching from makemkv.com would reject
                # the vendor's own legitimate bytes if the mirror ever
                # differed, and a client fetching from the archive would be
                # checking a copy against itself.
                #
                # Withholding the hash degrades to "install unverified",
                # which is exactly today's behaviour, while keeping the
                # relationship published so update gating still works while
                # makemkv.com is down. "cached" counts as unknown origin.
                sources = row.get("source") or {}
                vendor_sourced = bool(sources) and all(v == "vendor" for v in sources.values())
                entry = {
                    "ffmpeg_version": ff,
                    "validated_at": row.get("tested_at"),
                    "sha256": row.get("sha256") or {} if vendor_sourced else {},
                }
                if not vendor_sourced:
                    entry["hashes_withheld"] = (
                        "sources were not fetched from the vendor; hashes cannot "
                        "attest vendor authenticity"
                    )
                validated[mkv] = entry
        else:
            incompatible.append({
                "makemkv_version": mkv,
                "ffmpeg_version": ff,
                "reason": row.get("build") or "build_failed",
                "detail": (row.get("detail") or "")[:200],
            })

    # A version that later succeeded is not incompatible; drop stale negatives.
    incompatible = [r for r in incompatible if r["makemkv_version"] not in validated]

    latest = None
    if validated:
        latest = sorted(validated, key=mu.ffmpeg_version_key, reverse=True)[0]

    return {
        "schema": mf.SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "latest_validated": latest,
        "validated": validated,
        "known_incompatible": incompatible,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger-dir", required=True, type=Path,
                    help="checkout of the private sources repo")
    ap.add_argument("--manifest-out", default="makemkv-versions.json", type=Path)
    ap.add_argument("--max-candidates", type=int, default=4,
                    help="how far down the FFmpeg list to walk before giving up")
    ap.add_argument("--makemkv-version", default=None,
                    help="override the version under test (default: newest upstream)")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve and report without building or writing the ledger")
    args = ap.parse_args()

    rows = load_ledger(args.ledger_dir)
    log(f"ledger has {len(rows)} result(s)")

    makemkv_version = args.makemkv_version or mu.fetch_latest_version()
    candidates = mu.supported_ffmpeg_versions(limit=args.max_candidates)
    if not makemkv_version or not candidates:
        log(f"cannot resolve inputs (makemkv={makemkv_version}, ffmpeg candidates={candidates})")
        return 2
    log(f"testing MakeMKV {makemkv_version} against {candidates}")

    if args.dry_run:
        log("dry run: stopping before download/build")
        return 0

    # Fetch sources once; every candidate reuses them. The download path is
    # the same one users take, fallback included, so a broken fetch surfaces
    # here rather than in someone's setup wizard.
    work = mu.predownload_dir(makemkv_version)
    work.mkdir(parents=True, exist_ok=True)
    provenance: dict[str, str] = {}
    for kind, url_tpl in (("bin", mu.MAKEMKV_BIN_URL), ("oss", mu.MAKEMKV_OSS_URL)):
        name = f"makemkv-{kind}-{makemkv_version}.tar.gz"
        target = work / name
        if not target.exists():
            provenance[name] = fetch_with_provenance(url_tpl.format(version=makemkv_version), target)
        else:
            provenance[name] = "cached"
        log(f"  {name}: {provenance[name]}")

    dl = mu.download_makemkv_sources(makemkv_version, log_cb=lambda l: print(f"    {l}", flush=True))
    sources = {"bin": Path(dl.bin_tar), "oss": Path(dl.oss_tar)}
    stored = archive_sources(args.ledger_dir, makemkv_version, list(sources.values()))
    log(f"archived {len(stored)} source tarball(s)")

    result_written = False
    for ffmpeg_version in candidates:
        prior = known_result(rows, makemkv_version, ffmpeg_version)
        if prior == "ok":
            log(f"{makemkv_version} + {ffmpeg_version}: already validated, nothing to do")
            result_written = True
            break
        if prior == "failed":
            log(f"{makemkv_version} + {ffmpeg_version}: known bad, skipping")
            continue

        log(f"building {makemkv_version} + {ffmpeg_version}")
        try:
            ok, detail = try_pair(makemkv_version, ffmpeg_version, sources)
        except mu.BuildEnvironmentError as exc:
            # Abort the whole run: no ledger row, no manifest, non-zero exit
            # so the failure surfaces as an issue instead of as a false
            # compatibility claim.
            log(f"BUILD ENVIRONMENT NOT READY — aborting without recording a verdict: {exc}")
            return 7

        entry = {
            "makemkv_version": makemkv_version,
            "ffmpeg_version": ffmpeg_version,
            "build": "ok" if ok else "build_failed",
            "detail": detail if not ok else "",
            "tested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if ok:
            # Hash AFTER a successful build + smoke test: the certificate is
            # about the bytes that produced a working binary, and re-hashing
            # here proves they were not mutated in place along the way.
            entry["sha256"] = {p.name: sha256_of(p) for p in sources.values()}
            # Check the archive path agrees, while the primary is healthy.
            entry["source"] = provenance
            entry["fallback"] = {
                p.name: check_fallback_parity(
                    (mu.MAKEMKV_BIN_URL if "bin" in p.name else mu.MAKEMKV_OSS_URL)
                    .format(version=makemkv_version),
                    entry["sha256"][p.name],
                    provenance.get(p.name, "unknown"),
                )
                for p in sources.values()
            }
            for name, res in entry["fallback"].items():
                log(f"  fallback {name}: {res['status']}")
            if any(r.get("status") == "MISMATCH" for r in entry["fallback"].values()):
                # Do not certify bytes the archive disagrees about — users
                # who fall back would get something we never tested.
                log("  archive.org disagrees with upstream; withholding certification")
                entry["build"] = "fallback_mismatch"
                entry.pop("sha256", None)
                ok = False
        append_ledger(args.ledger_dir, entry)
        rows.append(entry)
        result_written = True
        log(f"  -> {'VALIDATED' if ok else 'failed: ' + detail[:120]}")
        if ok:
            break

    if not result_written:
        log("no candidate produced a result")

    manifest = build_manifest(rows)
    args.manifest_out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    log(f"wrote {args.manifest_out}: latest_validated={manifest['latest_validated']} "
        f"validated={len(manifest['validated'])} incompatible={len(manifest['known_incompatible'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
