"""The user's risk rules — one column per parameter in CLAUDE.md §7."""

from __future__ import annotations

import uuid
from datetime import datetime, time
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Time,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from signalguard.db.base import MONEY, PERCENT, Base, created_at_col, updated_at_col, uuid_pk


class RiskProfile(Base):
    """One row per user (plan §2, A13).

    Every percentage is stored as a fraction: `risk_per_trade_pct = 0.01` means
    1%. The CHECK constraints below are not decoration — a profile with
    `risk_per_trade_pct = 50` (someone typing "50" meaning 50%) would size
    positions 5,000× too large, and the database is the last place that mistake
    can be stopped before it reaches the sizing formula.
    """

    __tablename__ = "risk_profiles"
    __table_args__ = (
        CheckConstraint(
            "risk_per_trade_pct > 0 AND risk_per_trade_pct < 1", name="risk_pct_fraction"
        ),
        CheckConstraint(
            "max_daily_dd_pct > 0 AND max_daily_dd_pct < 1", name="dd_pct_fraction"
        ),
        CheckConstraint(
            "min_stop_distance_pct > 0 AND min_stop_distance_pct < 1",
            name="stop_dist_pct_fraction",
        ),
        CheckConstraint("max_alert_age_sec > 0", name="alert_age_positive"),
        CheckConstraint("future_tolerance_sec >= 0", name="future_tolerance_non_negative"),
        CheckConstraint("dedupe_window_sec > 0", name="dedupe_window_positive"),
        CheckConstraint("consecutive_loss_threshold > 0", name="loss_threshold_positive"),
        CheckConstraint("circuit_breaker_cooldown_minutes >= 0", name="cooldown_non_negative"),
        CheckConstraint("max_open_positions > 0", name="max_open_positive"),
        CheckConstraint("max_notional_per_trade > 0", name="max_notional_trade_positive"),
        CheckConstraint("max_total_notional > 0", name="max_total_notional_positive"),
        CheckConstraint("fee_slippage_buffer_bps >= 0", name="buffer_non_negative"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )

    # Bumped on every edit and copied into each decision's rule_snapshot, so
    # "which rules produced this decision?" is answerable with a cheap lookup
    # rather than a JSONB diff (plan §3, Q4).
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- Rule 3: staleness ---------------------------------------------------
    max_alert_age_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    future_tolerance_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    # --- Rule 4: duplicates --------------------------------------------------
    dedupe_window_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    # --- Rule 5: symbol allowlist -------------------------------------------
    # Default empty = nothing is tradeable. A new profile trades nothing until
    # the user opts a symbol in; the fail-closed default is deny, not allow.
    allowed_symbols: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), nullable=False, default=list, server_default="{}"
    )

    # --- Rule 6: mandatory stop-loss ----------------------------------------
    # A stop closer than this to entry is rejected: a near-zero stop distance
    # produces an absurd position size, which is the classic way this feature
    # gets exploited by a buggy script.
    min_stop_distance_pct: Mapped[Decimal] = mapped_column(
        PERCENT, nullable=False, default=Decimal("0.001")
    )

    # --- Rule 7: circuit breaker --------------------------------------------
    consecutive_loss_threshold: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3
    )
    circuit_breaker_cooldown_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60
    )
    circuit_breaker_manual_reset: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # --- Rule 8: daily drawdown ---------------------------------------------
    max_daily_dd_pct: Mapped[Decimal] = mapped_column(
        PERCENT, nullable=False, default=Decimal("0.05")
    )

    # --- Rule 9: position sizing --------------------------------------------
    risk_per_trade_pct: Mapped[Decimal] = mapped_column(
        PERCENT, nullable=False, default=Decimal("0.01")
    )
    # Fee + slippage headroom in basis points (20 = 0.20%), applied so a
    # worst-case stop-out still loses no more than risk_per_trade_pct.
    fee_slippage_buffer_bps: Mapped[int] = mapped_column(
        Integer, nullable=False, default=20
    )
    max_notional_per_trade: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("1000")
    )

    # --- Rule 10: exposure caps ---------------------------------------------
    max_open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_total_notional: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("5000")
    )
    allow_pyramiding: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # --- Daily reset (rule 8's baseline) -------------------------------------
    # Stored as a local wall-clock time plus an IANA timezone name, NOT as a UTC
    # offset. An offset would silently drift by an hour at every DST change; the
    # zone name keeps "09:00 my time" meaning 09:00 all year (constraint #7).
    daily_reset_time: Mapped[time] = mapped_column(
        Time, nullable=False, default=time(0, 0)
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
