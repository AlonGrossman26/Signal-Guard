"""Pure risk decision logic.

**This package performs no I/O.** No database, no network, no clock reads — the
current time is passed in via `Snapshot.now`. An import of `httpx`, `sqlalchemy`,
`redis`, or a call to `datetime.now()` inside this package is a defect, and
`tests/risk/test_purity.py` fails the build if one appears.

That constraint is what makes the engine exhaustively testable: every rule can be
exercised at its exact boundary, in milliseconds, with no fixtures.
"""

from signalguard.risk.engine import evaluate
from signalguard.risk.types import (
    AccountState,
    AlertInput,
    CircuitBreakerSnapshot,
    Decision,
    DrawdownSnapshot,
    InstrumentSpec,
    OpenPosition,
    RiskConfig,
    Snapshot,
)

__all__ = [
    "AccountState",
    "AlertInput",
    "CircuitBreakerSnapshot",
    "Decision",
    "DrawdownSnapshot",
    "InstrumentSpec",
    "OpenPosition",
    "RiskConfig",
    "Snapshot",
    "evaluate",
]
