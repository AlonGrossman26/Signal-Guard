"""Suite-wide guards.

**"No test may touch a real network" (CLAUDE.md §13) is enforced here, not
trusted.** The rule was previously a convention, and a convention is exactly what
Phase 8 broke: wiring `ingress/` to `execution/` silently turned an existing
end-to-end test into one that dialled `testnet.binance.vision` on every run. It
still passed — the call failed, the pipeline fail-closed to `BROKER_UNAVAILABLE`,
and the assertion under test was about something else — so nothing went red. Only
the outbound proxy log showed it.

That is the failure mode worth engineering against: not a test that breaks when
it reaches the network, but one that keeps passing while reaching it. A test
suite that quietly depends on an exchange being up is a suite that will fail at
3am for reasons unrelated to the code, and worse, one that could place a real
order from a laptop with real keys in the environment.

Loopback stays open because Postgres and Redis are genuine dependencies of the
integration suite — the guarantees they produce (atomic dedupe claims, partial
unique indexes) cannot be faked without testing the fake instead.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

# Everything the suite is legitimately allowed to reach: the local Postgres and
# Redis, and nothing else.
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


class NetworkAccessError(RuntimeError):
    """A test tried to open a socket to something that is not loopback."""


def _host_of(address: Any) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)


@pytest.fixture(autouse=True, scope="session")
def _forbid_outbound_network() -> Iterator[None]:
    """Fail any test that opens a non-loopback socket.

    Patched at `socket.socket.connect`, the single chokepoint every TCP client in
    the stack goes through — httpx, asyncpg and redis-py all land here — so a new
    code path cannot route around it by using a different library.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard(self: socket.socket, address: Any) -> Any:
        host = _host_of(address)
        if host not in _ALLOWED_HOSTS:
            raise NetworkAccessError(
                f"Test attempted to connect to {host!r}. CLAUDE.md §13: no test may "
                "touch a real network. Inject a fake (tests/fakes/fake_broker.py) "
                "or an httpx.MockTransport instead."
            )
        return real_connect(self, address)

    def guard_ex(self: socket.socket, address: Any) -> Any:
        host = _host_of(address)
        if host not in _ALLOWED_HOSTS:
            raise NetworkAccessError(f"Test attempted to connect to {host!r} (§13).")
        return real_connect_ex(self, address)

    socket.socket.connect = guard  # type: ignore[method-assign]
    socket.socket.connect_ex = guard_ex  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
