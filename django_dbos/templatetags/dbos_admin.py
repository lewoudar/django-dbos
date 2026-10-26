"""Template filters of the django-dbos admin pages: timestamps and durations."""

from __future__ import annotations

from datetime import datetime

from django import template
from django.utils import dateformat, timezone

register = template.Library()

TIMESTAMP_FORMAT = 'Y-m-d H:i:s'


@register.filter
def dbos_time(value: datetime | str | None) -> str:
    """A timestamp to the second, in the current time zone; strings are shown as they are."""
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return dateformat.format(value, TIMESTAMP_FORMAT)


@register.filter
def dbos_duration(milliseconds: float | None) -> str:
    """A duration in milliseconds, in the largest fitting units (``250 ms``, ``5.0 s``, ``2 min 5 s``)."""
    if milliseconds is None:
        return ''
    if milliseconds < 1000:
        return f'{milliseconds:.0f} ms'
    seconds = milliseconds / 1000
    if seconds < 60:
        return f'{seconds:.1f} s'
    minutes, seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return f'{minutes} min {seconds} s'
    hours, minutes = divmod(minutes, 60)
    return f'{hours} h {minutes} min'
