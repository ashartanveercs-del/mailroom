"""Fixtures shared by the test modules (builders live in tests/support.py)."""

from __future__ import annotations

import contextlib
import socket

import pytest

from tests.support import World


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The HTTP simulator is in-process; a real socket or DNS lookup is a test bug.

    pytest.fail raises outside Exception, so retry and fallback handlers cannot hide an
    accidental request and turn an online test into an apparent offline success.
    """
    def blocked(*args, **kwargs):
        pytest.fail("offline test attempted a real network operation")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "gethostbyname", blocked)
    monkeypatch.setattr(socket, "gethostbyname_ex", blocked)


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
