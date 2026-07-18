"""
Simple in-memory flag to short-circuit drive scans when MakeMKV is unavailable
(e.g., expired or not installed). The flag is cleared after a successful update.
"""
from typing import Optional

_disabled_reason: Optional[str] = None


def is_disabled() -> bool:
    return _disabled_reason is not None


def get_reason() -> Optional[str]:
    return _disabled_reason


def set_disabled(reason: str) -> None:
    global _disabled_reason
    _disabled_reason = reason


def clear_disabled() -> None:
    global _disabled_reason
    _disabled_reason = None
