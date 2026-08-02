"""The risk engine entry point.

One public function: `evaluate(snapshot) -> Decision`. It walks the rules in
specification order, stops at the first rejection, and otherwise approves with a
computed quantity.

Pure. No database, no network, no clock — `snapshot.now` is the only notion of
time, and it is supplied by the caller.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from signalguard.enums import ReasonCode, Verdict
from signalguard.risk.rules import ORDERED_RULES, size_for
from signalguard.risk.types import Decision, Snapshot

logger = logging.getLogger(__name__)


def evaluate(snapshot: Snapshot) -> Decision:
    """Evaluate one alert against the user's rules.

    Any unexpected exception is converted into a rejection rather than being
    allowed to propagate. That is constraint #1 taken literally: if the engine
    itself is broken, the safe answer is still "no". An exception escaping here
    would leave the caller deciding what to do about an alert whose risk is
    genuinely unknown, and there is no good answer to that question.
    """
    try:
        for rule in ORDERED_RULES:
            decision = rule(snapshot)
            if decision is not None:
                return decision
        return _approve(snapshot)

    except Exception:
        # Log with a stack trace, but never leak the payload into the message —
        # it may carry the shared secret.
        logger.exception("Risk engine raised; failing closed")
        return Decision(
            verdict=Verdict.REJECTED,
            reason_code=ReasonCode.INTERNAL_ERROR,
            reason_detail="risk evaluation failed; rejected to fail closed",
            rule_snapshot=snapshot.config.as_snapshot_dict(),
        )


def _approve(snapshot: Snapshot) -> Decision:
    """Build the approval, carrying the quantity the order should use."""
    alert = snapshot.alert
    assert alert is not None  # rule 2 guarantees this on the approval path

    if alert.is_exit:
        # An exit closes what is actually held. The quantity comes from broker
        # state, not from a calculation: sizing an exit would risk selling more
        # than we own, or leaving a remainder behind.
        position = (
            snapshot.account.position_for(alert.symbol)
            if snapshot.account is not None
            else None
        )
        qty = abs(position.qty) if position is not None else Decimal("0")

        if qty <= 0:
            # Nothing is held, so there is nothing to close (plan §4, OQ-6: a
            # `sell` with no position is refused rather than read as a short).
            # Rejecting rather than approving-with-zero is an audit-trail
            # decision: an APPROVED row for a signal that placed no order tells
            # the user a trade happened when none did, and the decision feed is
            # the one thing in this product that must never mislead.
            return Decision(
                verdict=Verdict.REJECTED,
                reason_code=ReasonCode.NO_POSITION_TO_CLOSE,
                reason_detail=f"no open position in {alert.symbol} to close",
                computed_qty=Decimal("0"),
                entry_reference_price=snapshot.reference_price,
                rule_snapshot=snapshot.config.as_snapshot_dict(),
            )

        return Decision(
            verdict=Verdict.APPROVED,
            reason_code=ReasonCode.APPROVED,
            reason_detail=f"exit {alert.symbol}",
            computed_qty=qty,
            entry_reference_price=snapshot.reference_price,
            rule_snapshot=snapshot.config.as_snapshot_dict(),
        )

    result = size_for(snapshot)
    assert result is not None  # rule 9 guarantees this on the approval path

    return Decision(
        verdict=Verdict.APPROVED,
        reason_code=ReasonCode.APPROVED,
        reason_detail=(
            f"sized {result.qty} {alert.symbol} "
            f"(risk {result.risk_amount}, stop distance {result.stop_distance})"
        ),
        computed_qty=result.qty,
        entry_reference_price=snapshot.reference_price,
        stop_price=alert.stop_price,
        rule_snapshot=snapshot.config.as_snapshot_dict(),
    )
