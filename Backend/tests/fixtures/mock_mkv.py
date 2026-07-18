"""
MockMKV: test double for run_makemkv (MakeMKV/makemkvcon boundary).

Use this instead of the real makemkvcon binary. MockMKV implements run_makemkv:
parses args for output dir (mkv ... all <path> or info dev:...), writes MKV
files and optional makemkv_progress.log, invokes line_cb with TCOUNT/PRGV/MSG,
and returns (log_str, pid) where log_str is parse_log-compatible. This allows
real Disc.rip(), parse_log, and rename_outputs to run while avoiding hardware.

When to use: Request the mock_mkv fixture in tests that run the rip path
(rip_disc, Disc.rip) or any code that calls run_makemkv (e.g. load_disc_map,
_fallback_from_makemkv). Pair with mock_drive for drive/disc-info and test_db
for DB. MockMKV supersedes fake_disc/DummyDisc for the rip path: use real Disc
and mock_mkv instead of replacing Disc.

Patch points: core.utils.run_makemkv, core.disc.run_makemkv, api.crud.run_makemkv.
drive_operations and drive_manager use their own import; tests that exercise
those directly patch core._drive_operations.run_makemkv and do not use mock_mkv.

Config:
- titles: list of {"file": "00001.mpls"} (and optional "size_mb") for the
  MSG:3307 log and number of MKV files. Default [{"file": "00001.mpls"}].
- progress: whether to call line_cb with TCOUNT, Title #, PRGV. Default True.
- failures: optional dict, e.g. {"run_makemkv": Exception("...")}.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

_DEFAULT_TITLES = [{"file": "00001.mpls"}]


def _parse_output_dir(args: str) -> Optional[str]:
    """Extract output directory from mkv ... all <path> or backup ... <path>."""
    parts = args.split()
    if "mkv" in args and "all" in parts:
        for i, p in enumerate(parts):
            if p == "all" and i + 1 < len(parts):
                return parts[i + 1]
    if "backup" in parts:
        # backup --decrypt ... dev:... <output_folder>
        for i, p in enumerate(parts):
            if p.startswith("dev:") and i + 1 < len(parts):
                return parts[i + 1]
    return None


def _build_parse_log_output(titles: List[Dict[str, Any]]) -> str:
    """Build a log string that parse_log will accept (MSG:3307 lines)."""
    lines = []
    for i, t in enumerate(titles):
        fn = t.get("file") or f"0000{i+1}.mpls"
        tid = i + 1
        lines.append(f'MSG:3307,0,2,"File {fn} was added as title #{tid}"')
    return "\n".join(lines) if lines else ""


class MockMKV:
    """
    Test double for run_makemkv. Implements the same signature and return type
    (log_str, pid). Writes MKV files when args contain mkv+all or backup;
    for "info" args only returns a parse_log-compatible string. Do not run
    real makemkvcon.
    """

    def __init__(
        self,
        *,
        titles: Optional[List[Dict[str, Any]]] = None,
        progress: bool = True,
        failures: Optional[Dict[str, Exception]] = None,
    ):
        self.titles: List[Dict[str, Any]] = titles if titles is not None else _DEFAULT_TITLES.copy()
        self.progress: bool = progress
        self.failures: Dict[str, Exception] = failures or {}

    def _maybe_raise(self, key: str) -> None:
        exc = self.failures.get(key)
        if exc is not None:
            raise exc

    def run_makemkv(
        self,
        cmd_args: str,
        line_cb: Optional[Any] = None,
        log_path: Optional[Path] = None,
        **kwargs: Any,
    ) -> tuple[str, int | None]:
        """
        Match core.utils.run_makemkv signature. Returns (log_str, pid).
        log_str is parse_log-compatible; pid is 9999 for mkv/backup, None for info.
        """
        self._maybe_raise("run_makemkv")
        log_str = _build_parse_log_output(self.titles)
        is_rip = "mkv" in cmd_args and "all" in cmd_args
        is_backup = "backup" in cmd_args
        output_dir = _parse_output_dir(cmd_args)

        if (is_rip or is_backup) and output_dir:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            for i in range(len(self.titles)):
                name = f"test_t{i+1}.mkv"
                (out / name).write_bytes(b"x" * 1500)  # minimal non‑zero for hashing
            if log_path is not None:
                log_path = Path(log_path)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("PRGV:1,1,1\nTitle #1\n", encoding="utf-8")
            if self.progress and line_cb:
                line_cb("TCOUNT:%d" % len(self.titles))
                for i in range(len(self.titles)):
                    line_cb("Title #%d" % (i + 1))
                    line_cb("PRGV:100,1,1")
            return (log_str, 9999)

        # "info" or other: no MKV files, no pid
        if self.progress and line_cb:
            line_cb("TCOUNT:%d" % len(self.titles))
        if log_path is not None:
            try:
                log_path = Path(log_path)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(log_str, encoding="utf-8")
            except Exception:
                pass
        return (log_str, None)
