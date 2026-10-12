"""Fixtures shared by the test modules (builders live in tests/support.py)."""

from __future__ import annotations

import contextlib

import pytest

from tests.support import World


@pytest.fixture
def world_factory():
    worlds = []

    def make(*args, **kwargs):
        world = World(*args, **kwargs)
        worlds.append(world)
        return world

    yield make
    for world in worlds:
        with contextlib.suppress(Exception):
            world.ledger.close()
