"""Pure low-step scheduling and a shared metadata-only other-care budget."""

import hashlib
import math
import random
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .care_logic import MAX_SNAPSHOT_AGE
from .health_freshness import CURRENT_STEPS_MAX_AGE

ZONE = ZoneInfo("Asia/Shanghai")
OTHER_DAILY_LIMIT = 2
EXCLUDED_KINDS = {
    "sleep",
    "predicted_period_lead",
    "confirmed_period_start",
    "confirmed_period_end",
    "post_period_care",
    "period_late_inquiry",
    "wellness_low_state",
    "sunlight_evening",
}
STEP_STATUSES = {"planned", "skipped", "not_low", "not_selected", "reserved", "dispatching"}


def aware(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))  # noqa: FURB162 -- Python 3.10 requires this Z normalization.
    except ValueError:
        return None
    return (
        parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    )


def local_now(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Other-care time must be timezone-aware")
    return now.astimezone(ZONE)


def validate_ledger(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "date", "draw_seed", "steps", "attempts"}
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("Other-care ledger has an invalid shape")
    try:
        day = date.fromisoformat(value["date"])
    except (ValueError, TypeError, KeyError):
        raise ValueError("Other-care date is invalid") from None
    seed = value["draw_seed"]
    if (
        not isinstance(seed, str)
        or len(seed) != 32
        or any(character not in "0123456789abcdef" for character in seed)
    ):
        raise ValueError("Other-care daily entropy is invalid")
    steps = value["steps"]
    if (
        not isinstance(steps, dict)
        or set(steps) != {"check_at", "status", "decided_at", "reason"}
        or steps["status"] not in STEP_STATUSES
        or not isinstance(steps["reason"], str)
        or len(steps["reason"]) > 80
    ):
        raise ValueError("Low-step decision is invalid")
    check_at = aware(steps["check_at"])
    if (
        check_at is None
        or check_at.astimezone(ZONE).date() != day
        or check_at.astimezone(ZONE).hour != 20
    ):
        raise ValueError("Low-step check window is invalid")
    decided = aware(steps["decided_at"])
    if steps["status"] == "planned":
        if steps["decided_at"] is not None:
            raise ValueError("Unmade low-step decision has a timestamp")
    elif decided is None or decided.astimezone(ZONE).date() != day:
        raise ValueError("Low-step decision timestamp is invalid")
    attempts = value["attempts"]
    if not isinstance(attempts, list) or len(attempts) > OTHER_DAILY_LIMIT:
        raise ValueError("Other-care budget is invalid")
    seen = set()
    for attempt in attempts:
        if not isinstance(attempt, dict) or set(attempt) != {"kind", "event_id", "at"}:
            raise ValueError("Other-care attempt is invalid")
        stamp = aware(attempt["at"])
        kind, event_id = attempt["kind"], attempt["event_id"]
        if (
            not isinstance(kind, str)
            or not kind
            or len(kind) > 64
            or kind in EXCLUDED_KINDS
            or not isinstance(event_id, str)
            or not event_id
            or len(event_id) > 128
            or event_id in seen
            or stamp is None
            or stamp.astimezone(ZONE).date() != day
        ):
            raise ValueError("Other-care attempt metadata is invalid")
        seen.add(event_id)
    return deepcopy(value)


def prepare_ledger(value, now, chooser=None):
    if chooser is None:
        chooser = random.SystemRandom()
    current = local_now(now)
    today = current.date().isoformat()
    if value is not None:
        value = validate_ledger(value)
        if value["date"] > today:
            raise ValueError("Other-care ledger is ahead of the current date")
        if value["date"] == today:
            return value
    check_at = datetime.combine(current.date(), time(20), tzinfo=ZONE)
    check_at += timedelta(minutes=chooser.randrange(60))
    return {
        "schema_version": 1,
        "date": today,
        "draw_seed": f"{chooser.getrandbits(128):032x}",
        "steps": {
            "check_at": check_at.isoformat(),
            "status": "planned",
            "decided_at": None,
            "reason": "waiting",
        },
        "attempts": [],
    }


def valid_steps(snapshot, now):
    """Verify the fresh daily aggregate without ageing its last movement."""
    current = local_now(now)
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("date") != current.date().isoformat()
        or snapshot.get("source") != "OPPO 健康云端"
    ):
        return None, "not_today_or_unknown_source"
    fetched = aware(snapshot.get("fetched_at"))
    if (
        fetched is None
        or fetched > now
        or now - fetched > MAX_SNAPSHOT_AGE
        or fetched.astimezone(ZONE).date() != current.date()
    ):
        return None, "stale_snapshot"
    errors = snapshot.get("errors")
    if not isinstance(errors, dict) or "steps" in errors:
        return None, "steps_source_failed"
    metrics = snapshot.get("metrics")
    steps = metrics.get("steps") if isinstance(metrics, dict) else None
    total = steps.get("total") if isinstance(steps, dict) else None
    measured = aware(steps.get("measured_at")) if isinstance(steps, dict) else None
    try:
        finite = type(total) in (int, float) and math.isfinite(total)
    except OverflowError:
        finite = False
    if (
        not finite
        or total < 0
        or measured is None
        or measured > fetched
        or measured > now
        or measured.astimezone(ZONE).date() != current.date()
        or now - measured > CURRENT_STEPS_MAX_AGE
    ):
        return None, "invalid_steps"
    return total, "valid"


