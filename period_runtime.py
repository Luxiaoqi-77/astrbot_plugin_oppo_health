"""Validated, score-free durable state for period inquiries and confirmation.

Only stable cycle/event IDs, local dates, timestamps, and dispatch outcomes are
stored. Incoming message text and OPPO health values are never persisted here.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import hashlib
import re
from typing import Any
from zoneinfo import ZoneInfo


_ID = re.compile(r"^[0-9a-f]{64}$")
_INQUIRY_STATUS = {"reserved", "handed_off", "failed", "failed_uncertain", "skipped"}
_DELIVERY_STATUS = {"pending", "sent", "failed_not_sent", "unknown"}
_CONFIRMATION_STATUS = {"sync_claimed", "sync_running", "sync_complete", "sync_unavailable"}


def _iso_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == value else None


def _aware(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def cycle_id(start_date: str) -> str:
    if _iso_date(start_date) is None:
        raise ValueError("invalid period cycle date")
    return hashlib.sha256(f"oppo-calendar-cycle-v1\0{start_date}".encode()).hexdigest()


def confirmation_id(cycle: str, confirmed_at: str) -> str:
    if not _ID.fullmatch(cycle) or not _aware(confirmed_at):
        raise ValueError("invalid period confirmation metadata")
    return hashlib.sha256(f"oppo-period-confirm-v1\0{cycle}\0{confirmed_at}".encode()).hexdigest()


def empty_period_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "active_cycle": None,
        "inquiries": [],
        "confirmation": None,
        "postcare_cycle_ids": [],
    }


def validate_period_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "active_cycle", "inquiries", "confirmation", "postcare_cycle_ids"
    } or value.get("schema_version") != 1 or isinstance(value.get("schema_version"), bool):
        raise ValueError("period runtime state shape is unsupported")

    active = value.get("active_cycle")
    if active is not None and (
        not isinstance(active, dict)
        or set(active) != {"cycle_id", "start_date", "last_seen_at"}
        or not isinstance(active.get("cycle_id"), str) or not _ID.fullmatch(active["cycle_id"])
        or _iso_date(active.get("start_date")) is None
        or cycle_id(active["start_date"]) != active["cycle_id"]
        or not _aware(active.get("last_seen_at"))
    ):
        raise ValueError("period active cycle is invalid")

    inquiries = value.get("inquiries")
    if not isinstance(inquiries, list) or len(inquiries) > 365:
        raise ValueError("period inquiry history is invalid")
    normalized_inquiries = []
    seen_events = set()
    for row in inquiries:
        fields = {
            "event_id", "cycle_id", "start_date", "date", "period_day", "status", "updated_at",
            "delivery_status", "delivery_at",
        }
        legacy_fields = fields - {"delivery_status", "delivery_at"}
        if isinstance(row, dict) and set(row) == legacy_fields:
            row = {**row, "delivery_status": "unknown", "delivery_at": row.get("updated_at")}
        if (
            not isinstance(row, dict) or set(row) != fields
            or not isinstance(row.get("event_id"), str) or not _ID.fullmatch(row["event_id"])
            or row["event_id"] in seen_events
            or not isinstance(row.get("cycle_id"), str) or not _ID.fullmatch(row["cycle_id"])
            or _iso_date(row.get("start_date")) is None
            or cycle_id(row["start_date"]) != row["cycle_id"]
            or _iso_date(row.get("date")) is None
            or isinstance(row.get("period_day"), bool) or row.get("period_day") not in {6, 7, 8}
            or (date.fromisoformat(row["date"]) - date.fromisoformat(row["start_date"])).days + 1 != row["period_day"]
            or row.get("status") not in _INQUIRY_STATUS
            or not _aware(row.get("updated_at"))
            or row.get("delivery_status") not in _DELIVERY_STATUS
            or (row.get("delivery_status") == "pending" and row.get("delivery_at") is not None)
            or (row.get("delivery_status") != "pending" and not _aware(row.get("delivery_at")))
        ):
            raise ValueError("period inquiry row is invalid")
        seen_events.add(row["event_id"])
        normalized_inquiries.append(deepcopy(row))

    confirmation = value.get("confirmation")
    if confirmation is not None:
        fields = {
            "event_id", "cycle_id", "start_date", "inquiry_date", "confirmed_at",
            "status", "sync_attempt_id", "updated_at", "end_date", "postcare_event_id",
        }
        if (
            not isinstance(confirmation, dict) or set(confirmation) != fields
            or not isinstance(confirmation.get("event_id"), str) or not _ID.fullmatch(confirmation["event_id"])
            or not isinstance(confirmation.get("cycle_id"), str) or not _ID.fullmatch(confirmation["cycle_id"])
            or _iso_date(confirmation.get("start_date")) is None
            or cycle_id(confirmation["start_date"]) != confirmation["cycle_id"]
            or _iso_date(confirmation.get("inquiry_date")) is None
            or not _aware(confirmation.get("confirmed_at"))
            or confirmation.get("status") not in _CONFIRMATION_STATUS
            or confirmation.get("sync_attempt_id") != confirmation.get("event_id")
            or not _aware(confirmation.get("updated_at"))
            or (confirmation.get("end_date") is not None and _iso_date(confirmation.get("end_date")) is None)
            or (confirmation.get("postcare_event_id") is not None
                and (not isinstance(confirmation.get("postcare_event_id"), str)
                     or not _ID.fullmatch(confirmation["postcare_event_id"])))
        ):
            raise ValueError("period confirmation state is invalid")

    postcare_ids = value.get("postcare_cycle_ids")
    if (not isinstance(postcare_ids, list) or len(postcare_ids) > 365
            or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in postcare_ids)
            or len(set(postcare_ids)) != len(postcare_ids)):
        raise ValueError("period post-care deduplication state is invalid")
    return {
        "schema_version": 1,
        "active_cycle": deepcopy(active),
        "inquiries": normalized_inquiries,
        "confirmation": deepcopy(confirmation),
        "postcare_cycle_ids": list(postcare_ids),
    }


def reserve_inquiry(state: object, fact: object, now: datetime) -> tuple[dict[str, Any], bool]:
    normalized = validate_period_state(state)
    if not isinstance(fact, dict) or now.tzinfo is None or now.utcoffset() is None:
        return normalized, False
    start_date = _iso_date(fact.get("start_date"))
    inquiry_date = _iso_date(fact.get("target_date"))
    event_id = fact.get("event_id")
    period_day = fact.get("period_day")
    if (
        start_date is None or inquiry_date is None
        or (date.fromisoformat(inquiry_date) - date.fromisoformat(start_date)).days + 1 not in {6, 7, 8}
        or isinstance(period_day, bool) or period_day not in {6, 7, 8}
        or (date.fromisoformat(inquiry_date) - date.fromisoformat(start_date)).days + 1 != period_day
        or not isinstance(event_id, str) or not _ID.fullmatch(event_id)
    ):
        return normalized, False
    identity = cycle_id(start_date)
    existing_confirmation = normalized.get("confirmation")
    if isinstance(existing_confirmation, dict) and existing_confirmation.get("cycle_id") == identity:
        return normalized, False
    if any(row["event_id"] == event_id or (
        row["cycle_id"] == identity and row["date"] == inquiry_date
    ) for row in normalized["inquiries"]):
        return normalized, False
    normalized["active_cycle"] = {
        "cycle_id": identity,
        "start_date": start_date,
        "last_seen_at": now.isoformat(),
    }
    normalized["inquiries"].append({
        "event_id": event_id,
        "cycle_id": identity,
        "start_date": start_date,
        "date": inquiry_date,
        "period_day": period_day,
        "status": "reserved",
        "updated_at": now.isoformat(),
        "delivery_status": "pending",
        "delivery_at": None,
    })
    normalized["inquiries"] = normalized["inquiries"][-365:]
    return normalized, True


def mark_inquiry_outcome(state: object, event_id: str, outcome: str, now: datetime) -> dict[str, Any]:
    normalized = validate_period_state(state)
    if outcome not in _INQUIRY_STATUS or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("invalid inquiry outcome")
    for row in normalized["inquiries"]:
        if row["event_id"] == event_id:
            if row["status"] != "reserved":
                return normalized
            row["status"] = outcome
            row["updated_at"] = now.isoformat()
            break
    return normalized


def mark_inquiry_delivery(state: object, event_id: str, outcome: str, now: datetime) -> dict[str, Any]:
    """Record only an actual transport response for a pending inquiry event."""
    normalized = validate_period_state(state)
    if outcome not in {"sent", "failed_not_sent", "unknown"} or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("invalid inquiry transport result")
    for row in normalized["inquiries"]:
        if row["event_id"] != event_id:
            continue
        if row["delivery_status"] != "pending":
            return normalized
        row["delivery_status"] = outcome
        row["delivery_at"] = now.isoformat()
        row["updated_at"] = now.isoformat()
        break
    return normalized


def claim_confirmation(state: object, inquiry_event_id: str, confirmed_at: datetime) -> tuple[dict[str, Any], dict[str, str] | None]:
    normalized = validate_period_state(state)
    if confirmed_at.tzinfo is None or confirmed_at.utcoffset() is None:
        return normalized, None
    inquiry = next((row for row in reversed(normalized["inquiries"])
                    if row["event_id"] == inquiry_event_id), None)
    active = normalized.get("active_cycle")
    if (inquiry is None or not isinstance(active, dict)
            or active.get("cycle_id") != inquiry["cycle_id"]
            or inquiry["delivery_status"] != "sent"
            or datetime.fromisoformat(inquiry["delivery_at"]).astimezone(confirmed_at.tzinfo) > confirmed_at):
        return normalized, None
    if normalized.get("confirmation") is not None and normalized["confirmation"]["cycle_id"] == inquiry["cycle_id"]:
        return normalized, None
    confirmed_local = confirmed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    if confirmed_local.isoformat() != inquiry["date"]:
        return normalized, None
    identity = confirmation_id(inquiry["cycle_id"], confirmed_at.isoformat())
    confirmation = {
        "event_id": identity,
        "cycle_id": inquiry["cycle_id"],
        "start_date": inquiry["start_date"],
        "inquiry_date": inquiry["date"],
        "confirmed_at": confirmed_at.isoformat(),
        "status": "sync_claimed",
        "sync_attempt_id": identity,
        "updated_at": confirmed_at.isoformat(),
        "end_date": None,
        "postcare_event_id": None,
    }
    normalized["confirmation"] = confirmation
    return normalized, {
        "event_id": identity,
        "cycle_id": inquiry["cycle_id"],
        "start_date": inquiry["start_date"],
        "inquiry_date": inquiry["date"],
        "confirmed_at": confirmed_at.isoformat(),
    }


def settle_confirmation(
    state: object, confirmation_event_id: str, now: datetime, *,
    end_date: str | None = None, postcare_event_id: str | None = None,
) -> dict[str, Any]:
    normalized = validate_period_state(state)
    current = normalized.get("confirmation")
    if current is None or current["event_id"] != confirmation_event_id:
        return normalized
    if current.get("status") not in {"sync_claimed", "sync_running"}:
        return normalized
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("settlement timestamp must be timezone-aware")
    if end_date is None:
        current["status"] = "sync_unavailable"
        current["end_date"] = None
        current["postcare_event_id"] = None
    else:
        if _iso_date(end_date) is None or (postcare_event_id is not None and not _ID.fullmatch(postcare_event_id)):
            raise ValueError("invalid confirmation sync result")
        current["status"] = "sync_complete"
        current["end_date"] = end_date
        current["postcare_event_id"] = postcare_event_id
        if postcare_event_id and current["cycle_id"] not in normalized["postcare_cycle_ids"]:
            normalized["postcare_cycle_ids"].append(current["cycle_id"])
            normalized["postcare_cycle_ids"] = normalized["postcare_cycle_ids"][-365:]
    current["updated_at"] = now.isoformat()
    return normalized
