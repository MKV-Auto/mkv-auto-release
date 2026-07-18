"""Tests for the prefer-mpls tiebreaker in pick_primary_duplicate_row.

When a duplicate group contains both a .mpls playlist and its underlying
.m2ts segment file (sharing the same segment_map), we want the .mpls to
win — it's the "intended" playlist and carries chapter markers. This is
the V-for-Vendetta UHD / D&D Honor Among Thieves case.

For disparate-size groups (e.g. main feature vs short extra) the 5%
score window leaves only the dominant winner anyway, so prefer-mpls
doesn't kick in.
"""
from types import SimpleNamespace

from core.duplicate_group_sync import pick_primary_duplicate_row


def _title(
    *,
    id_: str,
    source_file: str,
    size_gb: float,
    audio_score: int = 5,
    chapters_count: int = 16,
    active: bool | None = None,
    order_index: int | None = None,
):
    """Build a DiscTitle-shaped namespace with the fields the picker reads."""
    size_bytes = int(size_gb * (1024**3))
    return SimpleNamespace(
        id=id_,
        source_file=source_file,
        size=size_bytes,
        mkv_size=None,
        active=active,
        order_index=order_index,
        # metadata_scan / streams / chapters get read by _comparative_metrics;
        # we plant the audio_score + chapters_count directly via metadata_scan.
        metadata_scan={
            "audio_score": audio_score,
            "chapters_count": chapters_count,
        },
        streams=None,
        chapters={},
    )


def test_active_true_overrides_score(monkeypatch):
    """Existing behavior: a single active=True wins regardless of score."""
    a = _title(id_="a", source_file="a.m2ts", size_gb=10.0, active=True)
    b = _title(id_="b", source_file="b.mpls", size_gb=20.0)
    assert pick_primary_duplicate_row([a, b]) is a


def test_prefer_mpls_when_sizes_within_5_percent():
    """V-for-Vendetta case: same audio/chapters, sizes within 5%, mpls wins."""
    mpls = _title(id_="mpls", source_file="00800.mpls", size_gb=40.0)
    m2ts = _title(id_="m2ts", source_file="00800.m2ts", size_gb=39.5)
    # m2ts is technically slightly smaller — without the tiebreaker, current
    # scoring would prefer mpls anyway by size, but the 5% bucket means we
    # also win when sizes go the OTHER way.
    assert pick_primary_duplicate_row([mpls, m2ts]) is mpls


def test_prefer_mpls_when_m2ts_is_slightly_larger():
    """Same as above but m2ts is the larger one — mpls should still win."""
    mpls = _title(id_="mpls", source_file="00800.mpls", size_gb=39.0)
    m2ts = _title(id_="m2ts", source_file="00800.m2ts", size_gb=40.0)
    assert pick_primary_duplicate_row([mpls, m2ts]) is mpls


def test_dominant_size_wins_over_mpls_preference():
    """Phase 2 invariant: prefer-mpls does NOT override a clearly larger title.
    Main feature (39 GB) vs short extra (1 GB) — main wins even if extra is mpls."""
    main = _title(id_="main", source_file="00800.m2ts", size_gb=39.0)
    extra = _title(id_="extra", source_file="00100.mpls", size_gb=1.0)
    assert pick_primary_duplicate_row([main, extra]) is main


def test_two_mpls_files_fall_back_to_size_tiebreaker():
    """When prefer-mpls is symmetric (both mpls), pick the larger one."""
    bigger = _title(id_="big", source_file="00539.mpls", size_gb=39.0)
    smaller = _title(id_="small", source_file="00540.mpls", size_gb=38.5)
    # Both mpls so mpls-bias is symmetric; size_int breaks the tie.
    assert pick_primary_duplicate_row([smaller, bigger]) is bigger


def test_two_m2ts_files_fall_back_to_size_tiebreaker():
    bigger = _title(id_="big", source_file="00800.m2ts", size_gb=39.0)
    smaller = _title(id_="small", source_file="00801.m2ts", size_gb=38.5)
    assert pick_primary_duplicate_row([smaller, bigger]) is bigger


def test_single_member_group_returned_as_is():
    only = _title(id_="only", source_file="x.mpls", size_gb=10.0)
    assert pick_primary_duplicate_row([only]) is only


def test_multiple_actives_first_by_order_index_wins():
    """Existing tie-break: when multiple active=True, lowest order_index wins."""
    a = _title(id_="a", source_file="a.m2ts", size_gb=10.0, active=True, order_index=2)
    b = _title(id_="b", source_file="b.mpls", size_gb=20.0, active=True, order_index=1)
    assert pick_primary_duplicate_row([a, b]) is b
