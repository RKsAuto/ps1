"""Background worker: delivers provider callbacks, sweeps timeouts and runs due jobs."""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, engine, interval_s: float | None = None):
        self.engine = engine
        self.interval = interval_s or engine.s.worker_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="onboarding-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.engine.tick()
            except Exception:  # never let the loop die
                log.exception("worker tick failed")
            self._stop.wait(self.interval)
