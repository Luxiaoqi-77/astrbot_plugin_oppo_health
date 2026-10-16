"""Metadata-only, at-most-once daily activity-care delivery ledger.

This module is intentionally pure so scheduling and retry semantics can be
reviewed offline. It stores no health values, prompt text, or conversation IDs.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = 1
RETENTION_DAYS = 30
MAX_ATTEMPTS = 2  # initial dispatch plus one confirmed-not-sent retry
RETRY_COOLDOWN = timedelta(minutes=5)
WINDOW_START_HOUR = 14
WINDOW_END_HOUR = 21


def _local(at: datetime) -> datetime:
    if not isinstance(at, datetime) or at.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return at.astimezone(TZ)


def empty_ledger(now: datetime) -> dict[str, Any]:
    local = _local(now)
    return {"schema_version": SCHEMA_VERSION, "date": local.date().isoformat(), "days": {}}


def prepare_ledger(value: Any, now: datetime) -> dict[str, Any]:
    """Validate the metadata envelope and prune entries older than 30 days."""
    local = _local(now)
    if value is None:
        return empty_ledger(local)
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or not isinstance(value.get("days"), dict)):
        raise ValueError("invalid random-activity ledger")
    days: dict[str, Any] = {}
    cutoff = local.date() - timedelta(days=RETENTION_DAYS - 1)
    for day, entry in value["days"].items():
        if (not isinstance(day, str) or len(day) != 10 or not isinstance(entry, dict)
                or set(entry) != {"target_at", "status", "attempt_count", "sent_count",
                                  "active_attempt_id", "retry_after", "unknown_at",
                                  "reason", "attempts"}):
            raise ValueError("invalid random-activity day entry")
        try:
            parsed_day = datetime.fromisoformat(day).date()
        except ValueError as exc:
            raise ValueError("invalid random-activity date") from exc
        if parsed_day < cutoff or parsed_day > local.date() + timedelta(days=1):
            continue
        if (entry["status"] not in {"ready", "in_flight", "failed_not_sent", "unknown", "sent", "suppressed"}
                or type(entry["attempt_count"]) is not int or not 0 <= entry["attempt_count"] <= MAX_ATTEMPTS
                or type(entry["sent_count"]) is not int or entry["sent_count"] not in (0, 1)
                or not isinstance(entry["attempts"], list) or len(entry["attempts"]) > MAX_ATTEMPTS
                or entry["attempt_count"] != len(entry["attempts"])
                or not _valid_iso(entry["target_at"])
                or (entry["active_attempt_id"] is not None and not _valid_id(entry["active_attempt_id"]))
                or (entry["retry_after"] is not None and not _valid_iso(entry["retry_after"]))
                or (entry["unknown_at"] is not None and not _valid_iso(entry["unknown_at"]))
                or (entry["reason"] is not None and (not isinstance(entry["reason"], str) or len(entry["reason"]) > 80))):
            raise ValueError("invalid random-activity ledger fields")
        for attempt in entry["attempts"]:
            if (not isinstance(attempt, dict) or set(attempt) != {"id", "at", "outcome", "transport_calls", "receipt_ref", "verification_ref"}
                    or not _valid_id(attempt["id"]) or not _valid_iso(attempt["at"])
                    or attempt["outcome"] not in {"in_flight", "failed_not_sent", "unknown", "sent", "suppressed"}
                    or type(attempt["transport_calls"]) is not int or not 0 <= attempt["transport_calls"] <= 100
                    or any(v is not None and (not isinstance(v, str) or len(v) > 160)
                           for v in (attempt["receipt_ref"], attempt["verification_ref"]))):
                raise ValueError("invalid random-activity attempt")
        if entry["status"] == "in_flight":
            if (entry["active_attempt_id"] is None or not entry["attempts"]
                    or entry["attempts"][-1]["id"] != entry["active_attempt_id"]
                    or entry["attempts"][-1]["outcome"] != "in_flight"):
                raise ValueError("invalid in-flight random-activity entry")
        elif entry["active_attempt_id"] is not None:
            raise ValueError("inactive random-activity entry has an active attempt")
        if entry["sent_count"] != int(entry["status"] == "sent"):
            raise ValueError("random-activity sent count conflicts with status")
        days[day] = deepcopy(entry)
    return {"schema_version": SCHEMA_VERSION, "date": local.date().isoformat(), "days": days}


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def _valid_iso(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def eligible(ledger: dict[str, Any], now: datetime, target_at: datetime) -> tuple[bool, str]:
    local, target = _local(now), _local(target_at)
    if target.date() != local.date() or not WINDOW_START_HOUR <= target.hour < WINDOW_END_HOUR:
        return False, "outside_schedule"
    if local < target or local.hour >= WINDOW_END_HOUR:
        return False, "not_due_or_expired"
    entry = ledger["days"].get(local.date().isoformat())
    if entry is None:
        return True, "eligible"
    if entry["status"] == "ready":
        return True, "eligible"
    if entry["status"] == "failed_not_sent" and entry["attempt_count"] < MAX_ATTEMPTS:
        retry_after = datetime.fromisoformat(entry["retry_after"]) if entry["retry_after"] else None
        if retry_after is not None and local >= retry_after:
            return True, "retry_eligible"
        return False, "retry_cooldown"
    return False, entry["status"]


def reserve(ledger: dict[str, Any], now: datetime, target_at: datetime, attempt_id: str) -> tuple[bool, str]:
    local = _local(now)
    if not _valid_id(attempt_id):
        raise ValueError("invalid attempt ID")
    can_reserve, reason = eligible(ledger, local, target_at)
    if not can_reserve:
        return False, reason
    key = local.date().isoformat()
    entry = ledger["days"].get(key)
    if entry is None:
        entry = {"target_at": _local(target_at).isoformat(), "status": "ready", "attempt_count": 0,
                 "sent_count": 0, "active_attempt_id": None, "retry_after": None,
                 "unknown_at": None, "reason": None, "attempts": []}
        ledger["days"][key] = entry
    entry["status"] = "in_flight"
    entry["attempt_count"] += 1
    entry["active_attempt_id"] = attempt_id
    entry["retry_after"] = None
    entry["reason"] = None
    entry["attempts"].append({"id": attempt_id, "at": local.isoformat(), "outcome": "in_flight",
                              "transport_calls": 0, "receipt_ref": None, "verification_ref": None})
    return True, "reserved"


def finish(ledger: dict[str, Any], attempt_id: str, outcome: str, now: datetime,
           *, reason: str | None = None, transport_calls: int = 0,
           receipt_ref: str | None = None) -> tuple[bool, str]:
    local = _local(now)
    if outcome not in {"sent", "failed_not_sent", "unknown", "suppressed"}:
        raise ValueError("invalid outcome")
    day_entry = next((e for e in ledger["days"].values() if e.get("active_attempt_id") == attempt_id), None)
    if day_entry is None or day_entry["status"] != "in_flight":
        return False, "attempt_not_in_flight"
    attempt = day_entry["attempts"][-1]
    if (type(transport_calls) is not int or not 0 <= transport_calls <= 100
            or (receipt_ref is not None and (not isinstance(receipt_ref, str) or len(receipt_ref) > 160))):
        raise ValueError("invalid transport metadata")
    attempt.update(outcome=outcome, transport_calls=transport_calls, receipt_ref=receipt_ref)
    day_entry["status"] = outcome
    day_entry["active_attempt_id"] = None
    day_entry["reason"] = reason[:80] if isinstance(reason, str) else None
    if outcome == "sent":
        day_entry["sent_count"] = 1
        day_entry["retry_after"] = None
    elif outcome == "failed_not_sent" and day_entry["attempt_count"] < MAX_ATTEMPTS:
        day_entry["retry_after"] = (local + RETRY_COOLDOWN).isoformat()
    elif outcome == "unknown":
        day_entry["unknown_at"] = local.isoformat()
        day_entry["retry_after"] = None
    else:
        day_entry["retry_after"] = None
    return True, outcome


def defer_unstarted_refresh(ledger: dict[str, Any], attempt_id: str) -> bool:
    """Undo only a persisted reservation when fresh context failed before LLM use."""
    if not _valid_id(attempt_id):
        return False
    entry = next((row for row in ledger.get("days", {}).values()
                  if row.get("active_attempt_id") == attempt_id), None)
    if (not isinstance(entry, dict) or entry.get("status") != "in_flight"
            or not entry.get("attempts") or entry["attempts"][-1].get("id") != attempt_id
            or entry["attempts"][-1].get("outcome") != "in_flight"
            or entry["attempts"][-1].get("transport_calls") != 0):
        return False
    entry["attempts"].pop()
    entry.update(
        status="ready", attempt_count=len(entry["attempts"]), active_attempt_id=None,
        retry_after=None, unknown_at=None, reason="freshness_refresh_deferred",
    )
    return True


def recover_in_flight(ledger: dict[str, Any], now: datetime) -> int:
    """Convert interrupted attempts to unknown, never retry them blindly."""
    local = _local(now)
    count = 0
    for entry in ledger["days"].values():
        if entry["status"] != "in_flight":
            continue
        attempt = entry["attempts"][-1]
        attempt["outcome"] = "unknown"
        entry.update(status="unknown", active_attempt_id=None, unknown_at=local.isoformat(),
                     retry_after=None, reason="recovered_interrupted")
        count += 1
    return count


def reconcile_unknown(ledger: dict[str, Any], day: str, outcome: str, evidence_ref: str) -> bool:
    """Unlock only with explicit external evidence; evidence is an opaque ref."""
    if outcome not in {"sent", "failed_not_sent", "suppressed"}:
        raise ValueError("invalid reconciliation outcome")
    if not isinstance(evidence_ref, str) or not evidence_ref or len(evidence_ref) > 160:
        raise ValueError("reconciliation evidence reference required")
    entry = ledger["days"].get(day)
    if not isinstance(entry, dict) or entry["status"] != "unknown":
        return False
    attempt = entry["attempts"][-1]
    attempt["outcome"] = outcome
    attempt["verification_ref"] = evidence_ref
    entry["status"] = outcome
    entry["unknown_at"] = None
    if outcome == "sent":
        entry["sent_count"] = 1
        entry["retry_after"] = None
    elif outcome == "failed_not_sent" and entry["attempt_count"] < MAX_ATTEMPTS:
        base = datetime.fromisoformat(attempt["at"])
        entry["retry_after"] = (base + RETRY_COOLDOWN).isoformat()
    return True


def migrate_legacy_attempt(ledger: dict[str, Any], day: str, attempted: bool, now: datetime) -> bool:
    """A legacy attempted marker is ambiguous; migrate it to a retry lock."""
    if not attempted or day in ledger["days"]:
        return False
    local = _local(now)
    ledger["days"][day] = {"target_at": local.isoformat(), "status": "unknown", "attempt_count": 1,
                           "sent_count": 0, "active_attempt_id": None, "retry_after": None,
                           "unknown_at": local.isoformat(), "reason": "legacy_attempt_marker",
                           "attempts": [{"id": "0" * 32, "at": local.isoformat(), "outcome": "unknown",
                                         "transport_calls": 0, "receipt_ref": None, "verification_ref": None}]}
    return True


def suppress_day(ledger: dict[str, Any], now: datetime, target_at: datetime, reason: str) -> None:
    """Record an intentional skip so a late poll cannot catch up the message."""
    local = _local(now)
    key = local.date().isoformat()
    if key not in ledger["days"]:
        ledger["days"][key] = {"target_at": _local(target_at).isoformat(), "status": "suppressed",
                               "attempt_count": 0, "sent_count": 0, "active_attempt_id": None,
                               "retry_after": None, "unknown_at": None,
                               "reason": reason[:80], "attempts": []}


def suppress_freshness_failure(
    ledger: dict[str, Any], now: datetime, target_at: datetime, reason: str
) -> None:
    """Consume an unstarted daily slot when its required fresh read failed."""
    local = _local(now)
    key = local.date().isoformat()
    entry = ledger["days"].get(key)
    if entry is None:
        suppress_day(ledger, local, target_at, reason)
        return
    if entry["status"] in {"ready", "failed_not_sent"} and entry["active_attempt_id"] is None:
        entry.update(
            status="suppressed", retry_after=None, reason=reason[:80],
            unknown_at=None,
        )
