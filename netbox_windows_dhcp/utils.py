"""Shared utility helpers for netbox-windows-dhcp."""


def lease_lifetime_display(seconds: int) -> str:
    """
    Convert a lease lifetime in seconds to the most readable human string,
    preferring the largest clean (exact) unit.

    Examples:
        259200  -> "3 Days"
        86400   -> "1 Day"
        262800  -> "73 Hours"   (not an exact number of days)
        3600    -> "1 Hour"
        90      -> "1 Minute 30 Seconds"  (not exact minutes, fall through)
        90      -> actually 90s = 1.5 min -> "90 Seconds"
        60      -> "1 Minute"
        45      -> "45 Seconds"
    """
    if seconds <= 0:
        return f'{seconds} Seconds'

    days, rem = divmod(seconds, 86400)
    if rem == 0:
        return f'{days} {"Day" if days == 1 else "Days"}'

    hours, rem = divmod(seconds, 3600)
    if rem == 0:
        return f'{hours} {"Hour" if hours == 1 else "Hours"}'

    minutes, rem = divmod(seconds, 60)
    if rem == 0:
        return f'{minutes} {"Minute" if minutes == 1 else "Minutes"}'

    return f'{seconds} {"Second" if seconds == 1 else "Seconds"}'


def decompose_lease_lifetime(seconds: int) -> tuple[int, str]:
    """
    Split a lease lifetime in seconds into (value, unit) using the largest
    clean unit — the inverse of what lease_lifetime_display does.

    Returns a tuple of (value, unit) where unit is one of:
        'days', 'hours', 'minutes', 'seconds'
    """
    if seconds > 0:
        if seconds % 86400 == 0:
            return seconds // 86400, 'days'
        if seconds % 3600 == 0:
            return seconds // 3600, 'hours'
        if seconds % 60 == 0:
            return seconds // 60, 'minutes'
    return seconds, 'seconds'
