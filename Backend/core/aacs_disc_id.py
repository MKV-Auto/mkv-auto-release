"""Compute TheDiscDB's ``GlobalDiscId`` — the AACS disc ID.

``SHA1`` of the disc's ``AACS/Unit_Key_RO.inf``, uppercase hex, no prefix. The
same 40-hex value libbluray/``bd_info`` reports and that ``keydb.cfg`` keys on.

The file is **unencrypted** — computing this needs no AACS keys, only the bytes.
It sits beside ``BDMV`` at the root of a mounted Blu-ray or UHD, so a read-only
mount is all that is required.

Availability is the reason every caller must tolerate ``None``:

- **DVDs have no AACS directory.** Their equivalent is computed over the IFO
  structures and is a different, harder algorithm; upstream deferred it.
- **It cannot be derived from a rip.** The AACS directory never enters an MKV, so
  the only source is the physical disc in a drive.

Upstream treats the field as optional and *add-only* — once set it is immutable —
which is why a wrong value would be worse than no value, and why nothing here
guesses or falls back.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Real ones are ~10-100 KB. The cap stops a malformed or hostile filesystem from
# pulling an arbitrarily large read into memory.
_MAX_BYTES = 4 * 1024 * 1024


def compute_from_mount(mount_path: str | Path) -> Optional[str]:
    """AACS disc ID for a mounted disc root, or ``None`` if unavailable.

    ``mount_path`` is the directory holding ``BDMV`` — the same path
    :func:`core.segment_reorder._mounted_disc` yields.
    """
    unit_key = Path(mount_path) / "AACS" / "Unit_Key_RO.inf"
    try:
        if not unit_key.is_file():
            # Normal for a DVD, and for a Blu-ray that mounted without AACS.
            logger.debug("No AACS/Unit_Key_RO.inf under %s", mount_path)
            return None
        size = unit_key.stat().st_size
        if size == 0 or size > _MAX_BYTES:
            logger.warning(
                "Ignoring implausible Unit_Key_RO.inf at %s (%d bytes)", unit_key, size
            )
            return None
        return hashlib.sha1(unit_key.read_bytes()).hexdigest().upper()
    except OSError as exc:
        # A disc that is scratched, ejected mid-read, or mounted read-broken.
        logger.info("Could not read %s: %s", unit_key, exc)
        return None


def compute_from_device(device_path: str) -> Optional[str]:
    """AACS disc ID for a disc in ``/dev/srN``, mounting it read-only.

    Never raises: this runs inside disc scanning, where failing to compute an
    optional identifier must not take down the scan itself.

    Refuses anything that is not a block device before shelling out. Mounting
    costs an external command with no timeout of its own, and this sits in the
    scan path — so a bad or absent device must cost nothing rather than however
    long ``mount`` decides to take. It also keeps unit tests that drive the scan
    with a placeholder path off the real mount machinery entirely.
    """
    try:
        if not _is_block_device(device_path):
            logger.debug("Not a block device, skipping AACS disc ID: %s", device_path)
            return None

        from core.segment_reorder import _mounted_disc

        with _mounted_disc(device_path) as mount_path:
            return compute_from_mount(mount_path)
    except Exception as exc:
        logger.info("Could not compute AACS disc ID for %s: %s", device_path, exc)
        return None


def _is_block_device(device_path: str) -> bool:
    try:
        return stat.S_ISBLK(os.stat(device_path).st_mode)
    except OSError:
        return False