def reserve_other_attempt(ledger, kind, event_id, now):
    state = prepare_ledger(ledger, now)
    if kind in EXCLUDED_KINDS:
        return state, False, "category_excluded"
    if not isinstance(kind, str) or not kind or len(kind) > 64:
        raise ValueError("Other-care kind is invalid")
    if not isinstance(event_id, str) or not event_id or len(event_id) > 128:
        raise ValueError("Other-care event ID is invalid")
    if any(item["event_id"] == event_id for item in state["attempts"]):
        return state, False, "already_attempted"
    if len(state["attempts"]) >= OTHER_DAILY_LIMIT:
        return state, False, "other_daily_budget_exhausted"
    state["attempts"].append(
        {"kind": kind, "event_id": event_id, "at": local_now(now).isoformat()}
    )
    return state, True, "reserved"


def window_end(check_at):
    local = check_at.astimezone(ZONE)
    return min(
        check_at + timedelta(minutes=15),
        datetime.combine(local.date(), time(21), tzinfo=ZONE),
    )


def daily_draw(ledger):
    """Resolve persisted daily entropy only after valid low steps are confirmed."""
    digest = hashlib.sha256(
        (ledger["date"] + "\0" + ledger["draw_seed"]).encode()
    ).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def check_once(ledger, snapshot, now, activated_at, *, suppressed=False, chooser=None):
    if chooser is None:
        chooser = random.SystemRandom()
    state = prepare_ledger(ledger, now, chooser)
    decision = state["steps"]
    if decision["status"] != "planned":
        return state, None, "already_checked"
    check_at = aware(decision["check_at"])
    local_now(activated_at)
    if now < check_at:
        return state, None, "not_due"
    decision.update(status="skipped", decided_at=local_now(now).isoformat())
    if check_at < activated_at or now >= window_end(check_at):
        decision["reason"] = "old_or_expired_opportunity"
    elif suppressed:
        decision["reason"] = "suppressed"
    else:
        total, reason = valid_steps(snapshot, now)
        if total is None:
            decision["reason"] = reason
        elif total >= 500:
            decision.update(status="not_low", reason="threshold_not_met")
        elif daily_draw(state) >= 0.5:
            decision.update(status="not_selected", reason="daily_draw_missed")
        else:
            state, reserved, reason = reserve_other_attempt(
                state, "low_steps", "low_steps:" + state["date"], now
            )
            state["steps"].update(
                status="reserved" if reserved else "skipped", reason=reason
            )
            if reserved:
                marker = {
                    "date": state["date"],
                    "check_at": check_at.isoformat(),
                    "window_end": window_end(check_at).isoformat(),
                    "reserved_at": local_now(now).isoformat(),
                }
                return state, marker, "reserved"
    return state, None, state["steps"]["reason"]


def dispatch_skip_reason(
    ledger, marker, snapshot, now, activated_at, *, suppressed=False
):
    state = prepare_ledger(ledger, now)
    if (
        not isinstance(marker, dict)
        or set(marker) != {"date", "check_at", "window_end", "reserved_at"}
        or state["date"] != marker["date"]
        or state["steps"]["status"] not in {"reserved", "dispatching"}
        or state["steps"]["check_at"] != marker["check_at"]
        or state["steps"]["decided_at"] != marker["reserved_at"]
        or not any(
            item["event_id"] == "low_steps:" + marker["date"]
            for item in state["attempts"]
        )
    ):
        return "unreserved_opportunity"
    check_at, end = aware(marker["check_at"]), aware(marker["window_end"])
    if check_at is None or end != window_end(check_at) or check_at < activated_at:
        return "old_or_invalid_opportunity"
    if not check_at <= now < end:
        return "outside_check_window"
    if suppressed:
        return "suppressed"
    total, reason = valid_steps(snapshot, now)
    if total is None:
        return reason
    return "threshold_not_met" if total >= 500 else None
