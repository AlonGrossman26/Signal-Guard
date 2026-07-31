"""Daily-reset boundaries across DST and across a restart (CLAUDE.md §13).

The rule being tested: a trading day runs from one daily reset to the next, in
the user's own timezone. Get this wrong and the drawdown baseline is taken from
the wrong starting equity, which makes the daily loss limit measure the wrong
thing while still appearing to work — the worst kind of bug in this system.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from signalguard.risk.session import is_same_session, next_reset_utc, session_date_for

LONDON = "Europe/London"
NEW_YORK = "America/New_York"
RESET_9AM = time(9, 0)
RESET_MIDNIGHT = time(0, 0)


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


# --- Basic session assignment -------------------------------------------------


def test_before_reset_belongs_to_the_previous_day() -> None:
    """08:59 local, with a 09:00 reset, is still yesterday's trading day."""
    assert session_date_for(utc(2026, 7, 15, 7, 59), RESET_9AM, LONDON) == date(
        2026, 7, 14
    )


def test_after_reset_belongs_to_the_current_day() -> None:
    """09:01 BST is 08:01 UTC in July."""
    assert session_date_for(utc(2026, 7, 15, 8, 1), RESET_9AM, LONDON) == date(
        2026, 7, 15
    )


def test_exactly_at_reset_starts_the_new_day() -> None:
    assert session_date_for(utc(2026, 7, 15, 8, 0), RESET_9AM, LONDON) == date(
        2026, 7, 15
    )


def test_timezone_actually_matters() -> None:
    """The same instant is a different trading day for users in different zones."""
    instant = utc(2026, 7, 15, 12, 0)
    assert session_date_for(instant, RESET_9AM, LONDON) == date(2026, 7, 15)
    # 12:00 UTC is 08:00 in New York — before a 09:00 reset, so still the 14th.
    assert session_date_for(instant, RESET_9AM, NEW_YORK) == date(2026, 7, 14)


# --- DST: spring forward ------------------------------------------------------
# On 2026-03-29 London clocks jump 01:00 GMT -> 02:00 BST. Local 01:30 does not
# exist that day.


def test_spring_forward_day_is_not_skipped() -> None:
    """A 23-hour day still produces exactly one trading day."""
    before = session_date_for(utc(2026, 3, 29, 0, 30), time(1, 30), LONDON)
    after = session_date_for(utc(2026, 3, 29, 2, 30), time(1, 30), LONDON)
    assert before == date(2026, 3, 28)
    assert after == date(2026, 3, 29)


def test_spring_forward_nonexistent_reset_time_still_starts_a_session() -> None:
    """With a reset at 01:30 — a time that never occurs — the day still begins.

    Local time jumps 00:59 GMT to 02:00 BST, so the first local instant at or
    after 01:30 is 02:00. The session starts there rather than never starting.
    """
    at_0159_gmt = utc(2026, 3, 29, 0, 59)
    at_0200_bst = utc(2026, 3, 29, 1, 0)
    assert session_date_for(at_0159_gmt, time(1, 30), LONDON) == date(2026, 3, 28)
    assert session_date_for(at_0200_bst, time(1, 30), LONDON) == date(2026, 3, 29)


def test_reset_holds_local_wall_clock_across_spring_forward() -> None:
    """09:00 local stays 09:00 local — the UTC instant shifts, not the user's day.

    This is the whole reason the profile stores an IANA zone name rather than a
    UTC offset. An offset would have drifted the reset by an hour here.
    """
    before_dst = next_reset_utc(utc(2026, 3, 20, 12, 0), RESET_9AM, LONDON)
    after_dst = next_reset_utc(utc(2026, 4, 10, 12, 0), RESET_9AM, LONDON)
    assert before_dst.hour == 9   # GMT: 09:00 local == 09:00 UTC
    assert after_dst.hour == 8    # BST: 09:00 local == 08:00 UTC


# --- DST: fall back -----------------------------------------------------------
# On 2026-10-25 London clocks go 02:00 BST -> 01:00 GMT. Local 01:30 occurs twice.


def test_fall_back_day_is_not_duplicated() -> None:
    """A 25-hour day still produces exactly one trading day."""
    first_0130 = utc(2026, 10, 25, 0, 30)   # 01:30 BST
    second_0130 = utc(2026, 10, 25, 1, 30)  # 01:30 GMT, the repeat
    assert session_date_for(first_0130, time(1, 30), LONDON) == date(2026, 10, 25)
    assert session_date_for(second_0130, time(1, 30), LONDON) == date(2026, 10, 25)


def test_ambiguous_hour_stays_in_one_session() -> None:
    """Both passes through the repeated hour belong to the same trading day."""
    first = utc(2026, 10, 25, 0, 30)
    second = utc(2026, 10, 25, 1, 30)
    assert is_same_session(first, second, time(1, 30), LONDON)


def test_reset_holds_local_wall_clock_across_fall_back() -> None:
    before = next_reset_utc(utc(2026, 10, 20, 12, 0), RESET_9AM, LONDON)
    after = next_reset_utc(utc(2026, 11, 5, 12, 0), RESET_9AM, LONDON)
    assert before.hour == 8  # BST
    assert after.hour == 9   # GMT


# --- A full DST year ----------------------------------------------------------


@pytest.mark.parametrize("timezone", [LONDON, NEW_YORK, "Australia/Sydney", "UTC"])
def test_every_day_of_a_dst_year_maps_to_exactly_one_session(timezone: str) -> None:
    """Walk a whole year hourly: sessions advance by one day, never skip or repeat.

    This is the test that would catch an off-by-one at a DST edge that the
    hand-picked cases above happened to miss.
    """
    moment = utc(2026, 1, 1)
    end = utc(2027, 1, 1)
    seen: list[date] = []

    while moment < end:
        session = session_date_for(moment, RESET_9AM, timezone)
        if not seen or session != seen[-1]:
            seen.append(session)
        moment += timedelta(hours=1)

    # Strictly increasing, one calendar day at a time.
    for earlier, later in zip(seen, seen[1:], strict=False):
        assert later == earlier + timedelta(days=1), (
            f"{timezone}: session jumped from {earlier} to {later}"
        )
    assert len(seen) >= 364


# --- Restart survival ---------------------------------------------------------


def test_session_identity_is_a_pure_function_of_the_timestamps() -> None:
    """The baseline survives a restart because nothing depends on process state.

    "Which trading day is it?" is answered from the timestamp alone, so a process
    that restarts mid-session computes the same session date it did before, finds
    the same stored baseline, and carries on. Nothing is held in memory that a
    restart could lose.
    """
    before_restart = utc(2026, 7, 15, 10, 0)
    after_restart = utc(2026, 7, 15, 16, 30)

    assert is_same_session(before_restart, after_restart, RESET_9AM, LONDON)
    assert session_date_for(before_restart, RESET_9AM, LONDON) == session_date_for(
        after_restart, RESET_9AM, LONDON
    )


def test_restart_after_the_reset_starts_a_new_session() -> None:
    before_reset = utc(2026, 7, 15, 7, 0)   # 08:00 BST, before a 09:00 reset
    after_reset = utc(2026, 7, 15, 9, 0)    # 10:00 BST, after it
    assert not is_same_session(before_reset, after_reset, RESET_9AM, LONDON)


def test_midnight_reset_matches_the_local_calendar_day() -> None:
    assert session_date_for(utc(2026, 7, 15, 23, 30), RESET_MIDNIGHT, LONDON) == date(
        2026, 7, 16
    )  # 00:30 BST on the 16th
