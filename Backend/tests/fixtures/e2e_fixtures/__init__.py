"""E2E fixture catalog — selectable disc profiles for full-stack tests.

Each fixture module exposes a ``FIXTURE: Fixture`` constant describing the
MakeMKV + drive state plus any settings overrides required to reproduce a
specific scenario (DiscDB miss, hit, network error, heavy obfuscation, ...).

Selected via ``E2E_FIXTURE=<name>`` at backend startup; see
``Backend/scripts/e2e_bootstrap.py``.

Catalog (see individual modules for detail):
- ``miss``         — synthetic disc, ``discdb_disabled=True`` forces miss path
- ``hit_movie``    — real movie disc with known TheDiscDB entry (Phase B)
- ``hit_show``     — real TV show disc with known TheDiscDB entry (Phase B)
- ``midway``       — heavily obfuscated 4K UHD disc (Phase B)
- ``discdb_error`` — same payload as ``hit_movie`` but DiscDB endpoint
                     pointed at an invalid URL to simulate network error
                     (Phase B)
"""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._base import Fixture

__all__ = ["load_fixture"]


def load_fixture(name: str) -> "Fixture":
    """Resolve ``name`` to the matching fixture module and return its FIXTURE.

    Raises NotImplementedError for stub fixtures that have not been populated
    with real disc data yet (Phase B work).
    """
    mod = import_module(f"{__name__}.{name}")
    fixture = getattr(mod, "FIXTURE", None)
    if fixture is None:
        raise RuntimeError(
            f"e2e_fixtures.{name} does not export a FIXTURE constant"
        )
    return fixture
