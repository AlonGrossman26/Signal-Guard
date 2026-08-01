"""Outbound notifications (CLAUDE.md §1, §10).

Telegram first (Discord is explicitly out of scope for v1, §13). One rule governs
this whole package: **a notification failure must never break the thing it is
reporting on.** A kill switch that fails to flatten an account because Telegram
was unreachable would be a catastrophic inversion of priorities. So every send
is best-effort — failures are logged and swallowed, exactly like the realtime
pub/sub fan-out.
"""

from signalguard.notify.notifier import Notifier, notifier_from_settings
from signalguard.notify.telegram import TelegramClient, TelegramError

__all__ = ["Notifier", "TelegramClient", "TelegramError", "notifier_from_settings"]
