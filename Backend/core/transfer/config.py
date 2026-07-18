"""
Persisted transfer configuration (mode + destination) stored server-side.
"""
import json
from pathlib import Path
from typing import Literal, TypedDict, Optional
import os

from core.utils import get_mkvauto_data

ConfigMode = Literal["local", "rsync"]

class TransferConfig(TypedDict, total=False):
    mode: ConfigMode
    transfer_dir: str
    output_dir: str


_CONFIG_FILE = Path(get_mkvauto_data()) / "transfer_config.json"


def _default_config() -> TransferConfig:
    env_dir = os.getenv("MAKEMKV_TRANSFER_DIR")
    default_dir = Path(env_dir).expanduser() if env_dir else None
    return {
        "mode": "local",
        # transfer_dir intentionally omitted until user configures it
        "transfer_dir": str(default_dir) if default_dir else None,
        "output_dir": str(get_mkvauto_data()),
    }


def load_transfer_config() -> TransferConfig:
    """
    Load the persisted transfer config; fall back to defaults if missing/invalid.
    """
    cfg = _default_config()
    try:
        if _CONFIG_FILE.exists():
            data = json.loads(_CONFIG_FILE.read_text())
            if isinstance(data, dict):
                mode = data.get("mode")
                if mode in ("local", "rsync"):
                    cfg["mode"] = mode
                if data.get("transfer_dir"):
                    cfg["transfer_dir"] = str(data["transfer_dir"])
                if data.get("output_dir"):
                    cfg["output_dir"] = str(data["output_dir"])
    except Exception:
        # Ignore and keep defaults
        pass
    return cfg


def save_transfer_config(mode: ConfigMode, transfer_dir: Optional[str] = None, output_dir: Optional[str] = None) -> TransferConfig:
    """
    Persist the chosen transfer mode/path server-side.
    """
    cfg: TransferConfig = {
        "mode": mode if mode in ("local", "rsync") else "local",
    }
    if transfer_dir:
        cfg["transfer_dir"] = str(transfer_dir)
    else:
        cfg["transfer_dir"] = load_transfer_config().get("transfer_dir")
    if output_dir:
        cfg["output_dir"] = str(output_dir)
    else:
        cfg["output_dir"] = load_transfer_config().get("output_dir") or str(get_mkvauto_data())

    try:
        _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass
    return cfg
