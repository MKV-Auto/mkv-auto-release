"""
Tests for title ordering logic with title_id keys.
_build_title_output_map pairs title_keys (DB order) to ripped paths sorted by MakeMKV _tNN index.
"""
import pytest
import uuid
from unittest.mock import Mock
from sqlalchemy.orm import Session

from core.makemkv_output import sort_makemkv_mkv_filenames
from workers.tasks import _build_title_output_map


@pytest.fixture
def mock_disc_titles():
    """Create mock DiscTitle objects with title_id, source_file, and order_index."""
    titles = []
    for i in range(5):
        title = Mock()
        title.id = str(uuid.uuid4())
        title.source_file = f"0010{i}.mpls"
        title.index = i + 1
        title.order_index = i + 1
        titles.append(title)
    return titles


@pytest.fixture
def mock_db(mock_disc_titles):
    """Create mock database session."""
    db = Mock(spec=Session)
    mock_query = Mock()
    mock_query.filter.return_value.all.return_value = mock_disc_titles
    db.query.return_value = mock_query
    return db


def _makemkv_paths_for_titles(titles: list) -> dict[str, str]:
    """Simulate MakeMKV output names in title list order (t00, t01, ...)."""
    return {str(t.id): f"Disc_t{i:02d}.mkv" for i, t in enumerate(titles)}


class TestTitleOrdering:
    """Test title ordering with title_id keys."""

    def test_build_title_output_map_preserves_order(self, mock_disc_titles):
        title_keys = [str(title.id) for title in mock_disc_titles]
        ripped_files = _makemkv_paths_for_titles(mock_disc_titles)

        result = _build_title_output_map(title_keys, ripped_files)

        assert len(result) == len(mock_disc_titles)
        result_keys = list(result.keys())
        assert result_keys == title_keys

        sorted_paths = sort_makemkv_mkv_filenames(list(ripped_files.values()))
        for i, title_id in enumerate(title_keys):
            assert result[title_id] == sorted_paths[i]

    def test_title_ordering_with_missing_titles(self, mock_disc_titles):
        title_keys = [str(title.id) for title in mock_disc_titles]
        ripped_files = _makemkv_paths_for_titles(mock_disc_titles[:3])

        result = _build_title_output_map(title_keys, ripped_files)

        assert len(result) == 3
        sorted_paths = sort_makemkv_mkv_filenames(list(ripped_files.values()))
        for i, title_id in enumerate(title_keys[:3]):
            assert title_id in result
            assert result[title_id] == sorted_paths[i]

    def test_title_ordering_with_extra_titles(self, mock_disc_titles):
        title_keys = [str(title.id) for title in mock_disc_titles[:3]]
        ripped_files = _makemkv_paths_for_titles(mock_disc_titles)
        extra_id = str(uuid.uuid4())
        ripped_files[extra_id] = "extra_no_pattern.mkv"

        result = _build_title_output_map(title_keys, ripped_files)

        assert len(result) == 3
        assert extra_id not in result

    def test_title_ordering_with_different_order_index(self, mock_disc_titles):
        mock_disc_titles[0].order_index = 5
        mock_disc_titles[1].order_index = 2
        mock_disc_titles[2].order_index = 4
        mock_disc_titles[3].order_index = 1
        mock_disc_titles[4].order_index = 3

        sorted_titles = sorted(mock_disc_titles, key=lambda t: t.order_index)
        title_keys = [str(title.id) for title in sorted_titles]
        # Paths follow MakeMKV index order for titles as ordered on disc
        ripped_files = _makemkv_paths_for_titles(sorted_titles)

        result = _build_title_output_map(title_keys, ripped_files)

        result_keys = list(result.keys())
        assert result_keys == title_keys

        sorted_paths = sort_makemkv_mkv_filenames(list(ripped_files.values()))
        for i, title in enumerate(sorted_titles):
            title_id = str(title.id)
            assert result[title_id] == sorted_paths[i]

    def test_title_ordering_fallback_to_index(self, mock_disc_titles):
        mock_disc_titles[0].order_index = None
        mock_disc_titles[1].order_index = 2
        mock_disc_titles[2].order_index = None
        mock_disc_titles[3].order_index = 1
        mock_disc_titles[4].order_index = 3

        def sort_key(title):
            return title.order_index if title.order_index is not None else (
                title.index if title.index is not None else 9999
            )

        sorted_titles = sorted(mock_disc_titles, key=sort_key)
        title_keys = [str(title.id) for title in sorted_titles]
        ripped_files = _makemkv_paths_for_titles(sorted_titles)

        result = _build_title_output_map(title_keys, ripped_files)

        assert len(result) == len(mock_disc_titles)
        result_keys = list(result.keys())
        assert result_keys == title_keys


class TestBuildTitleOutputMapSelectiveRip:
    """Selective rip (Path A): file list is sparse — only the ripped indices
    appear — so the legacy positional zip mis-aligned rows. The disc_titles
    path matches by parsing `_tNN` from the filename, which is correct."""

    def _titles(self, *idx_id_pairs):
        out = []
        for idx, tid in idx_id_pairs:
            t = Mock()
            t.id = tid
            t.index = idx
            t.order_index = idx
            t.source_file = f"{idx:05d}.mpls"
            out.append(t)
        return out

    def test_disc_titles_path_matches_each_mkv_to_its_index(self):
        # Midway-shaped: 222 titles on disc, 4 of them ripped (indices 10, 88,
        # 90, 109). The MKVs are named after the MakeMKV title index.
        titles = self._titles(
            *[(i, f"uuid-{i}") for i in [0, 10, 88, 90, 109]]
        )
        title_keys = [t.id for t in titles]
        ripped = {
            "uuid-X": "Midway_t10.mkv",
            "uuid-Y": "Midway_t88.mkv",
            "uuid-Z": "Midway_t90.mkv",
            "uuid-W": "Midway_t109.mkv",
        }
        result = _build_title_output_map(title_keys, ripped, disc_titles=titles)
        # Each MKV ends up on the title row whose index matches its `_tNN`.
        assert result == {
            "uuid-10": "Midway_t10.mkv",
            "uuid-88": "Midway_t88.mkv",
            "uuid-90": "Midway_t90.mkv",
            "uuid-109": "Midway_t109.mkv",
        }

    def test_falls_back_to_positional_when_disc_titles_missing(self):
        titles = self._titles((0, "uuid-0"), (10, "uuid-10"))
        title_keys = [t.id for t in titles]
        ripped = {"a": "D_t0.mkv", "b": "D_t10.mkv"}
        # No disc_titles passed → legacy positional zip (sorted by _tNN):
        # uuid-0 → D_t0.mkv, uuid-10 → D_t10.mkv (here it happens to be right).
        result = _build_title_output_map(title_keys, ripped, disc_titles=None)
        assert result == {"uuid-0": "D_t0.mkv", "uuid-10": "D_t10.mkv"}

    def test_returns_empty_when_no_final_paths(self):
        assert _build_title_output_map(["uuid-0"], {}, disc_titles=[]) == {}
