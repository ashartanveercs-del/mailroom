"""Crash injection for the fakes: kill the "process" at an exact point in a run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


class SimulatedCrash(BaseException):
    """Stands in for the process dying. A BaseException, so no `except Exception` in the code
    under test can swallow it, just as no handler runs when a real process is killed."""


@dataclass
class CrashPlan:
    """Crash on the `at`-th write (0-based): `before` it reaches storage, or `after` it landed
    but before the caller learns that it did (the response was lost)."""

    at: int
    when: Literal["before", "after"]
    fired: bool = False

    def check(self, index: int, phase: str) -> None:
        if not self.fired and index == self.at and phase == self.when:
            self.fired = True
            raise SimulatedCrash(f"crash {phase} write #{index}")
