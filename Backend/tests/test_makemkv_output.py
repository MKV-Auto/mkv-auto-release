"""Tests for MakeMKV output filename ordering (_tNN.mkv)."""

from types import SimpleNamespace

from core.makemkv_output import (
    makemkv_mkv_rel_path_sort_key,
    makemkv_output_title_index,
    map_mkv_filenames_to_title_ids,
    sort_makemkv_mkv_filenames,
)


def test_makemkv_output_title_index():
    assert makemkv_output_title_index("Star Wars_t108.mkv") == 108
    assert makemkv_output_title_index("sub/Star Wars_t02.mkv") == 2
    assert makemkv_output_title_index("no_pattern.mkv") is None


def test_sort_makemkv_mkv_filenames_numeric_not_lexicographic():
    names = [
        "D_t100.mkv",
        "D_t11.mkv",
        "D_t2.mkv",
        "D_t09.mkv",
    ]
    assert sort_makemkv_mkv_filenames(list(names)) == [
        "D_t2.mkv",
        "D_t09.mkv",
        "D_t11.mkv",
        "D_t100.mkv",
    ]


def test_makemkv_mkv_rel_path_sort_key_matches_basename():
    assert makemkv_mkv_rel_path_sort_key("raw/D_t9.mkv") < makemkv_mkv_rel_path_sort_key("raw/D_t100.mkv")


class TestMapMkvFilenamesToTitleIds:
    def _titles(self, *index_id_pairs):
        return [SimpleNamespace(index=idx, id=tid) for idx, tid in index_id_pairs]

    def test_maps_each_filename_to_title_with_matching_index(self):
        titles = self._titles((0, "uuid-0"), (10, "uuid-10"), (109, "uuid-109"))
        out = map_mkv_filenames_to_title_ids(
            ["Midway_t10.mkv", "Midway_t109.mkv", "Midway_t00.mkv"],
            titles,
        )
        assert out == {
            "uuid-0": "Midway_t00.mkv",
            "uuid-10": "Midway_t10.mkv",
            "uuid-109": "Midway_t109.mkv",
        }

    def test_handles_selective_rip_with_sparse_indices(self):
        """Selective rip emits MKVs only for the indices in rip_set;
        the disc has many more titles than were ripped. The mapping
        must hit the exact `index` match — never positional."""
        titles = self._titles(*[(i, f"uuid-{i}") for i in range(0, 222)])
        rip_outputs = [
            "Midway_t10.mkv", "Midway_t88.mkv", "Midway_t90.mkv",
            "Midway_t109.mkv",
        ] + [f"Midway_t{i}.mkv" for i in range(204, 222)]
        out = map_mkv_filenames_to_title_ids(rip_outputs, titles)
        # 22 entries each pointing to the title with the matching index
        assert len(out) == 22
        assert out["uuid-109"] == "Midway_t109.mkv"
        assert out["uuid-88"] == "Midway_t88.mkv"
        assert out["uuid-10"] == "Midway_t10.mkv"
        # Sanity: the wrong (positional) mapping would have linked
        # uuid-1 → Midway_t10.mkv, uuid-2 → Midway_t88.mkv, etc.
        assert "uuid-1" not in out
        assert "uuid-2" not in out

    def test_skips_filenames_without_tNN_pattern(self):
        titles = self._titles((0, "uuid-0"))
        assert map_mkv_filenames_to_title_ids(["bonus.mkv", "no_pattern.mkv"], titles) == {}

    def test_skips_indices_with_no_matching_title(self):
        titles = self._titles((5, "uuid-5"))
        # The disc only has a title at index 5; t10's MKV has nowhere to live.
        out = map_mkv_filenames_to_title_ids(["Midway_t5.mkv", "Midway_t10.mkv"], titles)
        assert out == {"uuid-5": "Midway_t5.mkv"}

    def test_handles_rel_paths_and_basenames_alike(self):
        titles = self._titles((7, "uuid-7"))
        out = map_mkv_filenames_to_title_ids(
            ["raw/Midway_t7.mkv", "subdir/Midway_t7.mkv"],
            titles,
        )
        # Last write wins on the same title_id, but either form parses correctly.
        assert "uuid-7" in out
        assert out["uuid-7"].endswith("Midway_t7.mkv")

    def test_skips_titles_with_no_id_or_no_index(self):
        titles = [
            SimpleNamespace(index=None, id="uuid-no-idx"),
            SimpleNamespace(index=10, id=None),
            SimpleNamespace(index=109, id="uuid-109"),
        ]
        out = map_mkv_filenames_to_title_ids(
            ["Midway_t10.mkv", "Midway_t109.mkv"],
            titles,
        )
        assert out == {"uuid-109": "Midway_t109.mkv"}
