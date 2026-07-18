from core.utils import calculate_required_rip_space_bytes, estimate_preview_size_bytes


def test_calculate_required_rip_space_prefers_titles(monkeypatch):
    monkeypatch.setenv("MKVAUTO_PREVIEW_ESTIMATE_MBPS", "0")
    titles = {"1": {"size": "1 GB"}}

    required_bytes = calculate_required_rip_space_bytes(titles, disc_size_bytes=5 * 1024**3, buffer_multiplier=1.3)

    assert required_bytes == int(1 * 1024**3 * 1.3)


def test_calculate_required_rip_space_falls_back_to_disc_size(monkeypatch):
    monkeypatch.setenv("MKVAUTO_PREVIEW_ESTIMATE_MBPS", "0")

    required_bytes = calculate_required_rip_space_bytes({}, disc_size_bytes=10 * 1024**3, buffer_multiplier=1.3)

    assert required_bytes == int(10 * 1024**3 * 1.3)


def test_estimate_preview_size_includes_title_count():
    titles = {"1": {"size": "1 GB"}, "2": {"size": "1 GB"}}

    preview_bytes = estimate_preview_size_bytes(titles, duration_seconds=60, bitrate_mbps=8)

    assert preview_bytes == 2 * 60 * 1_000_000
