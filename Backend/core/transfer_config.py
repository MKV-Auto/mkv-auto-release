"""
Compatibility shim for legacy imports.
"""
from core.transfer import config as _config
from core.transfer.config import *  # noqa: F401,F403

_CONFIG_FILE = _config._CONFIG_FILE
