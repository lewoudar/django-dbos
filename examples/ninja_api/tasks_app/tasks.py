"""The one durable task this example enqueues and polls."""

from __future__ import annotations

import time

from django.tasks import task


@task
def count_words(text: str) -> dict[str, int]:
    # A sleep stands in for real work, so the API example has time to poll
    # a RUNNING status before the result becomes available.
    time.sleep(5)
    return {'word_count': len(text.split()), 'char_count': len(text)}
