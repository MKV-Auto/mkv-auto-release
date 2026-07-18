# MKV-Auto v1.0.0 — First public release

**Cut date:** 2026-06-27
**Image:** `ghcr.io/mkv-auto/mkv-auto-release:1.0.0` (also tagged `:latest`)

This is the first public release of MKV-Auto. The project has been running on a `0.9.0` internal milestone for ~5 months — ~780 commits of in-the-wild iteration have landed before this cut, and the bulk of this document is about what's actually in your hands when you install `:1.0.0`.

## What is MKV-Auto?

MKV-Auto is a self-hosted optical-disc ripping and media-management tool. Insert a Blu-ray, UHD, or DVD; it detects the disc, looks up metadata via TheDiscDB and TMDB, walks you through labeling (movie/series/extras), rips with MakeMKV, post-processes the output, and transfers the finished files to your media server (Plex / Jellyfin) over local or SMB (mount NFS/other network storage into the container and use local). Single-container Docker deploy, Angular frontend, FastAPI backend, Celery workers, embedded PostgreSQL + Redis (or BYO).

## Highlights

### Library redesign

The Library page is the surface where finalized discs live. The disc drawer was the biggest piece of UX debt in 0.9 — rebuilt as **compact title cards** with type-coloured edges, two-mode display/edit rows, and auto-ignored junk titles hidden by default (one-click "show ignored" to reveal). On real discs the drawer now opens in <0.5s instead of 30+s (#530, #600), and the page itself loads in <1s instead of 9+s. Inserting a disc that's already in the Library shows a passive **"Already in Library"** banner below the carousel with an `Open in Library →` link, so you can confirm before re-ripping (#603).

### Multi-drive concurrent ripping (beta)

Stable-identity tracking via `/dev/disk/by-id` serials (#549–#554), per-drive workflow contexts, multi-drive coordinator + Discord alerts on swaps, USB-bandwidth heuristics. **Single-drive users are unaffected.** Multi-drive is marked **beta** because some concurrent-rip edge cases still surface (see [Known limitations](#known-limitations) below).

### Pipeline collapse

The transfer step used to require the user to click twice: once to start post-process, once to start the actual file copy. Now a single CTA on the Ripper transfer step runs both stages back-to-back, with a **single completion notification** at the end (#365, #605). The intermediate "Ready to transfer" toast that previously fired between stages is suppressed when the active TransferConfig is auto-dispatch-capable (local / SMB).

### TheDiscDB v2

TheDiscDB powers disc identification: on insert, the disc hash is looked up against TheDiscDB and a hit prefills the labeling walkthrough. Search results show an emerald "**In Library**" chip on titles you already own (#590), so you can tell at a glance if you've already ripped something that came back from a search. **Lookups are read-only — MKV-Auto never uploads anything to TheDiscDB.** A per-disc contribution export (TheDiscDB bundle format) exists at the API level today; a guided in-app contribution flow is planned for a future release. Your labels persist in your library, so discs you rip now can be contributed retroactively once that ships.

### Settings polish

Multiple Settings tabs were tidied this release cycle:

- **Notifications** — Errors → Action required → Informative ordering; the Informative per-category matrix is hidden behind its master toggle (no more "enabled but all checked" confusion) (#609).
- **TMDB** — API key field now shows the persisted value (parity with MakeMKV registration) (#610).
- **Previews** — input styling unified with the rest of the page (#608).
- **MakeMKV** — registration UI clarified.
- **Transfer history** — KPIs and identity rows are human-readable (#592, #593).
- **Previews slider max** — driven by server CPU count, not browser (#594).
- **`<ui-checkbox>`** primitive replaces native checkboxes throughout for consistent styling (#595).

### Transfer path tracking

Pre-1.0 the Library disc panel rendered `"In transient"` for files that had been successfully transferred to SMB / rsync / NFS destinations — the post-transfer file-path writer worked only on local filesystems (#607). Rewritten to construct per-title destinations deterministically from the post-paths map and the protocol's destination root, no filesystem walking. **Existing installs with stale rows** can run the one-shot back-fill script — see [Upgrade from 0.9.x](#upgrade-from-09x) below.

### Robust stage-state delivery

The "Verifying…" spinner used to strand the UI after the rip-verification phase finished (#604), and the transfer view used to strand after transfer completed (#605). Root cause: the in-process progress emit at the worker level wasn't shipping `rip_state` / `post_state` / `transfer_state` in the payload. Every progress message now carries these so the frontend's local job-status stays current without a page refresh.

### Lots of small wins

- Library page **9.2s → 0.8s** load time (#530, #532).
- Disc drawer **30s → 0.4s** on 300+ title discs (#600).
- Carousel: superseded failed jobs hidden when a newer active job exists for the same disc; spinners only run for live stages.
- Rip notifications: copy + verification messages no longer collide; toasts respect Discord level dedupe.
- Title-editor cleanup: episode title/season/episode inputs hide when TMDB has a usable catalog (#602); Backdrop rows hide irrelevant fields; component clips moved below the description.
- Postprocess: duplicate-group consensus fix (no more silent `Track{tid}` collisions); auto-ignore heals on clean re-runs (#517 / #518).
- Settings import/export: roundtrip pytest + revived dead `BoxsetRelease` reference (#611).
- Dev-only code paths are stripped from the production build and image, and backend dev endpoints are removed from the published source. (A few inert frontend dev components remain in the source tree; they are excluded from production builds.)

A complete commit-by-commit list lives in the [`CHANGELOG.md`](../CHANGELOG.md) under `[1.0.0]`.

## Known limitations

These ship in 1.0.0; we'll address them in a future patch / release.

### Multi-drive: beta caveats

Single-drive users skip this section.

- **#547** — On the second concurrent rip of a Blu-ray, an info/hash scan can serialise and block for ~3 minutes before the second rip actually starts. The rip itself completes correctly; it's the start delay that's the issue.
- **#557, #558** — Concurrent rips can fail outright with MakeMKV `MSG:5010` when both drives are mid-rip on the same SATA / USB bus. The frontend reports it as `drive_unsupported` even though the drive is fine. We'll refine the error classification + add retry/back-off in a patch.
- **#559** — The "Start copy" button is gated globally instead of per-drive, so starting a rip on Drive B can be blocked by Drive A being mid-something.
- **#544, #545, #546** — Smaller items: orphan makemkvcon subprocess on revoked rip, 15s synchronous `/drives/drives` response during MakeMKV contention, and `/discs/current` not filtering by mount_point.

### Settings → Transfer config: conflict resolution

The "On file conflict" setting in Settings → Transfer (overwrite / skip / rename / fail) is **documented but not yet enforced at the actual transfer sites** (#606). All four transfer modes currently overwrite on re-rip. For most users this is what they want (a re-rip should replace the existing file with the improved one), but it's a known UX gap and will land in v1.0.1.

### Test gaps (transparency)

A handful of backend tests are quarantined for pre-existing flakiness (`#416` postprocess-error-handling hang, `#417` resume-postprocess-integration hang, `#373` e2e healthz/readyz race). None affect the runtime behaviour, but if you're running the full pytest suite during development, expect those to be skipped.

## Upgrade from 0.9.x

### 1. Pull the new image

```bash
docker pull ghcr.io/mkv-auto/mkv-auto-release:1.0.0
```

Restart your container with the new image. The entrypoint runs `alembic upgrade head` automatically — no manual migration step required.

### 2. (SMB / rsync / NFS users only) Back-fill historical transfer paths

If you've been running on the SMB / rsync / NFS transfer path, the file-path writer pre-#607 was broken and your Library disc panels show "In transient" with stale `/data/mkvauto/data/jobs/<uuid>/transient/…` paths for files that have actually shipped to your media server. The fixed writer applies to **new** transfers going forward; to heal **existing** rows, run the back-fill script once:

```bash
docker cp scripts/backfill_transfer_file_paths.py mkv-auto:/tmp/
docker exec \
  -e DATABASE_URL='postgresql://mkvauto:changeme@127.0.0.1:5432/discs' \
  mkv-auto /app/venv/bin/python /tmp/backfill_transfer_file_paths.py
```

The script is idempotent. Local-mode users (transfer destination is on the same filesystem as the rip) can skip — the pre-#607 writer worked correctly for local transfers.

### 3. Reload the Library

After upgrade, hard-refresh your Library page. Disc panels should now show `smb://…` / `rsync …` / `nfs://…` paths with an "At destination" pill on transferred titles.

## Reporting issues

[github.com/MKV-Auto/mkv-auto-release/issues](https://github.com/MKV-Auto/mkv-auto-release/issues) — please include your TransferConfig mode (local / SMB), the disc format (DVD / Blu-ray / UHD), and the relevant log lines from `/data/mkvauto/logs/`.

## Acknowledgements

This first release is a single-maintainer project; the path here owes a debt to TheDiscDB's open metadata API, MakeMKV (the actual ripper), and everyone who beta-tested 0.9.x in the field. Thanks for the patience.
