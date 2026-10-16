"""Fail-closed freshness evidence for health contexts.

Capture time and measurement/event time are deliberately kept separate. This
module contains only pure validation; it does not read a device or network.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import math
import asyncio
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


FETCH_MAX_AGE = timedelta(minutes=5)
CURRENT_STEPS_MAX_AGE = timedelta(minutes=15)
DATA_REFRESH_RETRY_DELAYS = (5.0, 15.0, 30.0)


async def retry_data_refresh(fetch, should_continue):
    """Retry only a failed data read with bounded backoff and a fresh gate.

    The caller owns the opportunity and must leave it pending when this helper
    returns ``None``. ``should_continue`` is checked before every read and
    after each delay so a quiet period or expired window cancels the retry.
    This helper must never wrap model generation or message delivery.
    """
    for attempt in range(len(DATA_REFRESH_RETRY_DELAYS) + 1):
        if not should_continue():
            return None
        try:
            value = await fetch()
        except (OSError, asyncio.TimeoutError, ValueError, TypeError):
            value = None
        if value is not None:
            return value
        if attempt == len(DATA_REFRESH_RETRY_DELAYS):
            break
        await asyncio.sleep(DATA_REFRESH_RETRY_DELAYS[attempt])
    return None


async def retry_fresh_context(fetch, build_context, should_continue):
    """Fetch source data anew for each try and build only from a successful read."""
    for attempt in range(len(DATA_REFRESH_RETRY_DELAYS) + 1):
        if not should_continue():
            return None
        try:
            source_value = await fetch()
        except (OSError, asyncio.TimeoutError, ValueError, TypeError):
            source_value = None
        if source_value is not None:
            context = build_context(source_value)
            if context is not None and context != "":
                return context
        if attempt == len(DATA_REFRESH_RETRY_DELAYS):
            break
        await asyncio.sleep(DATA_REFRESH_RETRY_DELAYS[attempt])
    return None


def parse_aware(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _finite_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def capture_is_fresh(
    capture: object,
    fields: list[str] | tuple[str, ...],
    now: datetime,
    *,
    max_age: timedelta = FETCH_MAX_AGE,
) -> bool:
    """Require a successful current OCR capture for every requested page."""
    if not isinstance(capture, dict) or now.tzinfo is None or now.utcoffset() is None:
        return False
    pages = capture.get("pages")
    if not isinstance(pages, dict):
        return False
    for field in fields:
        page = pages.get(field)
        data = page.get("data") if isinstance(page, dict) else None
        observed = parse_aware(data.get("observed_at")) if isinstance(data, dict) else None
        if (
            not isinstance(page, dict)
            or page.get("status") != "ok"
            or not isinstance(data, dict)
            or data.get("source") != "oppo_health_ui_ocr"
            or data.get("page") != field
            or observed is None
            or observed > now
            or now - observed > max_age
        ):
            return False
    return True


def capture_observation_receipt(
    capture: object,
    fields: list[str] | tuple[str, ...],
) -> str | None:
    """Return the oldest requested page observation, without refreshing it."""
    if not isinstance(capture, dict) or not fields:
        return None
    pages = capture.get("pages")
    if not isinstance(pages, dict):
        return None
    observed = []
    for field in fields:
        page = pages.get(field)
        data = page.get("data") if isinstance(page, dict) else None
        timestamp = parse_aware(data.get("observed_at")) if isinstance(data, dict) else None
        if timestamp is None:
            return None
        observed.append(timestamp)
    return min(observed).isoformat()


def source_receipts_are_fresh(
    receipts: object,
    now: datetime,
    *,
    max_age: timedelta = FETCH_MAX_AGE,
) -> bool:
    """Validate per-source observation timestamps bound to one pending turn."""
    if (
        not isinstance(receipts, dict)
        or not receipts
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        return False
    for source, value in receipts.items():
        observed = parse_aware(value)
        if (
            source not in {"cloud", "local_capture"}
            or observed is None
            or observed > now
            or now - observed > max_age
        ):
            return False
    return True


def measurement_receipts_are_fresh(
    receipts: object,
    now: datetime,
) -> bool:
    """Recheck sample-level freshness at the point context reaches an LLM."""
    if receipts is None:
        return True
    if not isinstance(receipts, dict) or not receipts or now.tzinfo is None or now.utcoffset() is None:
        return False
    for field, value in receipts.items():
        if field == "steps":
            measured = parse_aware(value)
            max_age = CURRENT_STEPS_MAX_AGE
            if measured is None or measured.astimezone(now.tzinfo).date() != now.date():
                return False
        elif field == "wellness" and isinstance(value, dict) and set(value) == {
            "measured_at", "max_age_minutes", "data_date", "timezone"
        }:
            measured = parse_aware(value.get("measured_at"))
            age_minutes = value.get("max_age_minutes")
            if type(age_minutes) is not int or not 1 <= age_minutes <= 1440:
                return False
            try:
                zone = ZoneInfo(value.get("timezone"))
                data_date = datetime.strptime(value.get("data_date"), "%Y-%m-%d").date()
            except (TypeError, ValueError, ZoneInfoNotFoundError):
                return False
            if (
                measured is None
                or measured.astimezone(zone).date() != data_date
                or now.astimezone(zone).date() != data_date
            ):
                return False
            max_age = timedelta(minutes=age_minutes)
        else:
            return False
        if measured is None or measured > now or now - measured > max_age:
            return False
    return True


def current_cloud_snapshot(
    value: Any,
    now: datetime,
    *,
    fetch_max_age: timedelta = FETCH_MAX_AGE,
    steps_max_age: timedelta = CURRENT_STEPS_MAX_AGE,
) -> tuple[dict[str, Any] | None, bool]:
    """Validate the fetch and remove values that lack reliable time evidence.

    A daily step total is current only while its latest contributing sample is
    at most 15 minutes old. Other metrics may remain as dated latest records;
    their timestamps must be preserved when rendered to the model.
    """
    if not isinstance(value, dict) or now.tzinfo is None or now.utcoffset() is None:
        return None, False
    if value.get("date") != now.date().isoformat() or value.get("source") != "OPPO 健康云端":
        return None, False
    fetched = parse_aware(value.get("fetched_at"))
    if fetched is None or fetched > now or now - fetched > fetch_max_age:
        return None, False
    errors = value.get("errors")
    if not isinstance(errors, dict):
        return None, False
    result = deepcopy(value)
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        result["metrics"] = {}
        return result, False

    steps = metrics.get("steps")
    if isinstance(steps, dict):
        measured = parse_aware(steps.get("measured_at"))
        total = steps.get("total")
        if (
            "steps" in errors
            or not _finite_number(total)
            or total < 0
            or measured is None
            or measured > now
            or measured > fetched
            or measured.astimezone(now.tzinfo).date() != now.date()
            or now - measured > steps_max_age
        ):
            metrics.pop("steps", None)

    for name, value_key in (("heart_rate", "latest"), ("blood_oxygen", "latest")):
        metric = metrics.get(name)
        if isinstance(metric, dict):
            measured = parse_aware(metric.get("measured_at"))
            if (
                name in errors
                or not _finite_number(metric.get(value_key))
                or measured is None
                or measured > now
                or measured > fetched
            ):
                metrics.pop(name, None)

    heart = metrics.get("heart_rate")
    if isinstance(heart, dict) and "resting" in heart:
        resting_at = parse_aware(heart.get("resting_measured_at"))
        if resting_at is None or resting_at > now or resting_at > fetched:
            heart.pop("resting", None)
            heart.pop("resting_measured_at", None)
    return result, bool(metrics)
