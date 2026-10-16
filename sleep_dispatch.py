"""Offline candidate for an at-most-once OPPO sleep-care dispatch ledger.

This module stores only scheduling and diagnostic metadata. It never accepts or
persists a health snapshot, duration, metric value, message text, or account ID.
The production caller must persist the returned ledger atomically before any
enqueue/send operation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import random
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_SNAPSHOT_AGE = timedelta(minutes=20)
STABILITY_WINDOW = timedelta(minutes=15)
LATE_GRACE = timedelta(minutes=30)
RETRY_COOLDOWN = timedelta(minutes=5)
MAX_ATTEMPTS = 2  # one original send plus at most one known-not-sent retry
LEDGER_RETENTION_DAYS = 30
MAX_DAILY_ACTIVITY_TIMESTAMPS = 512
MAX_SOURCE_VERSIONS_PER_DAY = 64

POLICY_PRIVATE_MESSAGE = "private_message_after_device_end"
WAKE_POLICIES = {POLICY_PRIVATE_MESSAGE}
OUTCOMES = {"sent", "failed_not_sent", "unknown"}
RETRYABLE_PRECHECK_REASONS = {
    "precheck_non_plain_input",
    "precheck_goodnight_quiet",
    "precheck_explicit_goodnight",
    "precheck_weight_mode",
    "precheck_other_person_health",
    "precheck_sync_unavailable",
    "precheck_sync_failed",
    "precheck_sleep_candidate_missing",
    "precheck_candidate_invalid",
    "precheck_evaluation_error",
    "precheck_recovered_claim",
}


class LedgerError(ValueError):
    """Raised for malformed or inconsistent scheduling metadata."""


def validate_ledger(value: object) -> dict[str, Any]:
    """Validate an on-disk ledger against the minimal metadata schema.

    Args:
        value: JSON-decoded value from the private sleep delivery file.

    Returns:
        The validated ledger dictionary.

    Raises:
        LedgerError: The value is malformed, oversized, or contains fields
            outside the allowlist.
    """
    root_fields = {"schema_version", "days", "private_activity_times_by_date"}
    day_fields = {
        "date",
        "selected",
        "delay_minutes",
        "delivery_state",
        "quota_consumed_at",
        "attempt_count",
        "retry_not_before",
        "current_version_id",
        "current_candidate_fetched_at",
        "current_due_at",
        "current_device_due_at",
        "current_private_confirmation_at",
        "current_version_stable",
        "versions",
        "attempts",
        "active_attempt_id",
        "unknown_since",
        "legacy_plan_due_at",
        "chat_flow",
    }
    version_fields = {
        "device_wake_at", "source_modified_at", "first_seen_at", "stable_since",
        "last_seen_at", "private_confirmation_at", "private_confirmed_due_at",
    }
    attempt_fields = {
        "attempt_id", "version_id", "started_at", "finished_at", "outcome",
        "wake_confirmation_policy", "user_activity_at", "source",
    }
    allowed_states = {
        "ready", "in_flight", "failed_not_sent", "unknown", "sent",
        "unknown_history",
    }
    if not isinstance(value, dict) or set(value) != root_fields:
        raise LedgerError("ledger has an unsupported shape")
    if type(value.get("schema_version")) is not int or value["schema_version"] not in {
        1,
        2,
    }:
        raise LedgerError("ledger has an unsupported version")
    days = value.get("days")
    activities = value.get("private_activity_times_by_date")
    if not isinstance(days, dict) or len(days) > LEDGER_RETENTION_DAYS:
        raise LedgerError("ledger days are invalid or oversized")
    if not isinstance(activities, dict) or len(activities) > 1:
        raise LedgerError("private activity dates are invalid or oversized")

    for day_key, day in days.items():
        try:
            date.fromisoformat(day_key)
        except (TypeError, ValueError) as exc:
            raise LedgerError("ledger date is invalid") from exc
        if not isinstance(day, dict) or set(day) - day_fields:
            raise LedgerError("daily ledger contains unsupported fields")
        if value["schema_version"] == 2:
            flow = day.get("chat_flow")
            if not isinstance(flow, dict) or set(flow) != {
                "status",
                "wake_event_hash",
                "wake_received_at",
                "checked_event_hash",
                "checked_at",
                "reason",
            }:
                raise LedgerError("chat flow has an unsupported shape")
            if flow["status"] not in {"armed", "waiting", "checked", "closed"}:
                raise LedgerError("chat flow state is invalid")
            for field in ("wake_event_hash", "checked_event_hash"):
                if flow[field] is not None and (
                    not isinstance(flow[field], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", flow[field])
                ):
                    raise LedgerError("chat event hash is invalid")
            for field in ("wake_received_at", "checked_at"):
                if flow[field] is not None:
                    _as_time(flow[field], field)
            if not isinstance(flow["reason"], str) or len(flow["reason"]) > 64:
                raise LedgerError("chat disposition is invalid")
            if flow["status"] == "waiting" and (
                flow["wake_event_hash"] is None or flow["wake_received_at"] is None
            ):
                raise LedgerError("chat wake anchor is missing")
            if flow["status"] == "checked" and (
                flow["checked_event_hash"] is None or flow["checked_at"] is None
            ):
                raise LedgerError("chat check metadata is missing")
        delivery_state = day.get("delivery_state")
        if (day.get("date") != day_key or not isinstance(delivery_state, str)
                or delivery_state not in allowed_states):
            raise LedgerError("daily ledger state is invalid")
        if day.get("selected") not in (True, False, None):
            raise LedgerError("daily selection is invalid")
        if day.get("selected") is not None and type(day.get("selected")) is not bool:
            raise LedgerError("daily selection is invalid")
        if type(day.get("current_version_stable")) is not bool:
            raise LedgerError("daily stability marker is invalid")
        delay = day.get("delay_minutes")
        if delay is not None and (type(delay) is not int or not 60 <= delay <= 120):
            raise LedgerError("daily delay is invalid")
        attempt_count = day.get("attempt_count")
        if type(attempt_count) is not int or not 0 <= attempt_count <= MAX_ATTEMPTS:
            raise LedgerError("daily attempt count is invalid")
        for field in (
            "quota_consumed_at", "retry_not_before", "current_candidate_fetched_at",
            "current_due_at", "current_device_due_at", "current_private_confirmation_at",
            "unknown_since", "legacy_plan_due_at",
        ):
            raw = day.get(field)
            if raw is not None:
                _as_time(raw, field)
        current_version_id = day.get("current_version_id")
        if current_version_id is not None and not re.fullmatch(r"[0-9a-f]{24}", str(current_version_id)):
            raise LedgerError("current source version is invalid")
        versions = day.get("versions")
        if not isinstance(versions, dict) or len(versions) > MAX_SOURCE_VERSIONS_PER_DAY:
            raise LedgerError("source version metadata is invalid or oversized")
        if current_version_id is not None and current_version_id not in versions:
            raise LedgerError("current source version metadata is missing")
        for version_id, version in versions.items():
            if not isinstance(version_id, str) or not re.fullmatch(r"[0-9a-f]{24}", version_id):
                raise LedgerError("source version key is invalid")
            if not isinstance(version, dict) or set(version) != version_fields:
                raise LedgerError("source version contains unsupported fields")
            for field in version_fields:
                if field in {
                    "device_wake_at", "source_modified_at", "first_seen_at",
                    "stable_since", "last_seen_at",
                } or version[field] is not None:
                    _as_time(version[field], field)
        attempts = day.get("attempts")
        if not isinstance(attempts, list) or len(attempts) > MAX_ATTEMPTS:
            raise LedgerError("daily attempts are invalid or oversized")
        attempt_ids = set()
        for attempt in attempts:
            if not isinstance(attempt, dict) or set(attempt) - attempt_fields:
                raise LedgerError("attempt contains unsupported fields")
            attempt_id = attempt.get("attempt_id")
            if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 80 or attempt_id in attempt_ids:
                raise LedgerError("attempt ID is invalid")
            attempt_ids.add(attempt_id)
            outcome = attempt.get("outcome")
            if not isinstance(outcome, str) or outcome not in {"in_flight", *OUTCOMES}:
                raise LedgerError("attempt outcome is invalid")
            attempt_version_id = attempt.get("version_id")
            if attempt_version_id is not None and (
                not isinstance(attempt_version_id, str)
                or not re.fullmatch(r"[0-9a-f]{24}", attempt_version_id)
            ):
                raise LedgerError("attempt source version is invalid")
            for field in ("started_at", "finished_at", "user_activity_at"):
                raw = attempt.get(field)
                if raw is not None:
                    _as_time(raw, field)
            policy = attempt.get("wake_confirmation_policy")
            if policy is not None and (
                not isinstance(policy, str) or policy not in WAKE_POLICIES
            ):
                raise LedgerError("attempt wake policy is invalid")
            source = attempt.get("source")
            if source is not None and (not isinstance(source, str) or len(source) > 64):
                raise LedgerError("attempt source is invalid")
        active_id = day.get("active_attempt_id")
        if active_id is not None and (
            not isinstance(active_id, str) or active_id not in attempt_ids
        ):
            raise LedgerError("active attempt metadata is missing")

    for activity_date, timestamps in activities.items():
        try:
            date.fromisoformat(activity_date)
        except (TypeError, ValueError) as exc:
            raise LedgerError("private activity date is invalid") from exc
        if not isinstance(timestamps, list) or len(timestamps) > MAX_DAILY_ACTIVITY_TIMESTAMPS:
            raise LedgerError("private activity timestamps are invalid or oversized")
        previous = None
        for raw in timestamps:
            parsed = _as_time(raw, "private_activity_at")
            if previous is not None and parsed < previous:
                raise LedgerError("private activity timestamps are unordered")
            if parsed.astimezone(SHANGHAI).date().isoformat() != activity_date:
                raise LedgerError("private activity timestamp date does not match")
            previous = parsed
    return value


def _as_time(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise LedgerError(f"invalid {field}") from exc
    else:
        raise LedgerError(f"invalid {field}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LedgerError(f"{field} must be timezone-aware")
    return parsed


def _iso(value: datetime) -> str:
    return value.isoformat()


def _today(now: datetime) -> str:
    return now.astimezone(SHANGHAI).date().isoformat()


def new_ledger() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "days": {},
        # Timestamps only, retained for the current local date. Caller must
        # invoke this only after validating the configured owner's private UMO.
        "private_activity_times_by_date": {},
    }


def _prune(ledger: dict[str, Any], today: str) -> None:
    cutoff = date.fromisoformat(today) - timedelta(days=LEDGER_RETENTION_DAYS - 1)
    days = ledger.setdefault("days", {})
    for key in list(days):
        try:
            old = date.fromisoformat(key) < cutoff
        except (TypeError, ValueError):
            old = True
        if old:
            del days[key]
    activity = ledger.setdefault("private_activity_times_by_date", {})
    for key in list(activity):
        if key != today:
            del activity[key]


def remember_private_activity(
    ledger: dict[str, Any],
    activity_at: str | datetime,
    observed_at: str | datetime,
) -> bool:
    """Persist a bounded time-only trail for an authorized owner's private chat.

    The caller must already have checked the configured private session and
    excluded synthetic bot messages. Re-evaluating timestamps against each new
    device version prevents a message before a revised sleep end from becoming
    permanent awake state.
    """
    timestamp = _as_time(activity_at, "activity_at")
    observed = _as_time(observed_at, "observed_at")
    local_date = _today(timestamp)
    current_date = _today(observed)
    # Ignore delayed/replayed events from another local date and timestamps
    # that are ahead of observation time. Only today's actual private activity
    # may confirm wakefulness or be retained in the bounded trail.
    if local_date != current_date or timestamp > observed:
        return False
    ledger.setdefault("schema_version", 1)
    ledger.setdefault("days", {})
    ledger.setdefault("private_activity_times_by_date", {})
    times = ledger["private_activity_times_by_date"].setdefault(local_date, [])
    serialized = _iso(timestamp)
    if serialized not in times:
        times.append(serialized)
        times.sort(key=lambda value: _as_time(value, "stored activity_at"))
        del times[:-MAX_DAILY_ACTIVITY_TIMESTAMPS]
    day = ledger.get("days", {}).get(local_date)
    if isinstance(day, dict):
        _refresh_current_confirmation(ledger, day, local_date)
    _prune(ledger, current_date)
    return True


def clear_private_activity_evidence(ledger: dict[str, Any]) -> bool:
    """Forget prior-process user timestamps without changing the daily draw.

    Args:
        ledger: Mutable scheduling ledger.

    Returns:
        Whether any persisted wake evidence was cleared.
    """
    changed = bool(ledger.get("private_activity_times_by_date"))
    ledger["private_activity_times_by_date"] = {}
    for day in ledger.get("days", {}).values():
        if not isinstance(day, dict):
            continue
        changed = changed or bool(day.get("current_private_confirmation_at"))
        changed = changed or bool(day.get("current_due_at"))
        day["current_private_confirmation_at"] = None
        day["current_due_at"] = None
        for version in day.get("versions", {}).values():
            if not isinstance(version, dict):
                continue
            changed = changed or bool(version.get("private_confirmation_at"))
            changed = changed or bool(version.get("private_confirmed_due_at"))
            version["private_confirmation_at"] = None
            version["private_confirmed_due_at"] = None
    return changed


def migrate_legacy_state(legacy: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """Migrate legacy daily-care metadata without treating an attempt as sent.

    A same-day attempted flag has no receipt in the legacy format, so it becomes
    unknown and is locked against retries. A copied prior-day timestamp remains
    prior-day history and never consumes today's opportunity.
    """
    today = _today(_as_time(now, "now"))
    ledger = new_ledger()
    _prune(ledger, today)

    attempt_at_raw = legacy.get("sleep_attempted_at")
    attempt_at = None
    if attempt_at_raw:
        try:
            attempt_at = _as_time(attempt_at_raw, "sleep_attempted_at")
        except LedgerError:
            attempt_at = None

    plan_due_raw = legacy.get("sleep_care_due_at")
    plan_due = None
    if plan_due_raw:
        try:
            plan_due = _as_time(plan_due_raw, "sleep_care_due_at")
        except LedgerError:
            plan_due = None

    legacy_date = legacy.get("date")
    if legacy_date == today:
        selected = legacy.get("sleep_care_selected")
        delay = legacy.get("sleep_care_delay_minutes")
        if isinstance(selected, bool):
            # A losing daily draw may have no due timestamp. Keep both results
            # so installing the chat policy cannot create a second chance.
            valid_delay = delay if type(delay) is int and 60 <= delay <= 120 else None
            ledger["days"][today] = _new_day(today, selected, valid_delay)
            if plan_due and _today(plan_due) == today:
                ledger["days"][today]["legacy_plan_due_at"] = _iso(plan_due)

    attempted_on_today = bool(
        (legacy_date == today and legacy.get("sleep_attempted") is True)
        or (attempt_at is not None and _today(attempt_at) == today)
    )
    legacy_version_id = legacy.get("sleep_attempted_record_id")
    version_id = (
        legacy_version_id
        if isinstance(legacy_version_id, str)
        and re.fullmatch(r"[0-9a-f]{24}", legacy_version_id)
        else None
    )
    if attempted_on_today:
        day = ledger["days"].setdefault(today, _new_day(today, True, None))
        day["selected"] = True
        day["delivery_state"] = "unknown"
        day["unknown_since"] = _iso(attempt_at) if attempt_at else _iso(_as_time(now, "now"))
        day["attempt_count"] = max(1, day.get("attempt_count", 0))
        day["attempts"].append({
            "attempt_id": "legacy-unknown",
            "version_id": version_id if isinstance(version_id, str) else None,
            "started_at": _iso(attempt_at) if attempt_at else None,
            "finished_at": None,
            "outcome": "unknown",
            "source": "legacy_state_without_receipt",
        })
    elif attempt_at is not None:
        # Some versions copied the last attempt timestamp across midnight while
        # resetting sleep_attempted. Preserve only non-blocking history.
        old_date = _today(attempt_at)
        if old_date != today:
            history = _new_day(old_date, False, None)
            history.update(
                selected=None,
                delivery_state="unknown_history",
                attempt_count=1,
                attempts=[{
                    "attempt_id": "legacy-history",
                    "version_id": version_id,
                    "started_at": _iso(attempt_at),
                    "finished_at": None,
                    "outcome": "unknown",
                    "source": "copied_prior_day_timestamp",
                }],
            )
            ledger["days"][old_date] = history

    _prune(ledger, today)
    return ledger


def _new_day(
    local_date: str, selected: bool | None, delay_minutes: int | None
) -> dict[str, Any]:
    return {
        "date": local_date,
        "selected": selected,
        "delay_minutes": delay_minutes,
        "delivery_state": "ready",
        "quota_consumed_at": None,
        "attempt_count": 0,
        "retry_not_before": None,
        "current_version_id": None,
        "current_candidate_fetched_at": None,
        "current_due_at": None,
        "current_device_due_at": None,
        "current_private_confirmation_at": None,
        "current_version_stable": False,
        "versions": {},
        "attempts": [],
    }


def _ensure_day(ledger: dict[str, Any], local_date: str, rng: Any) -> dict[str, Any]:
    ledger.setdefault("schema_version", 1)
    ledger.setdefault("days", {})
    ledger.setdefault("private_activity_times_by_date", {})
    day = ledger["days"].get(local_date)
    if day is None:
        selected = rng.random() < 0.5
        delay = rng.randint(60, 120)
        day = _new_day(local_date, selected, delay)
        ledger["days"][local_date] = day
    _prune(ledger, local_date)
    return day


def _refresh_current_confirmation(
    ledger: Mapping[str, Any], day: dict[str, Any], local_date: str
) -> None:
    version = day.get("versions", {}).get(day.get("current_version_id"))
    if not isinstance(version, dict):
        return
    device_end = _as_time(version["device_wake_at"], "device_wake_at")
    candidates = []
    for item in ledger.get("private_activity_times_by_date", {}).get(local_date, []):
        try:
            activity_at = _as_time(item, "private_activity_at")
        except LedgerError:
            continue
        if activity_at >= device_end:
            candidates.append(activity_at)
    confirmation = min(candidates) if candidates else None
    version["private_confirmation_at"] = _iso(confirmation) if confirmation else None
    version["private_confirmed_due_at"] = (
        _iso(confirmation + timedelta(minutes=day["delay_minutes"]))
        if confirmation and isinstance(day.get("delay_minutes"), int)
        else None
    )
    day["current_private_confirmation_at"] = (
        _iso(confirmation) if confirmation else None
    )
    day["current_due_at"] = version["private_confirmed_due_at"]


def observe_candidate(
    ledger: dict[str, Any],
    candidate: Mapping[str, Any],
    now: str | datetime,
    rng: Any = random,
) -> dict[str, Any]:
    """Record a validated sleep-record version without storing the snapshot.

    Required candidate keys: ``date``, ``version_id``, ``device_wake_at``,
    ``source_modified_at``, and ``fetched_at``. ``version_id`` is the existing
    opaque content hash from care_logic.sleep_record().
    """
    if ledger.get("schema_version") == 2:
        raise LedgerError("chat reply ledgers cannot schedule sleep care")
    current = _as_time(now, "now")
    local_date = _today(current)
    if candidate.get("date") != local_date:
        raise LedgerError("candidate is not for the current Asia/Shanghai date")

    version_id = candidate.get("version_id")
    if not isinstance(version_id, str) or not re.fullmatch(r"[0-9a-f]{24}", version_id):
        raise LedgerError("invalid candidate version_id")
    wake_at = _as_time(candidate.get("device_wake_at"), "device_wake_at")
    modified_at = _as_time(candidate.get("source_modified_at"), "source_modified_at")
    fetched_at = _as_time(candidate.get("fetched_at"), "fetched_at")
    if wake_at.astimezone(SHANGHAI).date().isoformat() != local_date:
        raise LedgerError("device wake timestamp is not on the candidate date")
    if wake_at > current or modified_at > fetched_at or modified_at < wake_at:
        raise LedgerError("candidate timestamps are inconsistent")
    if fetched_at > current or current - fetched_at > MAX_SNAPSHOT_AGE:
        raise LedgerError("candidate snapshot is stale")

    day = _ensure_day(ledger, local_date, rng)
    versions = day.setdefault("versions", {})
    version = versions.get(version_id)
    fetched_iso = _iso(fetched_at)
    wake_iso = _iso(wake_at)
    modified_iso = _iso(modified_at)
    previous_current = day.get("current_version_id")

    if version is None:
        version = {
            "device_wake_at": wake_iso,
            "source_modified_at": modified_iso,
            "first_seen_at": fetched_iso,
            "stable_since": fetched_iso,
            "last_seen_at": fetched_iso,
            "private_confirmation_at": None,
            "private_confirmed_due_at": None,
        }
        versions[version_id] = version
        if len(versions) > MAX_SOURCE_VERSIONS_PER_DAY:
            removable = sorted(
                (key for key in versions if key != version_id),
                key=lambda key: versions[key]["last_seen_at"],
            )
            for old_version_id in removable[:len(versions) - MAX_SOURCE_VERSIONS_PER_DAY]:
                del versions[old_version_id]
    else:
        # Re-entering a prior version after another version was current starts a
        # new stability interval; old observations are not considered continuous.
        if previous_current != version_id:
            version["stable_since"] = fetched_iso
            version["first_seen_at"] = fetched_iso
        if fetched_at > _as_time(version["last_seen_at"], "last_seen_at"):
            version["last_seen_at"] = fetched_iso
            version["device_wake_at"] = wake_iso
            version["source_modified_at"] = modified_iso

    current_fetch = day.get("current_candidate_fetched_at")
    if current_fetch is None or fetched_at >= _as_time(current_fetch, "current_candidate_fetched_at"):
        if previous_current != version_id:
            version["stable_since"] = fetched_iso
            version["first_seen_at"] = fetched_iso
        day["current_version_id"] = version_id
        day["current_candidate_fetched_at"] = fetched_iso
        delay = day.get("delay_minutes")
        day["current_device_due_at"] = (
            _iso(wake_at + timedelta(minutes=delay))
            if isinstance(delay, int) and 60 <= delay <= 120
            else None
        )
        _refresh_current_confirmation(ledger, day, local_date)
        day["current_version_stable"] = (
            _as_time(version["last_seen_at"], "last_seen_at")
            - _as_time(version["stable_since"], "stable_since")
            >= STABILITY_WINDOW
        )
    return day


def _wake_evidence(
    policy: str | None,
    device_wake_at: datetime,
    now: datetime,
    activity_at: datetime | None,
) -> tuple[bool, str]:
    if policy not in WAKE_POLICIES:
        return False, "wake_confirmation_policy_unselected"
    if activity_at is None or activity_at < device_wake_at or activity_at > now:
        return False, "waiting_for_private_message_after_device_end"
    return True, "private_message_after_device_end"


def eligibility(
    ledger: Mapping[str, Any],
    now: str | datetime,
    wake_confirmation_policy: str | None,
    activated_at: str | datetime | None = None,
) -> tuple[bool, str]:
    if ledger.get("schema_version") == 2:
        return False, "chat_reply_only"
    current = _as_time(now, "now")
    local_date = _today(current)
    day = ledger.get("days", {}).get(local_date)
    if not isinstance(day, Mapping):
        return False, "no_candidate"
    if day.get("delivery_state") in {"in_flight", "unknown", "sent"}:
        return False, day["delivery_state"]
    if day.get("selected") is not True:
        return False, "not_selected_today"
    version_id = day.get("current_version_id")
    version = day.get("versions", {}).get(version_id)
    if not isinstance(version, Mapping):
        return False, "no_candidate"
    if not day.get("current_version_stable"):
        return False, "candidate_not_stable"
    wake_at = _as_time(version["device_wake_at"], "device_wake_at")
    fetched_at = _as_time(version["last_seen_at"], "last_seen_at")
    if current - fetched_at > MAX_SNAPSHOT_AGE:
        return False, "candidate_stale"
    activity_at = None
    raw_activity = day.get("current_private_confirmation_at")
    if raw_activity is not None:
        try:
            activity_at = _as_time(raw_activity, "last_private_activity_at")
        except LedgerError:
            return False, "invalid_private_activity_timestamp"
    confirmed, reason = _wake_evidence(
        wake_confirmation_policy, wake_at, current, activity_at
    )
    if not confirmed:
        return False, reason

    due_raw = version.get("private_confirmed_due_at")
    if not due_raw:
        return False, "waiting_for_private_message_after_device_end"
    due_at = _as_time(due_raw, "private_confirmed_due_at")
    due_reason = "not_due_after_awake_confirmation"
    if activated_at is not None and due_at < _as_time(activated_at, "activated_at"):
        return False, "no_backfill_before_activation"
    if current < due_at:
        if current > due_at + LATE_GRACE:
            return False, "late_window_expired"
        else:
            return False, due_reason
    if current > due_at + LATE_GRACE:
        return False, "late_window_expired"

    if day.get("delivery_state") == "failed_not_sent":
        retry_at = day.get("retry_not_before")
        if retry_at and current < _as_time(retry_at, "retry_not_before"):
            return False, "retry_cooldown"
        if day.get("attempt_count", 0) >= MAX_ATTEMPTS:
            return False, "retry_limit_reached"
    return True, "eligible"


def begin_attempt(
    ledger: dict[str, Any],
    now: str | datetime,
    attempt_id: str,
    wake_confirmation_policy: str | None,
    activated_at: str | datetime | None = None,
) -> tuple[bool, str]:
    current = _as_time(now, "now")
    allowed, reason = eligibility(
        ledger, current, wake_confirmation_policy,
        activated_at=activated_at,
    )
    if not allowed:
        return False, reason
    if not isinstance(attempt_id, str) or not attempt_id:
        raise LedgerError("attempt_id is required")
    day = ledger["days"][_today(current)]
    version_id = day["current_version_id"]
    day["attempt_count"] += 1
    day["delivery_state"] = "in_flight"
    day["active_attempt_id"] = attempt_id
    day["attempts"].append({
        "attempt_id": attempt_id,
        "version_id": version_id,
        "started_at": _iso(current),
        "finished_at": None,
        "outcome": "in_flight",
        "wake_confirmation_policy": wake_confirmation_policy,
        "user_activity_at": day.get("current_private_confirmation_at"),
    })
    # The caller must atomically persist this state before placing work in the
    # asynchronous message queue. A crash after this point is reconciled as unknown.
    return True, "in_flight"


def finish_attempt(
    ledger: dict[str, Any],
    attempt_id: str,
    outcome: str,
    at: str | datetime,
) -> tuple[bool, str]:
    """Persist a transport result. Only an accepted send consumes daily quota."""
    if outcome not in OUTCOMES:
        raise LedgerError("outcome must be sent, failed_not_sent, or unknown")
    finished = _as_time(at, "outcome_at")
    day = next((
        item for item in ledger.get("days", {}).values()
        if isinstance(item, dict) and item.get("active_attempt_id") == attempt_id
    ), None)
    if day is None:
        return False, "attempt_not_current"
    attempt = next(
        (item for item in reversed(day.get("attempts", [])) if item.get("attempt_id") == attempt_id),
        None,
    )
    if attempt is None:
        return False, "attempt_not_found"

    previous = attempt.get("outcome")
    if previous in {"sent", "failed_not_sent"}:
        if previous == outcome:
            return True, previous
        # Conflicting transport evidence is never retried automatically.
        attempt["outcome"] = "unknown"
        attempt["finished_at"] = _iso(finished)
        day["delivery_state"] = "unknown"
        day["unknown_since"] = _iso(finished)
        day["quota_consumed_at"] = None
        return False, "conflicting_result_locked"

    attempt["outcome"] = outcome
    attempt["finished_at"] = _iso(finished)
    if outcome == "sent":
        day["delivery_state"] = "sent"
        day["quota_consumed_at"] = _iso(finished)
        day["retry_not_before"] = None
        return True, "quota_consumed"
    if outcome == "failed_not_sent":
        day["delivery_state"] = "failed_not_sent"
        day["quota_consumed_at"] = None
        day["retry_not_before"] = _iso(finished + RETRY_COOLDOWN)
        return True, "retryable_if_still_due"
    day["delivery_state"] = "unknown"
    day["unknown_since"] = _iso(finished)
    day["quota_consumed_at"] = None
    return True, "locked_pending_reconciliation"


def recover_in_flight(ledger: dict[str, Any], now: str | datetime) -> int:
    """Convert interrupted sends to unknown so a restart cannot resend them."""
    current = _as_time(now, "now")
    changed = 0
    for day in ledger.get("days", {}).values():
        if not isinstance(day, dict) or day.get("delivery_state") != "in_flight":
            continue
        day["delivery_state"] = "unknown"
        day["unknown_since"] = _iso(current)
        for attempt in reversed(day.get("attempts", [])):
            if attempt.get("attempt_id") == day.get("active_attempt_id"):
                attempt["outcome"] = "unknown"
                attempt["finished_at"] = _iso(current)
                break
        changed += 1
    return changed


def recover_unselected_chat_claim(ledger: dict[str, Any], now: datetime) -> bool:
    """Reopen today's exact interrupted claim only when no draw or send exists.

    Args:
        ledger: Validated v2 delivery metadata.
        now: Current timezone-aware time.

    Returns:
        Whether today's recoverable next-chat claim was reopened.
    """
    if ledger.get("schema_version") != 2:
        return False
    today = _today(now)
    day = ledger.get("days", {}).get(today)
    if not isinstance(day, dict):
        return False
    flow = day.get("chat_flow", {})
    if (
        flow.get("status") != "checked"
        or flow.get("reason") != "claimed_next_chat"
        or day.get("selected") is not None
        or day.get("attempt_count") != 0
        or day.get("delivery_state") != "ready"
        or day.get("quota_consumed_at") is not None
        or day.get("active_attempt_id") is not None
        or day.get("attempts")
        or day.get("current_version_id") is not None
        or day.get("current_candidate_fetched_at") is not None
        or day.get("current_due_at") is not None
        or day.get("current_version_stable") is not False
        or not isinstance(flow.get("checked_event_hash"), str)
        or flow.get("checked_at") is None
        or _today(_as_time(flow["checked_at"], "checked_at")) != today
        or flow.get("wake_received_at") is None
        or _today(_as_time(flow["wake_received_at"], "wake_received_at")) != today
    ):
        return False
    flow.update(status="waiting", reason="precheck_recovered_claim")
    return True


def classify_onebot_result(response: Mapping[str, Any] | None = None, error_kind: str | None = None) -> str:
    """Classify OneBot transport evidence without guessing on timeouts.

    A successful action response means the platform accepted the send; it does
    not mean the recipient read it. ``ActionFailed`` is an explicit negative
    action response. Missing responses and transport errors remain unknown.
    """
    if error_kind == "ActionFailed":
        return "failed_not_sent"
    if error_kind is not None:
        return "unknown"
    if isinstance(response, Mapping):
        if response.get("status") == "ok":
            return "sent"
        if response.get("status") == "failed":
            return "failed_not_sent"
    return "unknown"


def migrate_to_chat_reply(ledger: dict[str, Any], now: datetime) -> bool:
    """Cancel delayed sleep work while preserving all daily draws and receipts.

    Args:
        ledger: Validated v1 or v2 delivery metadata.
        now: Current timezone-aware time for restart recovery.

    Returns:
        Whether migration or interrupted wake-opportunity recovery changed state.
    """
    validate_ledger(ledger)
    changed = ledger["schema_version"] == 1
    was_legacy = changed
    ledger["schema_version"] = 2
    changed = clear_private_activity_evidence(ledger) or changed
    today = _today(now)
    for day_key, day in ledger["days"].items():
        for field in ("current_due_at", "current_device_due_at", "legacy_plan_due_at"):
            if day.get(field) is not None:
                changed = True
            day[field] = None
        if was_legacy:
            closed = day.get("selected") is not None or day["attempt_count"] > 0
            day["chat_flow"] = {
                "status": "closed" if closed else "armed",
                "wake_event_hash": None,
                "wake_received_at": None,
                "checked_event_hash": None,
                "checked_at": None,
                "reason": "legacy_day_closed" if closed else "awaiting_verified_wake",
            }
        elif day["chat_flow"]["status"] == "waiting":
            flow = day["chat_flow"]
            recoverable_wait = (
                day_key == today
                and flow["reason"]
                in RETRYABLE_PRECHECK_REASONS | {"awaiting_next_chat"}
                and day.get("selected") is None
                and day.get("attempt_count") == 0
                and day.get("delivery_state") == "ready"
                and day.get("quota_consumed_at") is None
                and day.get("active_attempt_id") is None
                and not day.get("attempts")
                and day.get("current_version_id") is None
                and day.get("current_candidate_fetched_at") is None
                and day.get("current_due_at") is None
                and day.get("current_version_stable") is False
            )
            if not recoverable_wait:
                flow.update(status="closed", reason="wake_interrupted_by_reload")
                changed = True
    return changed


def record_chat_wake(
    ledger: dict[str, Any],
    event_hash: str,
    received_at: datetime,
    device_wake_at: datetime | None = None,
) -> bool:
    """Arm one next-chat opportunity from a verified real private event.

    Args:
        ledger: V2 metadata ledger; the caller already checked owner and transport.
        event_hash: SHA-256 of platform/self/owner/message-ID correlation metadata.
        received_at: Transport receipt time, not a converted or synthetic timestamp.
        device_wake_at: End time from an already validated fresh cached sleep record.

    Returns:
        Whether this event creates today's first wake anchor and should start sync.

    Raises:
        LedgerError: The ledger or correlation metadata is invalid.
    """
    if ledger.get("schema_version") != 2 or not re.fullmatch(
        r"[0-9a-f]{64}", event_hash
    ):
        raise LedgerError("chat wake metadata is invalid")
    now = _as_time(received_at, "received_at")
    today = _today(now)
    _prune(ledger, today)
    day = ledger["days"].get(today)
    if day is None:
        day = _new_day(today, None, None)
        day["chat_flow"] = {
            "status": "armed",
            "wake_event_hash": None,
            "wake_received_at": None,
            "checked_event_hash": None,
            "checked_at": None,
            "reason": "awaiting_verified_wake",
        }
        ledger["days"][today] = day
    flow = day["chat_flow"]
    if (
        day["selected"] is None
        and day["attempt_count"] == 0
        and day["delivery_state"] == "ready"
        and flow["wake_received_at"] is not None
        and device_wake_at is not None
    ):
        device_end = _as_time(device_wake_at, "device_wake_at")
        prior = _as_time(flow["wake_received_at"], "wake_received_at")
        # Midnight/pre-sleep chat cannot consume the next morning's wake slot.
        # A later opportunity requires proof that the prior message was before
        # this sleep end, not merely that data eventually became available.
        if _today(device_end) == today and prior < device_end <= now:
            flow.update(
                status="armed",
                wake_event_hash=None,
                wake_received_at=None,
                checked_event_hash=None,
                checked_at=None,
                reason="prior_message_before_device_end",
            )
    if flow["status"] != "armed" or day["delivery_state"] != "ready":
        return False
    flow.update(
        status="waiting",
        wake_event_hash=event_hash,
        wake_received_at=_iso(now),
        reason="awaiting_next_chat",
    )
    return True


def claim_chat_reply(
    ledger: dict[str, Any],
    event_hash: str,
    received_at: datetime,
    now: datetime,
) -> tuple[bool, str]:
    """Consume the next ordinary reply slot before any snapshot or route checks.

    Args:
        ledger: V2 metadata ledger with a wake anchor.
        event_hash: Verified next-chat message correlation hash.
        received_at: Verified transport receipt time for this message.
        now: Current request-preparation time.

    Returns:
        A claim result and metadata-only disposition reason.
    """
    if ledger.get("schema_version") != 2:
        return False, "chat_schema_required"
    day = ledger["days"].get(_today(now))
    if day is None or day["chat_flow"]["status"] != "waiting":
        return False, "no_next_chat_opportunity"
    flow = day["chat_flow"]
    wake = _as_time(flow["wake_received_at"], "wake_received_at")
    if event_hash == flow["wake_event_hash"] or received_at < wake:
        return False, "wake_event_or_older"
    if (
        event_hash == flow["checked_event_hash"]
        and flow["reason"] in RETRYABLE_PRECHECK_REASONS
    ):
        return False, "same_precheck_event"
    if not re.fullmatch(r"[0-9a-f]{64}", event_hash):
        raise LedgerError("chat event hash is invalid")
    flow.update(status="checked", checked_event_hash=event_hash, checked_at=_iso(now))
    if day["delivery_state"] != "ready" or day["attempt_count"]:
        flow["reason"] = "prior_delivery_locked"
        return False, flow["reason"]
    if now < received_at:
        flow["reason"] = "invalid_reply_time"
        return False, flow["reason"]
    flow["reason"] = "claimed_next_chat"
    return True, flow["reason"]


def release_chat_reply_claim(
    ledger: dict[str, Any], event_hash: str, reason_code: str, now: datetime
) -> tuple[bool, str]:
    """Release a pre-draw claim while retaining a reason and duplicate guard.

    Args:
        ledger: Validated v2 delivery metadata.
        event_hash: Event hash that currently owns the provisional claim.
        reason_code: Fixed, content-free precheck code without the prefix.
        now: Current timezone-aware time.

    Returns:
        Whether the claim was released and its metadata-only disposition.
    """
    if ledger.get("schema_version") != 2 or reason_code not in {
        "non_plain_input",
        "goodnight_quiet",
        "explicit_goodnight",
        "weight_mode",
        "other_person_health",
        "sync_unavailable",
        "sync_failed",
        "sleep_candidate_missing",
        "candidate_invalid",
        "evaluation_error",
    }:
        return False, "invalid_precheck_reason"
    day = ledger.get("days", {}).get(_today(now))
    if day is None:
        return False, "no_claim"
    flow = day["chat_flow"]
    if (
        flow["status"] != "checked"
        or flow["checked_event_hash"] != event_hash
        or flow["reason"] not in {"claimed_next_chat", "snapshot_invalid"}
        or day.get("selected") is not None
        or day.get("attempt_count") != 0
        or day.get("delivery_state") != "ready"
        or day.get("quota_consumed_at") is not None
        or day.get("active_attempt_id") is not None
        or day.get("attempts")
        or day.get("current_version_id") is not None
        or day.get("current_candidate_fetched_at") is not None
        or day.get("current_due_at") is not None
        or day.get("current_version_stable") is not False
    ):
        return False, "claim_not_releasable"
    reason = f"precheck_{reason_code}"
    flow.update(status="waiting", reason=reason)
    return True, reason


def reserve_chat_attempt(
    ledger: dict[str, Any],
    event_hash: str,
    candidate: Mapping[str, Any],
    now: datetime,
    attempt_id: str,
    rng: Any = random,
) -> tuple[bool, str]:
    """Draw once and reserve only the already claimed same-chat sleep reply.

    Args:
        ledger: V2 delivery metadata; claim must be atomically saved first.
        event_hash: Same verified event that claimed the next-chat slot.
        candidate: Validated sleep metadata, never the snapshot or metric values.
        now: Current timezone-aware time.
        attempt_id: Bounded event-scoped send correlation ID.
        rng: Injectable 50% coin source for offline verification.

    Returns:
        Whether a send reservation exists, and a metadata-only reason.

    Raises:
        LedgerError: Candidate or reservation metadata cannot be safely interpreted.
    """
    if ledger.get("schema_version") != 2:
        return False, "chat_schema_required"
    day = ledger["days"].get(_today(now))
    if day is None:
        return False, "no_claim"
    flow = day["chat_flow"]
    if (
        flow["status"] != "checked"
        or flow["checked_event_hash"] != event_hash
        or flow["reason"] != "claimed_next_chat"
    ):
        return False, "claim_already_consumed"
    flow["reason"] = "snapshot_invalid"
    wake_anchor = _as_time(flow["wake_received_at"], "wake_received_at")
    fetched = _as_time(candidate.get("fetched_at"), "fetched_at")
    wake = _as_time(candidate.get("device_wake_at"), "device_wake_at")
    modified = _as_time(candidate.get("source_modified_at"), "source_modified_at")
    version = candidate.get("version_id")
    if (
        candidate.get("date") != _today(now)
        or _today(wake) != _today(now)
        or not isinstance(version, str)
        or not re.fullmatch(r"[0-9a-f]{24}", version)
        or not wake <= wake_anchor <= fetched <= now
        or modified < wake
        or modified > fetched
        or now - fetched > MAX_SNAPSHOT_AGE
    ):
        return False, flow["reason"]
    if day["delivery_state"] != "ready" or day["attempt_count"]:
        flow["reason"] = "prior_delivery_locked"
        return False, flow["reason"]
    if day["selected"] is None:
        day["selected"] = rng.random() < 0.5
    if not day["selected"]:
        flow["reason"] = "not_selected_today"
        return False, flow["reason"]
    if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 80:
        raise LedgerError("chat attempt ID is invalid")
    flow["reason"] = "same_reply_reserved"
    day.update(
        delivery_state="in_flight", active_attempt_id=attempt_id, attempt_count=1
    )
    day["attempts"].append(
        {
            "attempt_id": attempt_id,
            "version_id": version,
            "started_at": _iso(now),
            "finished_at": None,
            "outcome": "in_flight",
            "wake_confirmation_policy": POLICY_PRIVATE_MESSAGE,
            "user_activity_at": _iso(wake_anchor),
            "source": "next_chat_reply",
        }
    )
    return True, flow["reason"]
