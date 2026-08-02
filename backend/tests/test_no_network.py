"""The network guard must be armed (CLAUDE.md §13).

`tests/conftest.py` blocks non-loopback sockets for the whole suite. That guard
is only worth having if it is actually active, and an autouse session fixture is
exactly the kind of thing that can stop being applied without anyone noticing —
a renamed fixture, a moved conftest, a `-p no:cacheprovider`-style flag.

So the guard gets a test of its own. If this goes green, "no test touches a real
network" is a property of the suite rather than a habit of whoever wrote it.
"""

from __future__ import annotations

import socket

import pytest

from tests.conftest import NetworkAccessError


def test_outbound_connections_are_blocked() -> None:
    """A socket to the public internet must raise, not connect."""
    with socket.socket() as s, pytest.raises(NetworkAccessError):
        s.connect(("example.com", 443))


def test_the_exchange_is_blocked_by_name() -> None:
    """The specific regression: Phase 8's wiring made a test dial the exchange.

    It passed anyway — the call failed, the pipeline fail-closed, and the
    assertion under test was about something else. Only the proxy log showed it.
    """
    with socket.socket() as s, pytest.raises(NetworkAccessError):
        s.connect(("testnet.binance.vision", 443))


def test_loopback_is_still_allowed() -> None:
    """Postgres and Redis are real dependencies of the integration suite.

    Blocking them too would force the integration tests onto fakes, and the
    guarantees they cover — the atomic dedupe claim, the partial unique index —
    are produced by Postgres and Redis themselves. Faking those would test the
    fake.
    """
    with socket.socket() as s:
        s.settimeout(0.2)
        # Nothing needs to be listening; the point is that the guard does not
        # raise NetworkAccessError for loopback.
        try:
            s.connect_ex(("127.0.0.1", 1))
        except NetworkAccessError:  # pragma: no cover - the failure we assert against
            pytest.fail("loopback must not be blocked")
