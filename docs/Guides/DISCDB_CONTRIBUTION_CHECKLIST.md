# TheDiscDB Contribution Checklist

This checklist helps you contribute disc metadata to [TheDiscDB](https://thediscdb.com) so other MKV-Auto users can get automatic identification for their discs.

## When to contribute

If MKV-Auto shows your disc as a **DiscDB miss** (the disc wasn't found in TheDiscDB), you can contribute the metadata after you've labeled and processed it. This helps the next person who inserts the same disc get an automatic hit.

## What you need

### Required

- [ ] **Content hash** — MKV-Auto computes this automatically when a disc is inserted. It's visible in the disc info panel and stored in job details.
- [ ] **Disc format** — Blu-ray, 4K UHD, DVD, or HD DVD.

### Recommended

- [ ] **Movie or series title** — The correct title of the content.
- [ ] **Number of titles** — How many titles MakeMKV found on the disc.
- [ ] **Release year** — The year this specific release (not the movie) was published.
- [ ] **Release edition** — Edition name if applicable (e.g., Criterion, Arrow, Standard).

### For boxsets / multi-disc releases

- [ ] **Boxset name** — The name of the collection.
- [ ] **Disc number** — Which disc in the set (e.g., 1 of 3).
- [ ] **Content type** — Movie, Series, or Boxset.

## How to find your disc data

1. **Insert your disc** — MKV-Auto scans it automatically.
2. **Check disc info** — The content hash, format, and title count are shown in the disc info panel.
3. **After labeling** — If you manually labeled the disc (DiscDB miss), the movie name, year, and release details are available in the job and library views.

## Generate the bundle

MKV-Auto can build the whole submission for you as a single JSON bundle.

### From the UI

1. Open **Library**, click into the release, and click the disc row to open the disc drawer.
2. Click **⬇ Export DiscDB bundle** (shown for DiscDB-miss discs only — hits came *from* TheDiscDB, so there is nothing new to contribute).
3. The browser downloads `discdb-bundle-<release-slug>-discNN.json`.

### From the API

```bash
curl -s http://localhost:8080/api/discdb/contributions/<disc_id>/bundle -o bundle.json
```

(`GET /discdb/contributions` lists candidate discs with their contribution status.)

### What's inside the bundle

The bundle mirrors the files in a TheDiscDB data-repo entry:

| Bundle key | Maps to upstream file | Contents |
| --- | --- | --- |
| `release` | `release.json` | Release metadata (slug, year, UPC/ASIN, edition) |
| `disc` | `discNN.json` | Per-title data: source files, durations, segment maps, chapters, types |
| `summary` | `discNN-summary.txt` | Human-readable title summary (ignored titles excluded) |
| `content_hash` | — | The unique identifier that matches the disc on insert |

`info_log_included` tells you whether the MakeMKV info log was still available to enrich the title data; `false` means the bundle was built from the database only (still valid, slightly less detail).

### Export tracking

Each successful export stamps the disc with `discdb_contribution_status='exported'` and `discdb_exported_at`, so `GET /discdb/contributions?status=not_submitted` shows only discs that still need attention. After you submit upstream, mark it:

```bash
curl -s -X PATCH http://localhost:8080/api/discdb/contributions/<disc_id> \
  -H 'Content-Type: application/json' -d '{"status": "submitted"}'
```

Statuses: `not_submitted → draft → exported → submitted → accepted/rejected`. Re-exporting never downgrades a `submitted`/`accepted` disc.

## How to submit

[Open an issue](https://github.com/MKV-Auto/mkv-auto-release/issues/new) titled "DiscDB contribution: <release name>" to file a contribution, attaching the bundle (or its parts) from the step above.

## Guidelines

- **Accuracy matters** — Double-check the content hash and title information before submitting.
- **One disc per submission** — If you have a multi-disc boxset, submit each disc separately (reference the boxset name and disc number).
- **Include the content hash** — This is the unique identifier that links your disc to TheDiscDB. Without it, the submission can't be matched.
