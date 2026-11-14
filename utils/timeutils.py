"""
Single place for time handling.

Rule of the project:  STORE in UTC  ->  CONVERT to local time only when DISPLAYING.

* The database keeps naive DATETIME columns that always hold UTC wall-clock time
  (MySQL drops tzinfo), so a naive datetime coming from the DB is treated as UTC.
* Anything shown to a human or to the LLM (Excel exports, dashboards, chat history,
  "current time" in prompts) must go through `to_local` / `fmt_local` / `now_local`.

The display timezone defaults to Africa/Cairo and can be changed with the
DISPLAY_TIMEZONE environment variable (e.g. DISPLAY_TIMEZONE=UTC).
"""
import logging
import os
from datetime import datetime, timezone, tzinfo
from typing import Optional

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DISPLAY_TZ_NAME = os.getenv("DISPLAY_TIMEZONE", "Africa/Cairo")


def _load_display_tz() -> tzinfo:
    try:
        return ZoneInfo(DISPLAY_TZ_NAME)
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError, ...
        logger.error(
            "Display timezone '%s' could not be loaded (%s). Is the 'tzdata' package "
            "installed? Falling back to UTC: displayed times will NOT be local.",
            DISPLAY_TZ_NAME, exc,
        )
        return timezone.utc


DISPLAY_TZ: tzinfo = _load_display_tz()


def utcnow() -> datetime:
    """Timezone-aware 'now' in UTC. Use this for everything that is stored."""
    return datetime.now(timezone.utc)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Return `dt` as an aware UTC datetime. A naive value is assumed to already be UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_local(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a stored (UTC) datetime to the display timezone."""
    aware = as_utc(dt)
    return aware.astimezone(DISPLAY_TZ) if aware else None


def now_local() -> datetime:
    """Current time in the display timezone (for prompts / anything shown to people)."""
    return utcnow().astimezone(DISPLAY_TZ)


def fmt_local(dt: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M", default: str = "") -> str:
    """Format a stored (UTC) datetime in the display timezone; `default` when dt is empty."""
    local = to_local(dt)
    return local.strftime(fmt) if local else default
