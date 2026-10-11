"""Date normalisation.

Every provider returns release dates in a different shape (ms timestamp, second
timestamp, "YYYY-MM-DD", "YYYY年MM月DD日"). The contract stores one shape -
``YYYY-MM-DD`` - and keeps the original in ``Field.raw`` so nothing is lost.
"""

from __future__ import annotations

import datetime as _dt
import re

_CN = re.compile(r"(\d{4})\s*[年./-]\s*(\d{1,2})?\s*[月./-]?\s*(\d{1,2})?")
_MS_THRESHOLD = 10 ** 11
_SEC_THRESHOLD = 10 ** 8


def iso_date(value) -> str | None:
    """Best-effort ISO date. Returns None when the value carries no date."""
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, (int, float)):
        try:
            if value >= _MS_THRESHOLD:
                ts = value / 1000.0
            elif value >= _SEC_THRESHOLD:
                ts = float(value)
            else:
                return None
            return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    m = _CN.search(text)
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2) or 1)
    day = int(m.group(3) or 1)
    if not (1900 <= year <= 2200 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def iso_utc(value) -> str | None:
    """Epoch (seconds **or** milliseconds) -> ``YYYY-MM-DDTHH:MM:SSZ``.

    Used for the playlist-level ``created_at`` / ``updated_at`` extras: the
    platforms hand out raw epochs (netease in ms, the others in seconds), so the
    contract stores one shape and keeps the original number in ``Field.raw``.
    Epoch is absolute, so the conversion is lossless - no timezone guesswork.
    Returns None for anything that is not a plausible timestamp.
    """
    if value in (None, "", 0, "0"):
        return None
    if not isinstance(value, (int, float)):
        return None
    try:
        if value >= _MS_THRESHOLD:
            ts = value / 1000.0
        elif value >= _SEC_THRESHOLD:
            ts = float(value)
        else:
            return None
        return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def year_of(value: str | int | None) -> int | None:
    """Year from an ISO date or from a bare year (statistics pass ints around)."""
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value if 1000 <= value <= 3000 else None
    text = str(value)
    if len(text) < 4:
        return None
    try:
        return int(text[:4])
    except ValueError:
        return None


def decade_of(iso: str | int | None) -> str | None:
    y = year_of(iso)
    if y is None:
        return None
    return f"{y // 10 * 10}s"
