import random

import pytest

from onboarding.clock import FakeClock
from onboarding.config import Settings
from onboarding.db import Database
from onboarding.engine import Engine


class Harness:
    def __init__(self, db_path=":memory:"):
        self.clock = FakeClock()
        self.settings = Settings(db_path=db_path, jitter=False, seed=7)
        self.engine = Engine(self.settings, self.clock, Database(db_path))
        self.rng = random.Random(42)

    def settle(self, seconds: float = 60, step: float = 0.5):
        """Advance simulated time, letting callbacks, timeouts and jobs run."""
        for _ in range(int(seconds / step)):
            self.clock.advance(step)
            self.engine.tick()

    def app(self, app_id):
        return self.engine.get_application(app_id)

    def check(self, app_id, ct):
        return next(c for c in self.app(app_id)["checks"] if c["check_type"] == ct)

    def actions(self, app_id):
        return [a["action"] for a in self.app(app_id)["audit"]]


@pytest.fixture
def h():
    return Harness()
