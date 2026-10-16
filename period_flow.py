"""Fail-closed period interval and confirmation logic for isolated candidates.

This module is side-effect free. It never reads OPPO, stores conversation text,
or writes either OPPO records or bot state.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
import hashlib
import re
import unicodedata
from typing import Any
from zoneinfo import ZoneInfo


_TARGET_UMO_PATTERN = re.compile(r"^.+:FriendMessage:\d+$")
_QUOTE_MARKERS = set("\"'`“”‘’「」『』")
_END_CONFIRMATIONS = {
    "结束了", "已经结束", "已经结束了", "是", "是的", "对", "对的", "嗯", "嗯嗯",
    "是的结束了", "对的结束了", "嗯结束了", "嗯嗯结束了",
    "经期结束了", "经期已经结束", "经期已经结束了", "我的经期结束了",
    "我的经期已经结束了", "这次经期结束了", "这次经期已经结束了",
    "月经结束了", "月经已经结束", "月经已经结束了", "我的月经结束了",
    "我的月经已经结束了", "例假结束了", "例假已经结束", "例假已经结束了",
    "我的例假已经结束了",
}


def _as_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _month_calendar(snapshot: object) -> tuple[set[date], set[date]] | None:
    if not isinstance(snapshot, dict):
        return None
    if snapshot.get("source") != "oppo_health_ui_ocr":
        return None
    month = snapshot.get("period")
    metrics = snapshot.get("metrics")
    if not isinstance(month, str) or not re.fullmatch(r"20\d{2}-\d{2}", month):
        return None
    if not isinstance(metrics, dict) or metrics.get("legend_verified") is not True:
        return None
    try:
        year, month_number = (int(part) for part in month.split("-"))
        days_in_month = monthrange(year, month_number)[1]
    except (ValueError, TypeError):
        return None
    expected_dates = {
        date(year, month_number, day) for day in range(1, days_in_month + 1)
    }
    if metrics.get("classification_complete") is not True:
        return None
    if metrics.get("calendar_coverage") != f"{days_in_month}/{days_in_month}":
        return None
    if metrics.get("classification_coverage") != f"{days_in_month}/{days_in_month}":
        return None

    def valid_dates(field: str) -> set[date] | None:
        raw = metrics.get(field)
        if not isinstance(raw, list):
            return None
        parsed = {_as_date(value) for value in raw}
        if None in parsed:
            return None
        values = {value for value in parsed if value is not None}
        if not values <= expected_dates or len(values) != len(raw):
            return None
        return values

    actual = valid_dates("period_dates")
    non_period = valid_dates("known_non_period_dates")
    classified = valid_dates("classified_dates")
    predicted = valid_dates("predicted_period_dates")
    if any(values is None for values in (actual, non_period, classified, predicted)):
        return None
    assert actual is not None and non_period is not None and classified is not None and predicted is not None
    if actual & non_period or actual & predicted or not predicted <= non_period:
        return None
    if classified != actual | non_period:
        return None
    if classified != expected_dates:
        return None
    return actual, non_period


def period_clusters(calendar_months: list[object]) -> list[dict[str, Any]]:
    """Return actual-date clusters with explicitly classified boundaries.

    Every contributing month must have a verified legend and complete day
    classification. A missing adjacent month leaves that boundary unknown.
    """
    actual: set[date] = set()
    non_period: set[date] = set()
    for snapshot in calendar_months:
        classified = _month_calendar(snapshot)
        if classified is None:
            continue
        month_actual, month_non_period = classified
        actual.update(month_actual)
        non_period.update(month_non_period)
    if not actual or actual & non_period:
        return []

    runs: list[list[date]] = []
    for current in sorted(actual):
        if not runs or current != runs[-1][-1] + timedelta(days=1):
            runs.append([current])
        else:
            runs[-1].append(current)
    return [
        {
            "start_date": run[0].isoformat(),
            "end_date": run[-1].isoformat(),
            "start_confirmed": run[0] - timedelta(days=1) in non_period,
            "end_boundary_classified": run[-1] + timedelta(days=1) in non_period,
            "date_precision": "date",
            "source": "oppo_health_ui_ocr",
        }
        for run in runs
    ]


def calendar_months_from_capture(capture: object) -> list[dict[str, Any]]:
    """Extract only successful Calendar snapshots; supports complete month batches."""
    pages = capture.get("pages", {}) if isinstance(capture, dict) else {}
    if not isinstance(pages, dict):
        return []
    snapshots = []
    current = pages.get("cycle_calendar")
    if isinstance(current, dict) and current.get("status") == "ok" and isinstance(current.get("data"), dict):
        snapshots.append(current["data"])
    batch = pages.get("cycle_calendar_months")
    if isinstance(batch, list):
        for item in batch:
            if isinstance(item, dict) and item.get("status") == "ok" and isinstance(item.get("data"), dict):
                snapshots.append(item["data"])
    by_month = {}
    for snapshot in snapshots:
        period = snapshot.get("period")
        if isinstance(period, str):
            by_month[period] = snapshot
    return [by_month[key] for key in sorted(by_month)]


def current_period_cluster(calendar_months: list[object], today: date) -> tuple[dict[str, Any], int] | None:
    """Return the newest fully bounded start cluster and its local cycle day."""
    eligible = []
    for cluster in period_clusters(calendar_months):
        start = _as_date(cluster.get("start_date"))
        if start is None or cluster.get("start_confirmed") is not True or start > today:
            continue
        eligible.append((start, cluster, (today - start).days + 1))
    if not eligible:
        return None
    start, cluster, day = max(eligible, key=lambda item: item[0])
    return cluster, day


def _period_event_id(kind: str, *dates: str) -> str:
    payload = "\0".join(("oppo_health_ui_ocr", kind, *dates))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def period_cycle_id(start_date: str) -> str:
    if _as_date(start_date) is None:
        raise ValueError("invalid period start date")
    return hashlib.sha256(f"oppo-calendar-cycle-v1\0{start_date}".encode("utf-8")).hexdigest()


def start_care_fact(calendar_months: list[object], today: date) -> dict[str, Any] | None:
    """Build a D1–D3 fact from a completely bounded actual date cluster."""
    current = current_period_cluster(calendar_months, today)
    if current is None:
        return None
    cluster, day = current
    if day not in {1, 2, 3}:
        return None
    start_text = cluster["start_date"]
    return {
        "event_id": _period_event_id("period-start", start_text),
        "confirmed": True,
        "event_date": start_text,
        "date_precision": "date",
        "timezone": "Asia/Shanghai",
        "target_date": today.isoformat(),
        "stage": day,
        "basis": "continuous_actual_period_cluster_with_classified_predecessor",
    }


def period_end_inquiry_fact(
    calendar_months: list[object],
    today: date,
    *,
    confirmed_end_start_dates: set[str] | None = None,
    already_asked_dates: set[str] | None = None,
) -> dict[str, Any] | None:
    """Create one idempotent daily inquiry fact on D6–D8 only."""
    ended_starts = confirmed_end_start_dates or set()
    asked_dates = already_asked_dates or set()
    current = current_period_cluster(calendar_months, today)
    if current is None:
        return None
    cluster, day = current
    start_text = cluster["start_date"]
    if day not in {6, 7, 8} or start_text in ended_starts or today.isoformat() in asked_dates:
        return None
    today_text = today.isoformat()
    return {
        "event_id": _period_event_id("period-end-inquiry", start_text, today_text),
        "kind": "period_end_inquiry",
        "start_date": start_text,
        "target_date": today_text,
        "period_day": day,
        "once_per_date": True,
    }


def explicit_end_confirmation(
    message_text: object,
    *,
    event_umo: object,
    target_umo: object,
    plain_private_message: bool,
    inquiry_open: bool,
    period_day: int,
) -> bool:
    """Accept only a short affirmative reply in the exact pending private DM."""
    if (
        not plain_private_message
        or not inquiry_open
        or period_day not in {6, 7, 8}
        or not isinstance(target_umo, str)
        or _TARGET_UMO_PATTERN.fullmatch(target_umo) is None
        or event_umo != target_umo
        or not isinstance(message_text, str)
        or any(marker in message_text for marker in _QUOTE_MARKERS)
        or "引用" in message_text
        or "转述" in message_text
        or message_text.lstrip().startswith(">")
    ):
        return False
    normalized = unicodedata.normalize("NFKC", message_text).strip()
    normalized = re.sub(r"[\s，,。.!！?？、]+", "", normalized)
    normalized = re.sub(r"\s+", "", normalized)
    return normalized in _END_CONFIRMATIONS


def build_end_confirmation_request(
    message_text: object,
    *,
    event_umo: object,
    target_umo: object,
    plain_private_message: bool,
    inquiry_open: bool,
    period_day: int,
    period_start_date: str,
    confirmed_at: str,
) -> dict[str, Any] | None:
    """Reduce a qualifying reply to a minimal local sync token, without text."""
    start = _as_date(period_start_date)
    try:
        confirmed_dt = datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if (
        not explicit_end_confirmation(
            message_text,
            event_umo=event_umo,
            target_umo=target_umo,
            plain_private_message=plain_private_message,
            inquiry_open=inquiry_open,
            period_day=period_day,
        )
        or start is None
        or confirmed_dt.tzinfo is None
        or confirmed_dt.utcoffset() is None
        or (confirmed_dt.astimezone(ZoneInfo("Asia/Shanghai")).date() - start).days + 1
        not in {6, 7, 8}
    ):
        return None
    return {
        "confirmed": True,
        "period_start_date": start.isoformat(),
        "confirmed_at": confirmed_dt.isoformat(),
        "confirmation_source": "target_private_plain_reply",
    }


def confirmed_end_record(
    calendar_months: list[object],
    confirmation: object,
    *,
    sync_observed_at: object,
) -> dict[str, Any] | None:
    """Create a bot-local date-only end record after a fresh post-confirmation sync."""
    if not isinstance(confirmation, dict) or confirmation.get("confirmed") is not True:
        return None
    start_text = confirmation.get("period_start_date")
    confirmed_at = confirmation.get("confirmed_at")
    start = _as_date(start_text)
    if start is None or not isinstance(confirmed_at, str) or not isinstance(sync_observed_at, str):
        return None
    try:
        confirmed_dt = datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
        observed_dt = datetime.fromisoformat(sync_observed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if (confirmed_dt.tzinfo is None or confirmed_dt.utcoffset() is None
            or observed_dt.tzinfo is None or observed_dt.utcoffset() is None
            or observed_dt < confirmed_dt):
        return None
    confirmed_local_day = confirmed_dt.astimezone(ZoneInfo("Asia/Shanghai")).date()
    sync_local_day = observed_dt.astimezone(ZoneInfo("Asia/Shanghai")).date()
    if (confirmed_local_day - start).days + 1 not in {6, 7, 8}:
        return None

    for cluster in reversed(period_clusters(calendar_months)):
        if cluster.get("start_date") != start.isoformat():
            continue
        if cluster.get("start_confirmed") is not True or cluster.get("end_boundary_classified") is not True:
            return None
        end_text = cluster["end_date"]
        if _as_date(end_text) is None or _as_date(end_text) > sync_local_day:
            return None
        return {
            "event_id": _period_event_id("period-end", start.isoformat(), end_text),
            "cycle_id": period_cycle_id(start.isoformat()),
            "confirmed": True,
            "period_start_date": start.isoformat(),
            "event_date": end_text,
            "date_precision": "date",
            "timezone": "Asia/Shanghai",
            "confirmation_source": "target_private_plain_reply",
            "confirmed_at": confirmed_dt.isoformat(),
            "observed_at": observed_dt.isoformat(),
            "target_date": confirmed_local_day.isoformat(),
            "end_boundary_classified": True,
            "bot_local_only": True,
        }
    return None


def post_period_care_fact(end_record: object) -> dict[str, Any] | None:
    """Return one idempotent post-period fact from a verified local end record."""
    if (
        not isinstance(end_record, dict)
        or end_record.get("confirmed") is not True
        or end_record.get("bot_local_only") is not True
        or end_record.get("date_precision") != "date"
        or _as_date(end_record.get("event_date")) is None
        or not isinstance(end_record.get("event_id"), str)
    ):
        return None
    end_date = _as_date(end_record["event_date"])
    assert end_date is not None
    target_date = _as_date(end_record.get("target_date")) or end_date
    return {
        "event_id": _period_event_id("post-period-care", end_record["event_id"]),
        "cycle_id": end_record.get("cycle_id"),
        "start_date": end_record.get("period_start_date"),
        "event_date": end_date.isoformat(),
        "target_date": target_date.isoformat(),
        "observed_at": end_record.get("observed_at"),
        "confirmed_at": end_record.get("confirmed_at"),
        "confirmed": True,
        "confirmation_source": "target_private_plain_reply",
        "bot_local_only": True,
        "date_precision": "date",
        "once_per_cycle": True,
        "once_per_end_record": True,
    }
