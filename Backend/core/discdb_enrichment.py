"""
DiscDB metadata overlay onto local MakeMKV scan rows.

Only metadata fields from TheDiscDB are merged. MakeMKV-owned structural and rip-facing
fields (e.g. source_file, comment, segment_map, duration, streams, chapters) must never
be overwritten from DiscDB payloads.
"""
from __future__ import annotations

import logging

from core.title_type_normalize import normalize_title_type_for_storage as _canonical_title_type

log = logging.getLogger(__name__)

# DiscDB keys we may copy onto a local title dict. Explicitly excludes `comment` (MakeMKV).
_DISCDB_METADATA_KEYS = ("type", "season", "episode", "title", "description", "episode_name")


def merge_discdb_enrichment_into_titles(
    titles: list[dict],
    discdb_tracks: dict[str, dict] | None,
    *,
    content_hash: str | None = None,
    strip_discdb_ignore_type: bool = False,
) -> list[dict]:
    """
    Enrich local scan titles with DiscDB metadata.

    Local scan is canonical for structure (index, source_file, segment_map, streams, size,
    duration, chapters). DiscDB is enrichment-only. ``comment`` is never read from DiscDB.

    When ``strip_discdb_ignore_type`` is True (DiscDB prefill + full labeling), do not copy
    DiscDB ``type`` when it resolves to ``ignore`` (including missing/blank type, matching
    DB overlay semantics).
    """
    if not titles or not discdb_tracks:
        return titles or []

    by_source: dict[str, dict] = {}
    by_basename: dict[str, dict] = {}

    def _norm_source(val: str | None) -> str | None:
        if not val:
            return None
        return str(val).strip()

    def _basename(val: str | None) -> str | None:
        if not val:
            return None
        s = str(val).replace("\\", "/")
        return s.rsplit("/", 1)[-1]

    for t in titles:
        src = _norm_source(t.get("source_file") or t.get("track_id") or t.get("title_id"))
        if src:
            by_source[src] = t
            base = _basename(src)
            if base:
                by_basename.setdefault(base, t)

    def _find_local_for_discdb(sf: str | None) -> dict | None:
        if not sf:
            return None
        sf_norm = _norm_source(sf)
        if not sf_norm:
            return None
        if sf_norm in by_source:
            return by_source[sf_norm]
        base = _basename(sf_norm)
        if base and base in by_basename:
            return by_basename[base]
        return None

    unmatched: list[tuple[str | None, dict]] = []

    enriched: list[dict] = []
    for t in titles:
        enriched.append(dict(t))

    enrichment_by_id: dict[int, dict] = {}
    for sf, payload in (discdb_tracks or {}).items():
        local = _find_local_for_discdb(sf)
        if not local:
            unmatched.append((sf, payload))
            continue
        pl = dict(payload) if isinstance(payload, dict) else {}
        pl.pop("index", None)
        pl.pop("order", None)
        pl.pop("Order", None)
        enrichment_by_id[id(local)] = pl

    for idx, t in enumerate(enriched):
        payload = enrichment_by_id.get(id(titles[idx]))
        if not payload:
            continue
        for key in _DISCDB_METADATA_KEYS:
            if key not in payload or payload[key] is None:
                continue
            if key == "type" and strip_discdb_ignore_type:
                raw_tv = payload.get("type")
                if raw_tv is None or (isinstance(raw_tv, str) and not raw_tv.strip()):
                    resolved_tv = "ignore"
                else:
                    resolved_tv = raw_tv
                if _canonical_title_type(resolved_tv) == "ignore":
                    continue
            t[key] = payload[key]

    if unmatched:
        try:
            sample = [sf for sf, _ in unmatched[:5]]
            log.warning(
                "DiscDB enrichment: %d tracks had no matching local title (disc_hash=%s, samples=%r)",
                len(unmatched),
                (content_hash or "")[:12] if content_hash else None,
                sample,
            )
        except Exception:
            pass

    return enriched
