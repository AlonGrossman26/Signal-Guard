"""`POST /webhook/{id}/test` as §8 actually intends it (F-1).

`CLAUDE.md` §8 calls this "the feature users will rely on most" and says to treat
it as first-class rather than a debug hook. Walking it as a new user showed it
was not living up to that, in three ways that all traced back to one decision:
it was handed **zeros** for account state, on the reading that "must not touch
the broker" meant "must know nothing".

* It could never return `APPROVED` — zero equity means a zero risk budget, so
  sizing rounded to nothing and the endpoint could only ever show failures.
* Market orders always came back `NO_STOP_LOSS`, blaming a stop that was fine
  when the real problem was having no price to check it against.
* An un-allowed symbol came back `INSTRUMENT_UNAVAILABLE`, naming an
  infrastructure problem instead of the user's actual mistake.

Reading our *own* tables is not touching the broker, and that is the fix. These
tests pin all four behaviours — including that a genuine stop error is still
reported as one, so the relabelling did not over-reach.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient

from tests.ingress.test_webhook_e2e import (
    DATABASE_URL,
    _seed_user_with_endpoint,
    hmac_headers,
    make_body,
)
from tests.ingress.test_webhook_e2e import app_env as app_env  # re-exported fixture

pytestmark = pytest.mark.integration


async def _post(
    client: AsyncClient, token: str, query: str = "", **overrides: object
) -> dict[str, Any]:
    body = make_body(**overrides)
    response = await client.post(
        f"/webhook/{token}/test{query}", content=body, headers=hmac_headers(body)
    )
    assert response.status_code == 200
    return json.loads(response.text)


async def test_a_good_signal_can_be_approved(app_env: AsyncClient) -> None:
    """The headline fix: `/test` can now say yes.

    Before this it could only ever show rejections, which made "check your rules
    before going live" impossible — the answer was always no, whatever you sent.
    """
    token = await _seed_user_with_endpoint()

    result = await _post(app_env, token, "?equity=100000")

    assert result["verdict"] == "APPROVED"
    assert result["reason_code"] == "APPROVED"
    assert result["computed_qty"] is not None
    # 1% of 100,000 = 1,000 risk; stop distance 1,000; 20bps of 62,000 = 124.
    # 1000 / 1124 = 0.8896..., rounded down to the 0.00001 lot step.
    assert result["computed_qty"].startswith("0.889")


async def test_a_market_order_without_a_price_does_not_blame_the_stop(
    app_env: AsyncClient,
) -> None:
    """`PRICE_UNAVAILABLE`, not `NO_STOP_LOSS`.

    The old code sent users to fix a stop that was never wrong.
    """
    token = await _seed_user_with_endpoint()

    result = await _post(
        app_env, token, order_type="market", limit_price=None, stop_price="61000.00"
    )

    assert result["verdict"] == "REJECTED"
    assert result["reason_code"] == "PRICE_UNAVAILABLE"
    assert "stop itself was not the problem" in result["reason_detail"]


async def test_a_market_order_with_a_supplied_price_is_evaluated_properly(
    app_env: AsyncClient,
) -> None:
    """`?price=` makes a market order testable at all."""
    token = await _seed_user_with_endpoint()

    result = await _post(
        app_env,
        token,
        "?equity=100000&price=62000",
        order_type="market",
        limit_price=None,
        stop_price="61000.00",
    )

    assert result["verdict"] == "APPROVED"
    assert result["assumptions"]["reference_price"] == "62000"


async def test_an_unallowed_symbol_names_the_allowlist_not_the_exchange(
    app_env: AsyncClient,
) -> None:
    """`SYMBOL_NOT_ALLOWED`, not `INSTRUMENT_UNAVAILABLE`.

    Exchange filters are only needed to size a trade, and a symbol rule 5 will
    reject is never sized — so the lookup that used to preempt it was both
    misleading and unnecessary.
    """
    token = await _seed_user_with_endpoint(allowed_symbols=["BTCUSDT"])

    result = await _post(app_env, token, symbol="ETHUSDT")

    assert result["reason_code"] == "SYMBOL_NOT_ALLOWED"


async def test_a_real_stop_error_is_still_reported_as_one(
    app_env: AsyncClient,
) -> None:
    """The relabelling must not swallow the case it was carved out of.

    A stop on the wrong side of entry is genuinely `NO_STOP_LOSS`, and staying
    that way is what makes the new code trustworthy.
    """
    token = await _seed_user_with_endpoint()

    result = await _post(app_env, token, stop_price="63000.00")

    assert result["reason_code"] == "NO_STOP_LOSS"
    assert "must be below entry" in result["reason_detail"]


async def test_the_response_explains_what_it_assumed(app_env: AsyncClient) -> None:
    """A simulator that hides its inputs is one you cannot trust.

    "0.889 BTC" means nothing until you know the equity and price behind it.
    """
    token = await _seed_user_with_endpoint()

    result = await _post(app_env, token, "?equity=50000")
    assumptions = result["assumptions"]

    assert assumptions["equity"] == "50000"
    assert assumptions["simulated"] is True
    assert "override" in assumptions["equity_source"]
    assert "No broker was contacted" in assumptions["note"]


async def test_it_says_when_it_fell_back_to_a_default(app_env: AsyncClient) -> None:
    """A brand-new account has no reconciled equity, and must say so.

    Silently inventing a number and presenting the resulting size as fact is the
    kind of thing that gets believed.
    """
    token = await _seed_user_with_endpoint()

    result = await _post(app_env, token)

    assert Decimal(result["assumptions"]["equity"]) == Decimal("10000")
    assert "assumed default" in result["assumptions"]["equity_source"]


async def test_a_simulation_leaves_no_trace_on_real_risk_state(
    app_env: AsyncClient,
) -> None:
    """A what-if must never write the state the real engine reads.

    `/test` runs on a *simulated* equity figure. If that got persisted as the
    day's drawdown baseline, every genuine drawdown check for the rest of the
    session would be measured against a number the user invented while
    experimenting — silently blocking live trading, or hiding a real drawdown.
    Caught because a test asserting on the default equity started seeing a
    database-round-tripped value instead of the constant.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    token = await _seed_user_with_endpoint()

    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def snapshot_count() -> int:
        async with maker() as s:
            return int(
                (
                    await s.execute(text("SELECT count(*) FROM equity_snapshots"))
                ).scalar_one()
            )

    # A delta, not an absolute: the live-webhook tests legitimately create
    # baselines, so only the change across this call is the invariant.
    before = await snapshot_count()
    await _post(app_env, token, "?equity=999999")
    after = await snapshot_count()
    await engine.dispose()

    assert after == before, "a /test run must not create an equity baseline"
