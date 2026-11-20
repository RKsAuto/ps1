"""Clock abstraction so timeouts, retries and callbacks are testable deterministically."""
from __future__ import annotations

import time


class Clock:
    def now(self) -> float:
        return time.time()


class FakeClock(Clock):
    """Manually advanced clock used by tests and the scripted demo."""

    def __init__(self, start: float = 1_767_225_600.0):  # 2026-01-01T00:00:00Z
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds
