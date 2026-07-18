"""
MakeMKV output naming: files are named ``<disc name>_tNN.mkv`` with zero-padded NN.

Lexicographic sort of these names is wrong (``t100`` sorts before ``t11``). Use numeric
ordering on ``NN`` when matching rips to title order.

The ``NN`` in the filename is the **MakeMKV title index** — the same number that
appears in the ``Title #N`` markers and is stored on ``disc_titles.index``. For a
full rip this matches the disc_titles row order naturally (0, 1, 2, …) so a
positional zip of titles-vs-files happens to work. For a **selective rip** (Path A:
the rip set is a small subset of indices), the file list is sparse — only the
ripped indices appear — so a positional zip silently mis-aligns titles to files.
Always match by parsing the index from the filename and looking it up against
``disc_titles.index`` on the same disc.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

_T_MKV_SUFFIX_RE = re.compile(r"_t(\d+)\.mkv$", re.IGNORECASE)


def makemkv_output_title_index(name: str) -> int | None:
    """Return MakeMKV's numeric title index from a basename like ``Foo_t108.mkv``, or None."""
    base = Path(name).name
    m = _T_MKV_SUFFIX_RE.search(base)
    return int(m.group(1)) if m else None


def sort_makemkv_mkv_filenames(filenames: list[str]) -> list[str]:
    """
    Sort filenames by MakeMKV ``_tNN`` numeric index (not lexicographic).
    Names without the pattern sort last, then by basename for stability.
    """
    def key(fn: str) -> tuple[int, str]:
        idx = makemkv_output_title_index(fn)
        n = idx if idx is not None else 1_000_000_000
        return (n, Path(fn).name.lower())

    return sorted(filenames, key=key)


def makemkv_mkv_rel_path_sort_key(rel_path: str) -> tuple[int, str]:
    """Sort key for relative paths whose basename is a MakeMKV ``_tNN.mkv`` output."""
    base = Path(rel_path).name
    idx = makemkv_output_title_index(base)
    n = idx if idx is not None else 1_000_000_000
    return (n, base.lower())


def map_mkv_filenames_to_title_ids(
    mkv_filenames: Iterable[str],
    disc_titles: Iterable[Any],
) -> dict[str, str]:
    """Return ``{title_id: rel_path}`` for the given MakeMKV ``_tNN.mkv`` outputs.

    Parses the ``_tNN`` index from each filename and matches it against
    ``DiscTitle.index`` on the supplied titles. Filenames whose parsed index
    doesn't correspond to any disc title — or that don't carry the ``_tNN``
    pattern at all — are skipped silently. The result is order-independent
    and works correctly for both **full** and **selective** rips.

    Args:
        mkv_filenames: relative paths or basenames of MakeMKV outputs.
        disc_titles: iterable of ``DiscTitle`` rows for the disc; each row
            must expose ``id`` and ``index`` attributes.
    """
    index_to_id: dict[int, str] = {}
    for t in disc_titles:
        idx = getattr(t, "index", None)
        tid = getattr(t, "id", None)
        if isinstance(idx, int) and tid:
            index_to_id[idx] = str(tid)
    out: dict[str, str] = {}
    for rel_path in mkv_filenames:
        if not rel_path:
            continue
        idx = makemkv_output_title_index(rel_path)
        if idx is None:
            continue
        tid = index_to_id.get(idx)
        if tid:
            out[tid] = rel_path
    return out
