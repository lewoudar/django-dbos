"""Tests for the template filters of the admin pages."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from django_dbos.templatetags.dbos_admin import dbos_duration, dbos_time


@pytest.mark.parametrize(
    ('milliseconds', 'expected'),
    [
        pytest.param(None, '', id='none'),
        pytest.param(250, '250 ms', id='milliseconds'),
        pytest.param(5_005, '5.0 s', id='seconds'),
        pytest.param(125_000, '2 min 5 s', id='minutes'),
        pytest.param(3_725_000, '1 h 2 min', id='hours'),
    ],
)
def test_dbos_duration(milliseconds, expected):
    assert dbos_duration(milliseconds) == expected


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        pytest.param(None, '', id='none'),
        pytest.param('not a date', 'not a date', id='string-kept'),
        # Converted to the current time zone (UTC+9, the next day)...
        pytest.param(datetime(2026, 10, 26, 17, 48, 12, tzinfo=UTC), '2026-10-27 02:48:12', id='aware'),
        # ...while a naive one is taken as already local and shown as it is.
        pytest.param(datetime(2026, 10, 26, 17, 48, 12), '2026-10-26 17:48:12', id='naive'),  # noqa: DTZ001
    ],
)
def test_dbos_time(settings, value, expected):
    # A time zone without daylight saving time: its offset never depends on the date.
    settings.TIME_ZONE = 'Asia/Tokyo'
    assert dbos_time(value) == expected
