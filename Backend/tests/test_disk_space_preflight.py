from core.utils import (
    calculate_disc_size_from_titles,
    calculate_required_rip_space_bytes,
    estimate_preview_size_bytes,
)


def test_selective_rip_uses_only_the_titles_it_will_write(monkeypatch):
    """rip_all=False is the selective rip_set loop: it writes just these titles,
    which can legitimately be a fraction of the disc."""
    monkeypatch.setenv("MKVAUTO_PREVIEW_ESTIMATE_MBPS", "0")
    titles = {"1": {"size": "1 GB"}}

    required_bytes = calculate_required_rip_space_bytes(
        titles, disc_size_bytes=5 * 1024**3, buffer_multiplier=1.3, rip_all=False
    )

    assert required_bytes == int(1 * 1024**3 * 1.3)


def test_all_mode_never_estimates_below_the_physical_disc(monkeypatch):
    """`mkv DEV all OUT` writes every title, so partial/missing title sizes must
    not produce an estimate smaller than the disc itself."""
    monkeypatch.setenv("MKVAUTO_PREVIEW_ESTIMATE_MBPS", "0")
    titles = {"1": {"size": "1 GB"}}

    required_bytes = calculate_required_rip_space_bytes(
        titles, disc_size_bytes=5 * 1024**3, buffer_multiplier=1.3
    )

    assert required_bytes == int(5 * 1024**3 * 1.3)


def test_all_mode_counts_duplicate_titles_above_the_physical_disc(monkeypatch):
    """#875 regression, with the real numbers: Revenge of the Sith is a 46 GB
    Blu-ray that lists the feature twice (40.3 GB playlist + 39.7 GB stream),
    so `all` mode wrote 80 GB. The estimate must follow the titles, not the
    46 GB physical size that let this rip fill the volume."""
    monkeypatch.setenv("MKVAUTO_PREVIEW_ESTIMATE_MBPS", "0")
    titles = {
        "10": {"size": "40.3 GB"},   # 00800.mpls, main feature (2 angles)
        "15": {"size": "589.1 MB"},  # 00300.m2ts, bonus
        "16": {"size": "39.7 GB"},   # 00301.m2ts, the SAME feature again
        "17": {"size": "197.4 MB"},  # 00302.m2ts, bonus
    }
    physical_bytes = 46 * 1024**3

    required_bytes = calculate_required_rip_space_bytes(
        titles, disc_size_bytes=physical_bytes, buffer_multiplier=1.3
    )

    titles_sum = calculate_disc_size_from_titles(titles)
    assert titles_sum > physical_bytes  # 81 GB of titles on a 46 GB disc
    assert required_bytes == int(titles_sum * 1.3)
    # The old fallback would have asked for ~60 GB against ~78 GB free and let
    # the 80 GB rip start; the honest estimate is over 100 GB.
    assert required_bytes > int(physical_bytes * 1.3)
    assert required_bytes > 100 * 1024**3


def test_falls_back_to_disc_size_when_titles_have_no_sizes(monkeypatch):
    """The cached scan payload carries {file, flag, obfuscated} and no sizes.
    The physical size is a floor, not an answer — the caller prefers DiscTitle
    rows — but it must still beat estimating nothing."""
    monkeypatch.setenv("MKVAUTO_PREVIEW_ESTIMATE_MBPS", "0")
    sizeless = {"0": {"file": "00142.mpls", "flag": 0, "obfuscated": False}}

    assert calculate_required_rip_space_bytes(
        {}, disc_size_bytes=10 * 1024**3, buffer_multiplier=1.3
    ) == int(10 * 1024**3 * 1.3)
    assert calculate_required_rip_space_bytes(
        sizeless, disc_size_bytes=10 * 1024**3, buffer_multiplier=1.3
    ) == int(10 * 1024**3 * 1.3)


def test_returns_none_when_nothing_is_known(monkeypatch):
    """No title sizes and no physical size: callers must treat this as
    'cannot estimate', not as 'fits'."""
    monkeypatch.setenv("MKVAUTO_PREVIEW_ESTIMATE_MBPS", "0")

    assert calculate_required_rip_space_bytes(None, disc_size_bytes=None) is None
    assert calculate_required_rip_space_bytes({}, disc_size_bytes=0) is None
    assert (
        calculate_required_rip_space_bytes(
            {"0": {"file": "00142.mpls"}}, disc_size_bytes=None, rip_all=False
        )
        is None
    )


def test_estimate_preview_size_includes_title_count():
    titles = {"1": {"size": "1 GB"}, "2": {"size": "1 GB"}}

    preview_bytes = estimate_preview_size_bytes(titles, duration_seconds=60, bitrate_mbps=8)

    assert preview_bytes == 2 * 60 * 1_000_000
