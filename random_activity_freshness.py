"""Freshness checks for the optional random activity-care snapshot."""

from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any


def current_random_activity_snapshot(
    value: Any,
    now: datetime,
    *,
    fetch_max_age: timedelta = timedelta(minutes=5),
    step_max_age: timedelta = timedelta(minutes=15),
) -> tuple[dict[str, Any] | None, bool]:
    """Return a recent snapshot and whether its step aggregate is current.

    The collector fetch time describes when the response was observed. The
    step ``measured_at`` timestamp describes the last contributing sample.
    They are validated separately so a new fetch cannot make an old sample
    appear newly measured.

    Args:
        value: Snapshot returned by the collector for the current day.
        now: Current timezone-aware local time.
        fetch_max_age: Maximum age of the completed collector read.
        step_max_age: Maximum age of the latest step sample.

    Returns:
        A defensive copy with stale steps removed, plus whether steps remain.
    """
    if not isinstance(value, dict) or now.tzinfo is None or now.utcoffset() is None:
        return None, False
    if (
        value.get("date") != now.date().isoformat()
        or value.get("source") != "OPPO 健康云端"
    ):
        return None, False

    try:
        fetched_at = datetime.fromisoformat(
            str(value.get("fetched_at", "")).replace("Z", "+00:00")
        )
    except ValueError:
        return None, False
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        return None, False
    fetch_age = now.astimezone(fetched_at.tzinfo) - fetched_at
    if fetch_age < timedelta(seconds=-30) or fetch_age > fetch_max_age:
        return None, False

    current = deepcopy(value)
    metrics = current.get("metrics")
    if not isinstance(metrics, dict):
        current["metrics"] = {}
        return current, False
    steps = metrics.get("steps")
    if (
        not isinstance(steps, dict)
        or type(steps.get("total")) not in (int, float)
        or not math.isfinite(steps["total"])
        or steps["total"] < 0
    ):
        metrics.pop("steps", None)
        return current, False
    try:
        measured_at = datetime.fromisoformat(
            str(steps.get("measured_at", "")).replace("Z", "+00:00")
        )
    except ValueError:
        metrics.pop("steps", None)
        return current, False
    if measured_at.tzinfo is None or measured_at.utcoffset() is None:
        metrics.pop("steps", None)
        return current, False
    sample_age = now.astimezone(measured_at.tzinfo) - measured_at
    if (
        measured_at.date() != now.astimezone(measured_at.tzinfo).date()
        or sample_age < timedelta(seconds=-30)
        or sample_age > step_max_age
        or measured_at > fetched_at + timedelta(minutes=1)
    ):
        metrics.pop("steps", None)
        return current, False
    return current, True
