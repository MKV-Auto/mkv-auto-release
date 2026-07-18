"""Shared types for the E2E fixture catalog.

A fixture is the minimum amount of data needed to make MockDrive + MockMKV
behave like a specific real-or-synthetic disc plus any per-fixture settings
overrides (e.g. ``discdb_disabled`` for the miss scenario, or a bad
``DISKDBURL`` for the network-error scenario).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Fixture:
    """A loadable E2E disc profile.

    Attributes
    ----------
    name
        Short identifier (matches the module name).
    discinfo_payload
        Dict matching MockDrive's ``discinfo_payload`` shape:
        ``disc_num``, ``mount_point``, ``disc_hash``, ``content_hash``,
        ``info_title``, ``format`` / ``disc_format``, ``resolution``,
        ``title_type``, ``tracks``, ``titles``, ``info_log`` / ``raw_info_log``.
    mockmkv_titles
        List of ``{"file": "00001.mpls", ...}`` dicts forwarded to ``MockMKV``.
    discdb_disabled
        If True, set ``settings.discdb_disabled`` so disc identification raises
        the ``"Dev mode: DiscDB disabled (simulated miss)"`` exception path —
        the canonical way to force MISS workflow without depending on
        ``content_hash`` being absent from TheDiscDB.
    discdb_url_override
        If set, override ``core.utils.DISKDBURL`` with the given URL before
        any disc lookup runs. Used by the ``discdb_error`` fixture to point
        at an invalid endpoint and exercise the network-failure path.
    expected_workflow
        One of ``"hit"`` (DiscDB hit → skip label → postprocess),
        ``"miss"`` (DiscDB miss → label → postprocess). Used by specs to
        assert end-state.
    notes
        Free-form description for human readers.
    """

    name: str
    discinfo_payload: Dict[str, Any]
    mockmkv_titles: List[Dict[str, Any]]
    discdb_disabled: bool = False
    discdb_url_override: Optional[str] = None
    expected_workflow: str = "miss"
    notes: str = ""

    def __post_init__(self) -> None:
        if self.expected_workflow not in ("hit", "miss"):
            raise ValueError(
                f"Fixture {self.name!r}: expected_workflow must be 'hit' or 'miss', "
                f"got {self.expected_workflow!r}"
            )
        if not self.discinfo_payload.get("content_hash"):
            raise ValueError(
                f"Fixture {self.name!r}: discinfo_payload requires content_hash"
            )


def make_simple_payload(
    *,
    content_hash: str,
    info_title: str,
    disc_format: str = "Blu-Ray",
    resolution: str = "1080p",
    title_type: str = "movie",
    mpls: str = "00001.mpls",
    season: str = "1",
    episode: str = "1",
    episode_name: str = "Pilot",
    info_log: Optional[str] = None,
    discdb_result: Optional[str] = None,
    label_required: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build a minimal MockDrive ``discinfo_payload`` for simple movie/episode
    fixtures. Use direct construction for richer multi-title catalogs.

    Pass ``discdb_result`` and/or ``label_required`` to short-circuit the
    hit/miss decision in ``crud.create_job_for_disc``: that path sets
    ``stage_profile`` from these payload fields without re-running disc
    identification, so they're the authoritative way to force MISS or HIT
    in a synthetic fixture.
    """
    if info_log is None:
        info_log = (
            f'TINFO:0,0,0,"{info_title}"\n'
            f'SINFO:0,0,19,0,"1920x1080"\n'
        )
    payload: Dict[str, Any] = {
        "disc_num": "1",
        "mount_point": "/dev/sr0",
        "disc_hash": content_hash,
        "content_hash": content_hash,
        "info_title": info_title,
        "format": disc_format,
        "disc_format": disc_format,
        "show_title": info_title,
        "show_image": None,
        "resolution": resolution,
        "title_type": title_type,
        "tracks": {
            mpls: {
                "season": season,
                "episode": episode,
                "episode_name": episode_name,
                "format": "MainFeature",
            },
        },
        "titles": {
            mpls: {
                "file": mpls,
                "title": info_title,
                "description": "Main Feature",
            },
        },
        # scan_tracks are what crud._apply_scan_tracks reads to create
        # DiscTitle rows with the MakeMKV numeric index. Without ``index`` set,
        # rip_verification can't map MockMKV's ``test_t1.mkv`` output back to
        # a title_id via ``core.makemkv_output.map_mkv_filenames_to_title_ids``,
        # and the MISS path fails with "Incomplete rip: 0/1 titles".
        # Index 1 matches MockMKV's ``test_t1.mkv`` naming for the first title.
        "scan_tracks": [
            {
                "source_file": mpls,
                "index": 1,
                "title": info_title,
                "description": "Main Feature",
                "type": "movie" if title_type == "movie" else None,
            },
        ],
        "info_log": info_log,
        "raw_info_log": info_log,
        # _hydrated:True keeps hydrate_disc_payload from re-parsing the minimal
        # test info_log and overwriting our explicit scan_tracks / titles.
        # Without this, parse_log emits placeholder scan_tracks (source_file
        # like "title-0", index=0) that wipe the fixture's index hints.
        "_hydrated": True,
    }
    if discdb_result is not None:
        payload["discdb_result"] = discdb_result
    if label_required is not None:
        payload["label_required"] = label_required
    return payload
