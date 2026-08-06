#!/usr/bin/env python3
"""Validate the MakeMKV + FFmpeg build inputs and emit a signed-off manifest.

Why this exists
---------------
The installer resolves its own build inputs at run time: the newest MakeMKV
from makemkv.com, the newest FFmpeg under the compatibility ceiling. That
means an upstream release can break a user's first-run setup with no change
on our side — which is exactly what happened on 2026-08-03, when FFmpeg 9.0
removed AVCodec fields MakeMKV's libffabi still uses and every fresh install
started failing mid-compile.

This script is the canary. It resolves the same versions the installer
would, proves they still build together, proves the archive.org fallback
can serve the same bytes as the primary, and writes a manifest recording
what was validated. The manifest ships in the image so the installer can
(a) prefer the version pair CI proved builds and (b) verify that what it
downloaded is what we tested.

We do NOT redistribute the sources. MakeMKV's binary tarball is
proprietary; the manifest carries hashes — facts about the files — not the
files themselves.

Usage:
    python scripts/validate-makemkv-build.py --out manifest.json [--skip-build]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Backend"))

from core import makemkv_updater as mu  # noqa: E402


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def log(msg: str) -> None:
    print(f"[validate] {msg}", flush=True)


def download_primary(url: str, dest: Path) -> None:
    """Fetch from the real upstream, with NO archive fallback.

    The fallback is validated separately and deliberately: if we let it
    engage here, a manifest could record archive.org's bytes as though they
    were upstream's, which is the one thing this file must never do.
    """
    mu._download(url, dest, [], headers=mu.DOWNLOAD_HEADERS)


def validate_fallback(url: str, primary_digest: str) -> dict:
    """Prove the archive.org path can serve the same bytes as upstream.

    Returns a status dict rather than raising: a *just-released* tarball
    legitimately has no capture yet, and that must not fail the canary. A
    capture that exists and DISAGREES with upstream is a different matter —
    that is a genuine integrity signal and is reported as a mismatch.
    """
    try:
        urls = mu._wayback_snapshot_urls_for(url, min_bytes=mu.WAYBACK_MIN_TARBALL_BYTES)
    except mu.WaybackLookupError as exc:
        return {"status": "unavailable", "detail": f"index could not be queried: {exc}"}
    if not urls:
        return {"status": "not_archived", "detail": "no usable capture in the index yet"}

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "fallback.bin"
        try:
            mu._download(
                urls[0], dest, [], headers=mu.WAYBACK_DOWNLOAD_HEADERS,
                timeout=mu.WAYBACK_DOWNLOAD_TIMEOUT,
            )
        except Exception as exc:
            return {"status": "download_failed", "detail": str(exc)[:200], "capture": urls[0]}
        mu._unwrap_double_gzip_if_needed(dest, [], None)
        digest = sha256_of(dest)
        return {
            "status": "match" if digest == primary_digest else "MISMATCH",
            "capture": urls[0],
            "sha256": digest,
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="makemkv-build-manifest.json")
    ap.add_argument("--skip-build", action="store_true",
                    help="resolve + hash + check the fallback, but don't compile (fast path)")
    args = ap.parse_args()

    started = time.time()
    manifest: dict = {
        "validated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ffmpeg_ceiling": ".".join(str(p) for p in mu.FFMPEG_MAX_VERSION_EXCLUSIVE),
    }

    # 1. Resolve exactly what the installer would resolve.
    makemkv_version = mu.fetch_latest_version()
    ffmpeg_version = mu.fetch_latest_ffmpeg()
    if not makemkv_version or not ffmpeg_version:
        # Still emit a manifest: the artifact recording HOW the canary failed
        # is the useful one, and "we could not even resolve versions" is a
        # materially different diagnosis from "the build broke".
        log(f"FAILED to resolve versions (makemkv={makemkv_version} ffmpeg={ffmpeg_version})")
        manifest["result"] = "resolve_failed"
        manifest["makemkv_version"] = makemkv_version
        manifest["ffmpeg_version"] = ffmpeg_version
        Path(args.out).write_text(json.dumps(manifest, indent=2) + "\n")
        return 2
    manifest["makemkv_version"] = makemkv_version
    manifest["ffmpeg_version"] = ffmpeg_version
    log(f"resolved makemkv={makemkv_version} ffmpeg={ffmpeg_version}")

    files = {
        f"makemkv-oss-{makemkv_version}.tar.gz":
            f"https://www.makemkv.com/download/makemkv-oss-{makemkv_version}.tar.gz",
        f"makemkv-bin-{makemkv_version}.tar.gz":
            f"https://www.makemkv.com/download/makemkv-bin-{makemkv_version}.tar.gz",
        f"ffmpeg-{ffmpeg_version}.tar.xz":
            mu.FFMPEG_URL_TEMPLATE.format(version=ffmpeg_version),
    }

    # 2 + 3. Hash upstream bytes, then prove the fallback agrees with them.
    sha: dict[str, str] = {}
    fallback: dict[str, dict] = {}
    workdir = Path(tempfile.mkdtemp(prefix="makemkv-validate-"))
    for name, url in files.items():
        dest = workdir / name
        log(f"downloading {name} from upstream")
        try:
            download_primary(url, dest)
        except Exception as exc:
            log(f"FAILED upstream download of {name}: {exc}")
            manifest["result"] = "upstream_unavailable"
            manifest["error"] = f"{name}: {str(exc)[:300]}"
            Path(args.out).write_text(json.dumps(manifest, indent=2) + "\n")
            return 3
        sha[name] = sha256_of(dest)
        log(f"  sha256 {sha[name]}")
        if name.startswith("makemkv-"):
            # Only MakeMKV tarballs use the archive fallback; ffmpeg.org has
            # been reliable and its releases are mirrored widely.
            fallback[name] = validate_fallback(url, sha[name])
            log(f"  fallback: {fallback[name]['status']}")

    manifest["sha256"] = sha
    manifest["fallback"] = fallback

    mismatches = [n for n, r in fallback.items() if r.get("status") == "MISMATCH"]
    if mismatches:
        # Not a flake: the archive is serving different bytes than upstream
        # for a file we are about to tell users to trust.
        log(f"INTEGRITY MISMATCH between upstream and archive.org for: {mismatches}")
        manifest["result"] = "fallback_mismatch"
        Path(args.out).write_text(json.dumps(manifest, indent=2) + "\n")
        return 4

    # 4. Prove the pair actually compiles together.
    if args.skip_build:
        manifest["result"] = "resolved_only"
        manifest["build"] = "skipped"
    else:
        log("building ffmpeg + makemkv (this takes 10-20 minutes)")
        prefix = workdir / "install"
        try:
            mu.install_makemkv_from_sources(
                makemkv_version,
                build_ffmpeg=True,
                ffmpeg_advanced_features=True,
                install_prefix=str(prefix),
                log_cb=lambda line: print(f"    {line}", flush=True),
            )
        except Exception as exc:
            log(f"BUILD FAILED: {exc}")
            manifest["result"] = "build_failed"
            manifest["error"] = str(exc)[:2000]
            Path(args.out).write_text(json.dumps(manifest, indent=2) + "\n")
            return 5

        # 5. A binary that exists is not the same as a binary that runs.
        binary = prefix / "bin" / "makemkvcon"
        try:
            proc = subprocess.run(
                [str(binary), "-r", "info", "disc:99"],
                capture_output=True, text=True, timeout=120,
            )
            started_ok = "MakeMKV" in (proc.stdout or "")
        except Exception as exc:
            started_ok = False
            log(f"binary smoke test errored: {exc}")
        manifest["binary_starts"] = started_ok
        if not started_ok:
            manifest["result"] = "binary_smoke_failed"
            Path(args.out).write_text(json.dumps(manifest, indent=2) + "\n")
            return 6
        manifest["build"] = "ok"
        manifest["result"] = "validated"

    manifest["duration_s"] = int(time.time() - started)
    Path(args.out).write_text(json.dumps(manifest, indent=2) + "\n")
    log(f"wrote {args.out}: {manifest['result']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
