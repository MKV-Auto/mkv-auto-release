# Contributing a disc to TheDiscDB

When MKV-Auto shows a disc as a **DiscDB miss**, the disc is not yet in
[TheDiscDB](https://thediscdb.com). Once you have labelled and ripped it, MKV-Auto
can package everything upstream needs, so the next person who inserts that disc
gets an automatic hit.

Label the disc, rip it, then export — there is no checklist of data to collect
first. A couple of things the export cannot fill in are listed at the end.

## Export the submission

1. Open **Library**, click into the release, then click the disc row to open the
   disc drawer.
2. Click **⬇ Export DiscDB bundle**.

The disc needs a **finished** job — finishing is only offered once ripping,
post-processing and transfer are all done, which is the point at which the disc's
data has stopped changing.

The button appears only for DiscDB-miss discs — a hit came *from* TheDiscDB, so
there is nothing new to contribute.

You get a zip laid out exactly like the upstream repository:

```
data/movie/Cinderella Man (2005)/2025-4k/
    release.json          release metadata: slug, year, UPC/ASIN, edition, cover art
    disc01.json           per-title data: source files, durations, segment maps, chapters
    disc01-summary.txt    human-readable title summary
    disc01.txt            the raw MakeMKV robot-mode log, redacted
    front.jpg             front cover art
    back.jpg              rear cover art, if the release has one
README.txt
```

## Exporting your whole library at once

**Settings → Export & Import → Export DiscDB submissions** builds one zip
containing every disc that qualifies, in the same layout. Unzip it over your fork
and open a single pull request for the set.

It runs in the background with a progress count, since fetching cover art for a
large library takes a while. You can leave the page or reload it — the export
keeps going, and the page picks it back up when you return. Cancelling stops
after the disc in progress. The download starts on its own if you are still on
the page when it finishes.

**If it finished while you were away**, the page offers a **Download last
export** button instead of making you build it again. Archives stay collectable
for six hours.

A disc qualifies when it has a **finished** job, a labelled release, and is not
already in TheDiscDB. Anything left out is listed under "Skipped" in the zip's
`README.txt`, so you can see what did not make it rather than assuming everything
did.

Upstream reviews these by hand. If your library is large, consider splitting it
across a few pull requests by removing directories from your clone before
committing.

## Submit it

1. Fork and clone [TheDiscDb/data](https://github.com/TheDiscDb/data).
2. Unzip the export into the root of your clone. The `data/…` path in the zip
   lines up with the repository, so the files land where they belong.
3. `git status` should show only new files — one release directory for a single
   disc, or one per release if you exported the whole library.
4. Commit, push, open a pull request.

`README.txt` inside the zip repeats these steps and lists anything the export
could not fill in for you.

## What the export cannot do for you

**Rear cover art.** A front cover URL is required to create a release, so
`front.jpg` is included as a matter of course. A rear cover is optional, so
`back.jpg` appears only if you supplied one. Upstream entries usually have both.

If a download fails, the zip's `README.txt` gives you the URL so you can save the
image yourself — re-running the export often works too, since the failure is
usually transient.

**`GlobalDiscId` for discs scanned by an older version.** This is the AACS disc
ID (`SHA1` of `AACS/Unit_Key_RO.inf`). MKV-Auto captures it whenever a disc is
scanned, but it exists only on the physical disc — it is not in your ripped
files — so a disc last scanned before this feature existed has no ID stored.

**Recovering it is just putting the disc back in the drive.** Any scan fills the
gap: the disc is matched by content hash and the ID is written to the existing
record. There is no separate backfill step, and an ID already stored is never
overwritten. If you would rather not re-insert anything, submit without it —
upstream treats the field as optional and add-only, so it can be contributed later
against the same disc.

DVDs never get one: they have no AACS directory, and their equivalent identifier
is a different algorithm that upstream has deferred.

**The MakeMKV log — only if the disc was never scanned by this version.**
`disc01.txt` comes from the job's `makemkv_info.log` when that is still on disk,
and otherwise from the copy the scan keeps on the disc record. The stored copy
outlives job cleanup, so in practice every scanned disc has one. If neither
exists, the zip says so in `README.txt` and re-scanning the disc captures it.

## Privacy

A raw MakeMKV log names your drive's model and serial number and the device path
it was attached to. The export **redacts** all of them, matching what upstream's
own committed logs look like:

```
DRV:2,2,999,12,"***","***","***"
```

The LibreDrive drive ID, any MakeMKV registration key, and your home-directory
name are stripped too. Nothing that identifies you or your hardware reaches the
pull request. Everything upstream actually parses — title lists, source files,
segment maps — is left untouched.

If you want to check before submitting, `disc01.txt` is plain text; read it.

## From the API

```bash
curl -s "http://localhost:8080/api/discdb/contributions/<disc_id>/bundle" -o submission.zip
```

Add `?format=json` for the raw bundle instead of the zip — the same data in one
JSON object, which is easier to inspect but is not a submission.

`GET /discdb/contributions` lists candidate discs with their contribution status.

The library-wide export is a background job, so it takes three calls:

```bash
# Start it; returns {"job_id": ...}
curl -s -X POST http://localhost:8080/api/discdb/contributions/export-all

# Poll until "status" is "completed" (or "failed")
curl -s http://localhost:8080/api/discdb/contributions/export-all/<job_id>

# Then collect it
curl -s http://localhost:8080/api/discdb/contributions/export-all/<job_id>/download \
  -o submissions.zip
```

`GET .../export-all/active` returns a run in progress, or the most recent
finished archive still on disk, which is how the UI reattaches after a reload.
`DELETE .../export-all/<job_id>` cancels a running export.

## Tracking what you have submitted

Each export stamps the disc with `discdb_contribution_status='exported'` and
`discdb_exported_at`, so `GET /discdb/contributions?status=not_submitted` shows
only discs that still need attention. After your PR is open:

```bash
curl -s -X PATCH http://localhost:8080/api/discdb/contributions/<disc_id> \
  -H 'Content-Type: application/json' -d '{"status": "submitted"}'
```

Statuses run `not_submitted → draft → exported → submitted → accepted/rejected`.
Re-exporting never downgrades a disc that is already `submitted` or `accepted`.

## Guidelines

- **A boxset's discs belong together.** Export them all — they share one release
  directory upstream, and a set submitted a disc at a time is harder to review
  than one submitted whole.
- **Check the title labelling first.** The export reflects what you labelled. If a
  title is mislabelled in MKV-Auto, it will be mislabelled in your PR.
- **Do not hand-edit the content hash.** It is how the disc is matched on insert;
  a wrong value makes the entry unreachable.
