"""All ORM models.

Importing this module registers every table on `Base.metadata`, which is what
Alembic's autogenerate compares against the live database. A model that is not
imported here is invisible to migrations — so new model modules must be added.
"""

from signalguard.db.models.alert import Alert
from signalguard.db.models.broker import BrokerAccount, Instrument
from signalguard.db.models.decision import Decision
from signalguard.db.models.order import Order
from signalguard.db.models.position import EquitySnapshot, Position
from signalguard.db.models.risk_profile import RiskProfile
from signalguard.db.models.trade import CircuitBreakerState, Trade
from signalguard.db.models.user import Session, User
from signalguard.db.models.webhook import WebhookEndpoint

__all__ = [
    "Alert",
    "BrokerAccount",
    "CircuitBreakerState",
    "Decision",
    "EquitySnapshot",
    "Instrument",
    "Order",
    "Position",
    "RiskProfile",
    "Session",
    "Trade",
    "User",
    "WebhookEndpoint",
]
