"""Guards the project's most important design rule: `risk/` performs no I/O.

CLAUDE.md §5: "An `import` of `httpx`, `sqlalchemy`, `redis`, or `datetime.now`
inside `risk/` is a defect. Add a test that asserts this."

This is enforced by parsing the source rather than by importing and inspecting,
so a violation is caught even if the offending line never executes. The failure
mode this prevents is gradual: someone needs one value from the database, adds
one query, and the engine stops being exhaustively testable — not with a bang,
but one convenience at a time.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import signalguard.risk

RISK_PACKAGE = Path(signalguard.risk.__file__).parent

# Modules that would make the engine depend on the outside world.
FORBIDDEN_IMPORTS = frozenset(
    {
        "httpx", "requests", "aiohttp", "urllib", "urllib3", "socket",
        "sqlalchemy", "asyncpg", "psycopg", "psycopg2",
        "redis", "aioredis",
        "fastapi", "starlette", "uvicorn",
        "os", "pathlib", "subprocess", "open",
        "signalguard.db", "signalguard.api", "signalguard.execution",
        "signalguard.ingress", "signalguard.config", "signalguard.redis_client",
    }
)

# Calls that read the clock. Time must be passed in, never fetched: a rule that
# called now() could not be tested at the exact boundary where it matters.
FORBIDDEN_CALLS = frozenset({"now", "utcnow", "today", "time", "monotonic"})

# `time.time()` etc. are caught by FORBIDDEN_CALLS; these are the safe owners of
# a method literally named `time` or `today`.
_SAFE_CALL_RECEIVERS = frozenset({"local", "candidate", "alert", "snapshot", "a", "b"})


def _risk_modules() -> list[Path]:
    return sorted(RISK_PACKAGE.rglob("*.py"))


def test_risk_package_has_modules() -> None:
    """If this fails, the rest of the file is silently testing nothing."""
    assert len(_risk_modules()) >= 5


@pytest.mark.parametrize("module_path", _risk_modules(), ids=lambda p: p.name)
def test_no_io_imports(module_path: Path) -> None:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    offenders = {
        name
        for name in imported
        for forbidden in FORBIDDEN_IMPORTS
        if name == forbidden or name.startswith(f"{forbidden}.")
    }
    assert not offenders, (
        f"{module_path.name} imports I/O modules {sorted(offenders)}. "
        f"risk/ must stay pure — pass the value in via the Snapshot instead."
    )


@pytest.mark.parametrize("module_path", _risk_modules(), ids=lambda p: p.name)
def test_no_clock_reads(module_path: Path) -> None:
    """The current time is passed in via `Snapshot.now`, never read."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in FORBIDDEN_CALLS:
            continue
        receiver = func.value
        receiver_name = receiver.id if isinstance(receiver, ast.Name) else None
        if receiver_name in _SAFE_CALL_RECEIVERS:
            continue
        offenders.append(f"{receiver_name}.{func.attr}() at line {node.lineno}")

    assert not offenders, (
        f"{module_path.name} reads the clock: {offenders}. "
        f"Time must arrive via Snapshot.now."
    )


def test_engine_evaluates_without_any_environment() -> None:
    """A sanity check that the purity actually buys what it claims.

    No database, no Redis, no network, no configuration — just data in, decision
    out. If this ever needs a fixture, purity has been lost.
    """
    from signalguard.risk import evaluate
    from tests.risk.builders import snapshot

    decision = evaluate(snapshot())
    assert decision.is_approved
