"""Declarative base and the shared column types.

The column type aliases exist so that "how do we store money?" is answered in
exactly one place. If every model spelled out `Numeric(36, 18)` by hand, one
model would eventually spell it differently — and a silently truncated quantity
is precisely the class of bug constraint #3 exists to prevent.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, Numeric, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Money and quantities: NUMERIC(36, 18). Never float, never double precision.
# 18 decimal places covers the smallest crypto lot steps; 18 integer digits
# covers any account size this will ever see.
MONEY = Numeric(36, 18)

# Percentages are stored as fractions: 0.01 means 1%, never 1. One convention,
# decided once, so no call site has to guess which one a column uses.
PERCENT = Numeric(9, 6)

# Predictable constraint/index names, so Alembic autogenerate produces stable
# migrations instead of churning on randomly-named constraints.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    """A UUID primary key, generated application-side.

    UUIDs rather than serial integers so an ID appearing in a URL is neither
    guessable nor enumerable — you cannot walk /decisions/1, /decisions/2.
    """
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


def created_at_col() -> Mapped[datetime]:
    """Creation timestamp, defaulted by the database, always UTC (constraint #7)."""
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def updated_at_col() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
