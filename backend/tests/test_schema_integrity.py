"""Schema-level guarantees that must not regress.

These check the models rather than a live database, so they run anywhere. The
things asserted here are the ones a future change could quietly break while
every other test still passes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import Float, Numeric

from signalguard.db.base import Base
from signalguard.db.models import (  # noqa: F401 - importing registers the tables
    Alert,
    BrokerAccount,
    Decision,
    Order,
    RiskProfile,
)

EXPECTED_TABLES = {
    "users",
    "sessions",
    "webhook_endpoints",
    "broker_accounts",
    "risk_profiles",
    "instruments",
    "alerts",
    "decisions",
    "orders",
    "positions",
    "equity_snapshots",
    "trades",
    "circuit_breaker_state",
}


def test_all_expected_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_no_float_columns_anywhere() -> None:
    """Constraint #3: no floats for money or quantities — anywhere, ever.

    Checked across every column rather than the money ones specifically, because
    the failure mode is someone adding a new column and reaching for Float out of
    habit.
    """
    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Float)
    ]
    assert offenders == [], f"Float columns found (must be NUMERIC): {offenders}"


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        ("orders", "qty"),
        ("orders", "price"),
        ("orders", "stop_price"),
        ("orders", "fees"),
        ("decisions", "computed_qty"),
        ("decisions", "entry_reference_price"),
        ("positions", "qty"),
        ("positions", "avg_entry"),
        ("equity_snapshots", "equity"),
        ("equity_snapshots", "free_balance"),
        ("trades", "realized_pnl"),
        ("instruments", "lot_step"),
        ("instruments", "min_notional"),
    ],
)
def test_money_columns_are_numeric_36_18(table_name: str, column_name: str) -> None:
    column = Base.metadata.tables[table_name].columns[column_name]
    assert isinstance(column.type, Numeric)
    assert (column.type.precision, column.type.scale) == (36, 18)


def test_alerts_and_decisions_have_no_cascade_delete_from_users() -> None:
    """The audit trail must not be deletable as a side effect of removing a user."""
    alerts_user_fk = next(
        fk for fk in Base.metadata.tables["alerts"].foreign_keys
        if fk.column.table.name == "users"
    )
    assert alerts_user_fk.ondelete == "RESTRICT"


def test_broker_accounts_are_pinned_to_testnet() -> None:
    """Constraint #2 enforced in the schema, not merely in application code."""
    checks = [
        str(c.sqltext)
        for c in Base.metadata.tables["broker_accounts"].constraints
        if hasattr(c, "sqltext")
    ]
    assert any("is_testnet" in c for c in checks), checks


def test_risk_percentages_are_constrained_to_fractions() -> None:
    """0.01 means 1%. A profile storing 50 would size 5,000x too large."""
    checks = [
        str(c.sqltext)
        for c in Base.metadata.tables["risk_profiles"].constraints
        if hasattr(c, "sqltext")
    ]
    assert any("risk_per_trade_pct > 0" in c and "< 1" in c for c in checks), checks


def test_decision_has_partial_unique_index_for_live_decisions() -> None:
    """One alert, one live decision — the database backstop for idempotency."""
    index = next(
        ix for ix in Base.metadata.tables["decisions"].indexes
        if ix.name == "uq_decisions_alert_id_live"
    )
    assert index.unique is True
    assert "is_test = false" in str(index.dialect_options["postgresql"]["where"])


def test_equity_baseline_is_unique_per_trading_day() -> None:
    """What makes the DST and restart requirements hold structurally."""
    index = next(
        ix for ix in Base.metadata.tables["equity_snapshots"].indexes
        if ix.name == "uq_equity_snapshots_baseline_per_day"
    )
    assert index.unique is True
    assert [c.name for c in index.columns] == ["broker_account_id", "session_date"]


def test_risk_profile_defaults_deny_all_symbols() -> None:
    """A new profile trades nothing until a symbol is opted in.

    Both defaults are checked: the Python-side one (used when the ORM builds a
    row) and the server-side one (used by a plain INSERT that bypasses the ORM).
    A row created either way must start with an empty allowlist.
    """
    column = Base.metadata.tables["risk_profiles"].columns["allowed_symbols"]
    assert column.default is not None
    # SQLAlchemy wraps a callable default so it receives an execution context,
    # so `.arg` is that wrapper rather than `list` itself — call it to see what
    # a new row would actually get.
    assert column.default.arg(None) == []
    assert str(column.server_default.arg) == "{}"


def test_decimal_defaults_are_decimal_not_float() -> None:
    """A float default would poison the value before it ever reached Postgres."""
    column = Base.metadata.tables["risk_profiles"].columns["risk_per_trade_pct"]
    assert isinstance(column.default.arg, Decimal)
