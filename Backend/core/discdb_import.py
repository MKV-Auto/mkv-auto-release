from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from core import utils


@dataclass
class FileHashInfo:
    index: int
    name: str
    creation_time: datetime
    size: int

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "creation_time": self.creation_time.isoformat(),
            "size": self.size,
        }


@dataclass
class DiscHashInfo:
    hash: Optional[str]
    files: List[FileHashInfo]

    def to_dict(self) -> dict:
        return {
            "hash": self.hash,
            "files": [f.to_dict() for f in self.files],
        }


def _collect_files(base: Path, pattern: str | None = None) -> List[Path]:
    if not base.exists():
        return []

    if pattern:
        return sorted(base.glob(pattern), key=lambda p: p.name.lower())
    return sorted(base.iterdir(), key=lambda p: p.name.lower())


def _normalize_mount(mount: str) -> Path:
    if len(mount) == 1 and mount.isalpha():
        return Path(f"{mount}:\\")
    if len(mount) == 2 and mount[1] == ":":
        return Path(f"{mount}\\")
    return Path(mount)


_hash_cache: dict[str, DiscHashInfo] = {}


def hash_media_disc_cached(mount: str) -> Optional[DiscHashInfo]:
    """
    Compute disc hash once per mount and cache the result for this process.
    Uses existing utils.hash_media_disc for hashing; collects file metadata for reference.
    """
    norm = str(_normalize_mount(mount))
    cached = _hash_cache.get(norm)
    if cached:
        return cached

    try:
        content_hash = utils.hash_media_disc(norm)
    except FileNotFoundError:
        return None

    mount_path = Path(norm)
    bluray_stream = mount_path / "BDMV" / "STREAM"
    dvd_video_ts = mount_path / "VIDEO_TS"

    target_dir: Optional[Path] = None
    pattern: Optional[str] = None
    if bluray_stream.exists():
        target_dir = bluray_stream
        pattern = "*.m2ts"
    elif dvd_video_ts.exists():
        target_dir = dvd_video_ts
        pattern = "*"

    files: List[FileHashInfo] = []
    if target_dir:
        file_paths = _collect_files(target_dir, pattern)
        for idx, fp in enumerate(file_paths):
            try:
                stat = fp.stat()
            except FileNotFoundError:
                continue
            files.append(
                FileHashInfo(
                    index=idx,
                    name=fp.name,
                    creation_time=datetime.fromtimestamp(stat.st_ctime),
                    size=stat.st_size,
                )
            )

    info = DiscHashInfo(hash=content_hash, files=files)
    _hash_cache[norm] = info
    return info


def hash_log_file(log_file: Path) -> Optional[DiscHashInfo]:
    """
    Parse hash info embedded in a MakeMKV log (HSH prefix lines).
    """
    if not log_file.exists():
        return None

    files: List[FileHashInfo] = []
    with log_file.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("HSH:"):
                continue
            try:
                _, payload = line.split(":", 1)
                parts = payload.split(",")
                index = int(parts[0])
                name = parts[1]
                creation_time = datetime.fromisoformat(parts[2])
                size = int(parts[3])
                files.append(
                    FileHashInfo(
                        index=index,
                        name=name,
                        creation_time=creation_time,
                        size=size,
                    )
                )
            except Exception:
                continue

    if not files:
        return None

    return DiscHashInfo(hash=_calculate_hash(files), files=files)
