"""Future wearable integration boundary; no device credentials are handled."""

from __future__ import annotations

from typing import Protocol

from .state import StatusSnapshot


class HealthAdapter(Protocol):
    def latest(self) -> StatusSnapshot | None:
        """Return a normalized snapshot or None when no device data exists."""
