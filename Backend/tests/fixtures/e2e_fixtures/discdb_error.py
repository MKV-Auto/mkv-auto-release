"""discdb_error fixture — same payload as hit_movie, DISKDBURL pointed at an
unreachable endpoint to simulate TheDiscDB outage.

Validates the acceptance criterion from #196: "API down or lookup failure
treated as MISS; pipeline never blocked". Despite the disc's content_hash
being a real entry in TheDiscDB, the lookup must fail at the network layer
and the pipeline must continue along the MISS path.

Reuses hit_movie's FIXTURE so there is exactly one copy of the underlying
real-disc data; regenerate the source by re-running export_e2e_fixture.py
against the source disc.
"""
from __future__ import annotations

from dataclasses import replace

from .hit_movie import FIXTURE as _HIT_MOVIE


# 127.0.0.1:1 — reserved port (TCP/IP RFC 6335) that is guaranteed to refuse
# connections, producing an immediate ECONNREFUSED instead of a slow timeout.
_UNREACHABLE_ENDPOINT = "http://127.0.0.1:1/graphql/"


FIXTURE = replace(
    _HIT_MOVIE,
    name="discdb_error",
    discdb_url_override=_UNREACHABLE_ENDPOINT,
    expected_workflow="miss",
    notes=(
        "Reuses hit_movie's payload (real disc with a real TheDiscDB hash) but "
        "points DISKDBURL at an unreachable endpoint so the lookup fails at "
        "the network layer. The pipeline must treat this as MISS (see #196)."
    ),
)
