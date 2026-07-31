"""Trading-day boundaries for the daily drawdown baseline (CLAUDE.md §7 rule 8).

Pure date arithmetic — no clock reads, no I/O. The current time is always passed
in. `zoneinfo` is imported for timezone *rules*, which is data, not I/O.

Why this is its own module: "which trading day is it?" looks trivial and is not.
The reset happens at the user's local wall-clock time, so the answer depends on a
timezone whose offset changes twice a year. Storing a UTC offset instead of an
IANA zone name would silently drift by an hour at every DST change — the baseline
would be taken at 09:00 in winter and 08:00 in summer, and a daily loss limit
measured from the wrong starting equity is worse than no limit, because it looks
like it is working.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def session_date_for(now_utc: datetime, reset_time: time, timezone: str) -> date:
    """Return the user-local trading day that `now_utc` belongs to.

    A trading day runs from one reset to the next, so an instant before today's
    reset still belongs to *yesterday's* session.

    DST behaviour falls out of comparing local wall-clock times, and both awkward
    cases resolve correctly without special handling:

    * **Spring forward**, where the reset time never occurs (clocks jump 01:00 to
      02:00 with a 01:30 reset): the comparison first succeeds at 02:00 local, so
      the session simply starts at the first instant that exists. No day is
      skipped.
    * **Fall back**, where the reset time occurs twice: the comparison succeeds
      at the first occurrence, so the session starts then. The partial unique
      index on `equity_snapshots` prevents a second baseline being recorded when
      the hour repeats.
    """
    local = now_utc.astimezone(ZoneInfo(timezone))
    if local.time() >= reset_time:
        return local.date()
    return local.date() - timedelta(days=1)


def next_reset_utc(now_utc: datetime, reset_time: time, timezone: str) -> datetime:
    """Return the UTC instant of the next daily reset strictly after `now_utc`."""
    tz = ZoneInfo(timezone)
    local = now_utc.astimezone(tz)

    candidate = datetime.combine(local.date(), reset_time, tzinfo=tz)
    if candidate <= local:
        candidate = datetime.combine(
            local.date() + timedelta(days=1), reset_time, tzinfo=tz
        )
    return candidate.astimezone(UTC)


def is_same_session(
    a_utc: datetime, b_utc: datetime, reset_time: time, timezone: str
) -> bool:
    """True when two instants fall in the same trading day.

    Used to decide whether a stored baseline still applies, which is what makes
    the baseline survive a process restart: the answer depends only on the two
    timestamps, never on how long the process has been running.
    """
    return session_date_for(a_utc, reset_time, timezone) == session_date_for(
        b_utc, reset_time, timezone
    )
