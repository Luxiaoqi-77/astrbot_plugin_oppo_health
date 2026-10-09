"""Score-free episode tracking and randomized weekly slots for status care.

The adapter consumes only the structured OPPO page result. Source-point time is
the sample identity; page update time is freshness metadata and never a sample.
Persisted state contains identifiers and timestamps, never status scores.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import math
import random
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CATEGORIES = {"Excellent", "Good", "Moderate", "Slow down"}
_STATE_KEYS = {
    "schema_version", "armed", "low_started_at", "low_sample_count",
    "recovery_started_at", "recovery_sample_count", "last_sample_id",
    "last_sample_at", "source_date", "current_episode_id", "last_reminder_at",
    "last_reminder_sample_id", "last_reminder_episode_id", "last_reason_code", "schedule_week", "weekly_slots",
    "used_slots", "sunlight_check_date", "sunlight_check_at", "sunlight_used",
    "weight_mode_enabled", "weight_mode_enabled_at",
}


@dataclass(frozen=True)
class WellnessSample:
    sample_id: str
    score: int | None
    category: str
    measured_at: datetime
    updated_at: datetime
    data_date: date
    quality: str
    completeness: str


def empty_runtime_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "armed": True,
        "low_started_at": None,
        "low_sample_count": 0,
        "recovery_started_at": None,
        "recovery_sample_count": 0,
        "last_sample_id": None,
        "last_sample_at": None,
        "source_date": None,
        "current_episode_id": None,
        "last_reminder_at": None,
        "last_reminder_sample_id": None,
        "last_reminder_episode_id": None,
        "last_reason_code": "not_configured",
        "schedule_week": None,
        "weekly_slots": [],
        "used_slots": [],
        "sunlight_check_date": None,
        "sunlight_check_at": None,
        "sunlight_used": False,
        "weight_mode_enabled": False,
        "weight_mode_enabled_at": None,
    }


def validate_runtime_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _STATE_KEYS or value.get("schema_version") != 1:
        raise ValueError("wellness runtime state has an unsupported shape")
    if type(value.get("armed")) is not bool:
        raise ValueError("wellness runtime armed marker is invalid")
    if type(value.get("sunlight_used")) is not bool:
        raise ValueError("wellness runtime sunlight marker is invalid")
    if type(value.get("weight_mode_enabled")) is not bool:
        raise ValueError("weight mode marker is invalid")
    for key in ("low_sample_count", "recovery_sample_count"):
        if type(value.get(key)) is not int or not 0 <= value[key] <= 10000:
            raise ValueError("wellness runtime sample count is invalid")
    for key in ("low_started_at", "recovery_started_at", "last_sample_at", "last_reminder_at", "sunlight_check_at", "weight_mode_enabled_at"):
        raw = value.get(key)
        if raw is not None and _aware(raw) is None:
            raise ValueError("wellness runtime timestamp is invalid")
    if value["weight_mode_enabled"] and value["weight_mode_enabled_at"] is None:
        raise ValueError("enabled weight mode requires an opt-in timestamp")
    for key in ("last_sample_id", "source_date", "current_episode_id", "last_reminder_sample_id", "last_reminder_episode_id", "schedule_week", "last_reason_code", "sunlight_check_date"):
        raw = value.get(key)
        if raw is not None and (not isinstance(raw, str) or len(raw) > 128):
            raise ValueError("wellness runtime metadata is invalid")
    slots = value.get("weekly_slots")
    used = value.get("used_slots")
    if not isinstance(slots, list) or len(slots) > 7 or not isinstance(used, list) or len(used) > 16:
        raise ValueError("wellness runtime schedule is invalid")
    for raw in slots + used:
        if not isinstance(raw, str) or len(raw) > 64 or _aware(raw) is None:
            raise ValueError("wellness runtime slot is invalid")
    return deepcopy(value)


def _aware(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _int_or_none(value: object, low: int, high: int) -> bool:
    return value is None or (type(value) is int and low <= value <= high)


def _configured(rule: object) -> tuple[str, str]:
    if not isinstance(rule, dict) or rule.get("enabled") is not True:
        return "disabled", "身心状态关怀已关闭"
    if rule.get("mode") == "unconfirmed":
        return "parameters_unconfirmed", "低值判定方式尚未确认"
    if rule.get("timezone_confirmed") is not True:
        return "timezone_unconfirmed", "设备时区尚未确认"
    if not isinstance(rule.get("mode"), str) or rule.get("mode") not in {"numeric", "slow_down_category"}:
        return "invalid_settings", "低值判定方式无效"
    if rule.get("mode") == "numeric":
        low, recovery = rule.get("low_score_threshold"), rule.get("recovery_score_threshold")
        if not (type(low) is int and 0 <= low <= 999 and type(recovery) is int
                and 0 <= recovery <= 999 and recovery > low):
            return "parameters_unconfirmed", "低值阈值或恢复阈值尚未确认"
    required = (
        "confirmation_minutes", "minimum_independent_samples", "recovery_debounce_minutes",
        "maximum_sample_age_minutes", "repeat_cooldown_minutes",
    )
    if any(rule.get(key) is None for key in required):
        return "parameters_unconfirmed", "持续时长、独立样本数、恢复去抖或间隔尚未确认"
    ranges = {
        "confirmation_minutes": (1, 1440), "minimum_independent_samples": (2, 100),
        "recovery_debounce_minutes": (1, 1440), "maximum_sample_age_minutes": (1, 1440),
        "repeat_cooldown_minutes": (1, 10080),
    }
    if any(not _int_or_none(rule.get(key), *bounds) for key, bounds in ranges.items()):
        return "invalid_settings", "持续低值参数超出支持范围"
    try:
        ZoneInfo(rule.get("timezone"))
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return "invalid_timezone", "设备时区无效"
    return "ready", "等待有效的独立来源样本"


def sample_from_capture(
    capture: object,
    rule: dict[str, Any],
    now: datetime,
) -> tuple[WellnessSample | None, str]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    try:
        zone = ZoneInfo(rule["timezone"])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return None, "invalid_timezone"
    pages = capture.get("pages") if isinstance(capture, dict) else None
    page = pages.get("wellness_home") if isinstance(pages, dict) else None
    data = page.get("data") if isinstance(page, dict) and page.get("status") == "ok" else None
    if (not isinstance(data, dict) or data.get("source") != "oppo_health_ui_ocr"
            or data.get("page") != "wellness_home"):
        return None, "source_unavailable"
    updated_at = _aware(data.get("observed_at"))
    if updated_at is None:
        return None, "missing_update_time"
    if updated_at > now:
        return None, "future_update_time"
    try:
        data_date = date.fromisoformat(data.get("date"))
    except (TypeError, ValueError):
        return None, "missing_data_date"
    if data_date != now.astimezone(zone).date():
        return None, "cross_day_data"
    metrics = data.get("metrics")
    if not isinstance(metrics, dict) or metrics.get("kind") != "home_point":
        return None, "unknown_measurement_kind"
    category = metrics.get("category")
    if not isinstance(category, str) or category not in CATEGORIES:
        return None, "missing_or_invalid_category"
    score = metrics.get("score")
    if score is not None and (type(score) is not int or not 0 <= score <= 999):
        return None, "missing_or_invalid_score"
    if rule.get("mode") == "numeric" and score is None:
        return None, "missing_or_invalid_score"
    point_time = metrics.get("point_time_local")
    if not isinstance(point_time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", point_time):
        return None, "missing_source_time"
    measured_at = datetime.combine(data_date, time.fromisoformat(point_time), tzinfo=zone)
    local_now = now.astimezone(zone)
    if measured_at > local_now:
        return None, "future_source_time"
    maximum_age = rule.get("maximum_sample_age_minutes")
    if type(maximum_age) is not int or not 1 <= maximum_age <= 1440:
        return None, "parameters_unconfirmed"
    freshness = timedelta(minutes=maximum_age)
    if local_now - measured_at > freshness or now - updated_at > freshness:
        return None, "stale_source_sample"
    sample_id = f"{data_date.isoformat()}T{point_time}@{rule['timezone']}"
    quality_value = metrics.get("quality_label") or metrics.get("quality")
    quality = quality_value.strip()[:64] if isinstance(quality_value, str) and quality_value.strip() else "来源未提供质量标签"
    completeness = (
        "解析成功：日期、实际测量时间、更新时间和分类齐全；来源未提供可核验质量标签"
        if quality == "来源未提供质量标签"
        else "解析成功：日期、实际测量时间、更新时间和分类齐全；标签未独立核验"
    )
    return WellnessSample(sample_id, score, category, measured_at, updated_at, data_date, quality, completeness), "ok"


def wellness_source_summary(capture: object, timezone: str, now: datetime) -> dict[str, Any]:
    """Expose actual page labels and clocks without claiming a valid sample."""
    pages = capture.get("pages") if isinstance(capture, dict) else None
    page = pages.get("wellness_home") if isinstance(pages, dict) else None
    data = page.get("data") if isinstance(page, dict) and page.get("status") == "ok" else None
    summary: dict[str, Any] = {
        "source": "wellness_low_state", "status": "unavailable", "data_date": None,
        "observed_at": None, "measured_at": None, "category": None,
        "quality": "来源未提供质量标签", "completeness": "缺少来源字段",
        "evaluation_state": None, "reason": "状态页来源暂不可用", "is_stale": None,
    }
    if (not isinstance(data, dict) or data.get("source") != "oppo_health_ui_ocr"
            or data.get("page") != "wellness_home"):
        return summary
    summary["status"] = "ok"
    try:
        summary["data_date"] = date.fromisoformat(data.get("date")).isoformat()
    except (TypeError, ValueError):
        summary["status"] = "error"
    updated = _aware(data.get("observed_at"))
    summary["observed_at"] = updated.isoformat() if updated else None
    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    point_time = metrics.get("point_time_local")
    if isinstance(point_time, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", point_time):
        summary["measured_at"] = f"{summary['data_date'] or '未知日期'} {point_time}（设备本地时间）"
    category = metrics.get("category")
    summary["category"] = category if isinstance(category, str) and category in CATEGORIES else None
    quality = metrics.get("quality_label") or metrics.get("quality")
    if isinstance(quality, str) and len(quality) <= 64:
        summary["quality"] = quality
    quality_available = isinstance(quality, str) and bool(quality.strip())
    kind_valid = metrics.get("kind") == "home_point"
    required = (summary["data_date"], updated, summary["category"], point_time, kind_valid)
    if all(required):
        summary["completeness"] = (
            "解析成功：记录日期、更新时间、测量时间和分类齐全；"
            + ("质量标签未独立核验" if quality_available else "来源未提供可核验质量标签")
        )
        summary["reason"] = (
            "来源字段可解析；测量质量无法由当前来源标签独立证明"
            if not quality_available else "来源字段可解析；质量标签不等同于独立测量验证"
        )
    else:
        summary["completeness"] = "记录日期、更新时间、实际测量时间、测量分类或测量点标记不完整"
        summary["reason"] = "来源缺少必要字段，已暂停解释"
    try:
        local_now = now.astimezone(ZoneInfo(timezone))
        today = local_now.date().isoformat()
        summary["is_stale"] = summary["data_date"] is not None and summary["data_date"] != today
        if updated is None or updated > now or now - updated > timedelta(minutes=30):
            summary["is_stale"] = True
        if summary["is_stale"]:
            summary.update(status="stale", reason="来源日期或页面更新时间已过期")
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        summary["is_stale"] = None
    return summary


def _low(sample: WellnessSample, rule: dict[str, Any]) -> bool:
    if rule.get("mode") == "numeric":
        return sample.score is not None and sample.score < rule["low_score_threshold"]
    return sample.category == "Slow down"


def _recovered(sample: WellnessSample, rule: dict[str, Any]) -> bool:
    if rule.get("mode") == "numeric":
        return sample.score is not None and sample.score >= rule["recovery_score_threshold"]
    return sample.category != "Slow down"


def _clear_streaks(state: dict[str, Any]) -> None:
    state.update(low_started_at=None, low_sample_count=0, recovery_started_at=None, recovery_sample_count=0)


def evaluate_capture(
    capture: object,
    rule: object,
    current_state: object,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any], WellnessSample | None]:
    """Consume one new verified source point and return metadata-only status."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    state = validate_runtime_state(current_state) if isinstance(current_state, dict) else empty_runtime_state()
    config_state, config_reason = _configured(rule)
    base_status = {
        "status": config_state,
        "reason_code": config_state,
        "reason": config_reason,
        "data_date": None,
        "measured_at": None,
        "observed_at": None,
        "category": None,
        "quality": "未知",
        "completeness": "未知",
        "sample_id": None,
        "low_confirmed": False,
    }
    if config_state != "ready" or not isinstance(rule, dict):
        state["last_reason_code"] = config_state
        return state, base_status, None
    sample, sample_reason = sample_from_capture(capture, rule, now)
    if sample is None:
        _clear_streaks(state)
        state["last_reason_code"] = sample_reason
        return state, {**base_status, "status": "paused_source", "reason_code": sample_reason,
                       "reason": _REASONS.get(sample_reason, "来源暂不可用")}, None
    status = {
        **base_status,
        "data_date": sample.data_date.isoformat(),
        "measured_at": sample.measured_at.isoformat(),
        "observed_at": sample.updated_at.isoformat(),
        "category": sample.category,
        "quality": sample.quality,
        "completeness": sample.completeness,
        "sample_id": sample.sample_id,
    }
    if sample.sample_id == state.get("last_sample_id"):
        state["last_reason_code"] = "duplicate_source_sample"
        duplicate_low = (not state["armed"] and bool(state.get("current_episode_id")) and _low(sample, rule))
        low_started = _aware(state.get("low_started_at"))
        duplicate_elapsed = sample.measured_at - low_started if low_started else timedelta(0)
        duplicate_confirmed = bool(
            duplicate_low
            and state.get("low_sample_count", 0) >= rule["minimum_independent_samples"]
            and duplicate_elapsed >= timedelta(minutes=rule["confirmation_minutes"])
        )
        return state, {**status, "status": "duplicate_sample", "reason_code": "duplicate_source_sample",
                       "reason": "仍是同一条测量记录，未增加样本",
                       "low_confirmed": duplicate_confirmed}, sample
    previous_at = _aware(state.get("last_sample_at"))
    if previous_at is not None and sample.measured_at <= previous_at:
        _clear_streaks(state)
        state["last_reason_code"] = "out_of_order_source_sample"
        return state, {**status, "status": "paused_source", "reason_code": "out_of_order_source_sample",
                       "reason": "来源测量时间没有向前，已暂停解释"}, sample
    previous_date = state.get("source_date")
    if previous_date != sample.data_date.isoformat() or (
        previous_at is not None and sample.measured_at - previous_at > timedelta(minutes=rule["maximum_sample_age_minutes"])
    ):
        _clear_streaks(state)
    state.update(
        last_sample_id=sample.sample_id,
        last_sample_at=sample.measured_at.isoformat(),
        source_date=sample.data_date.isoformat(),
    )

    low_now = _low(sample, rule)
    low_start = _aware(state.get("low_started_at"))
    if not state["armed"] and state.get("current_episode_id"):
        if _recovered(sample, rule):
            if state.get("recovery_started_at") is None:
                state["recovery_started_at"] = sample.measured_at.isoformat()
                state["recovery_sample_count"] = 1
            else:
                state["recovery_sample_count"] += 1
            recovery_start = _aware(state.get("recovery_started_at"))
            elapsed = sample.measured_at - recovery_start if recovery_start else timedelta(0)
            if state["recovery_sample_count"] >= 2 and elapsed >= timedelta(minutes=rule["recovery_debounce_minutes"]):
                state.update(armed=True, current_episode_id=None, last_reason_code="recovered")
                _clear_streaks(state)
                return state, {**status, "status": "recovered", "reason_code": "recovery_confirmed",
                               "reason": "恢复条件已连续满足；等待下一段新低值"}, sample
        else:
            state["recovery_started_at"] = None
            state["recovery_sample_count"] = 0
        low_confirmed = False
        if low_now:
            if low_start is None:
                state["low_started_at"] = sample.measured_at.isoformat()
                state["low_sample_count"] = 1
            else:
                state["low_sample_count"] += 1
            low_start = _aware(state.get("low_started_at"))
            elapsed = sample.measured_at - low_start if low_start else timedelta(0)
            low_confirmed = (
                state["low_sample_count"] >= rule["minimum_independent_samples"]
                and elapsed >= timedelta(minutes=rule["confirmation_minutes"])
            )
        else:
            state["low_started_at"] = None
            state["low_sample_count"] = 0
        state["last_reason_code"] = "episode_active" if low_now else "episode_active_waiting_low_sample"
        return state, {**status, "status": "episode_latched", "reason_code": state["last_reason_code"],
                       "reason": "同一持续状态仍在跟踪；需要新的有效样本及间隔后才可能再次关怀",
                       "low_confirmed": low_confirmed}, sample

    if low_now:
        if low_start is None:
            state["low_started_at"] = sample.measured_at.isoformat()
            state["low_sample_count"] = 1
        else:
            state["low_sample_count"] += 1
        low_start = _aware(state.get("low_started_at"))
        elapsed = sample.measured_at - low_start if low_start else timedelta(0)
        confirmed = (
            state["low_sample_count"] >= rule["minimum_independent_samples"]
            and elapsed >= timedelta(minutes=rule["confirmation_minutes"])
        )
        if confirmed:
            event_seed = str(state["low_started_at"])
            episode_id = hashlib.sha256(f"oppo-health-low-state\0{event_seed}".encode()).hexdigest()
            state.update(armed=False, current_episode_id=episode_id, last_reason_code="low_episode_confirmed")
            return state, {**status, "status": "low_confirmed", "reason_code": "low_episode_confirmed",
                           "reason": "持续低状态条件已满足，等待随机关怀时段",
                           "low_confirmed": True}, sample
        state["last_reason_code"] = "awaiting_duration_and_samples"
        return state, {**status, "status": "confirming_low_state", "reason_code": state["last_reason_code"],
                       "reason": "正在等待足够时长和独立测量样本"}, sample

    _clear_streaks(state)
    state["last_reason_code"] = "waiting_for_low_state"
    return state, {**status, "status": "waiting_for_low_state", "reason_code": state["last_reason_code"],
                   "reason": "当前有效记录未达到已确认的低值条件"}, sample


def ensure_weekly_slots(
    state: dict[str, Any],
    now: datetime,
    rule: dict[str, Any],
    *,
    chooser: Any = random.SystemRandom(),
) -> dict[str, Any]:
    """Persist a random set of weekly times inside the chosen local window."""
    zone = ZoneInfo(rule["timezone"])
    local_now = now.astimezone(zone)
    monday = local_now.date() - timedelta(days=local_now.weekday())
    week_id = f"{monday.isocalendar().year}-W{monday.isocalendar().week:02d}"
    if state.get("schedule_week") == week_id and len(state.get("weekly_slots", [])) == rule["weekly_target"]:
        return state
    start = time.fromisoformat(rule["send_window"]["start"])
    end = time.fromisoformat(rule["send_window"]["end"])
    span = int((datetime.combine(monday, end) - datetime.combine(monday, start)).total_seconds() // 60)
    if span < 1:
        raise ValueError("wellness send window has no minutes")
    population = 7 * span
    if rule["weekly_target"] > population:
        raise ValueError("weekly target exceeds the available send minutes")
    selections = sorted(chooser.sample(range(population), rule["weekly_target"]))
    slots = []
    for selection in selections:
        day_offset, minute_offset = divmod(selection, span)
        slot_day = monday + timedelta(days=day_offset)
        slot = datetime.combine(slot_day, start, tzinfo=zone) + timedelta(minutes=minute_offset)
        slots.append(slot.isoformat())
    state.update(schedule_week=week_id, weekly_slots=slots, used_slots=[])
    return state


def weekly_candidate(
    state: dict[str, Any],
    sample: WellnessSample | None,
    status: dict[str, Any],
    rule: dict[str, Any],
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    """Claim one due random slot only for a fresh, new sample in active low state."""
    state = ensure_weekly_slots(state, now, rule)
    zone = ZoneInfo(rule["timezone"])
    local_now = now.astimezone(zone)
    end_of_window = time.fromisoformat(rule["send_window"]["end"])
    last_reminder = _aware(state.get("last_reminder_at"))
    cooldown = timedelta(minutes=rule["repeat_cooldown_minutes"])
    for slot_text in state["weekly_slots"]:
        slot = _aware(slot_text)
        if slot is None or slot_text in state["used_slots"]:
            continue
        slot_end = min(slot + timedelta(minutes=15), datetime.combine(slot.date(), end_of_window, tzinfo=zone))
        if now < slot or now > slot_end:
            if now > slot_end:
                state["used_slots"].append(slot_text)
            continue
        state["used_slots"].append(slot_text)
        if status.get("low_confirmed") is not True or sample is None or not state.get("current_episode_id"):
            return state, None, "随机时段已到；当前没有满足持续条件的新样本"
        if sample.sample_id == state.get("last_reminder_sample_id"):
            return state, None, "这条测量记录已用于此前关怀"
        if last_reminder is not None and now - last_reminder < cooldown:
            return state, None, "同一持续状态仍在提醒间隔内"
        slot_key = slot.strftime("%Y%m%dT%H%M%z")
        event_id = hashlib.sha256(
            f"{state['current_episode_id']}\0{sample.sample_id}\0{slot_key}".encode()
        ).hexdigest()
        state.update(
            last_reminder_at=now.isoformat(),
            last_reminder_sample_id=sample.sample_id,
            last_reminder_episode_id=state["current_episode_id"],
            last_reason_code="reminder_reserved",
        )
        fact = {
            "event_id": event_id,
            "kind": "sustained_low_state",
            "data_date": sample.data_date.isoformat(),
            "observed_at": sample.updated_at.isoformat(),
            "candidate_at": slot.isoformat(),
            "candidate_window_end": slot_end.isoformat(),
        }
        return state, fact, "满足持续低值条件并占用一个随机时段"
    next_slot = next((slot for slot in state["weekly_slots"] if slot not in state["used_slots"]), None)
    reason = "等待下一个随机关怀时段" if next_slot else "本周随机时段已用完"
    return state, None, reason


def sunlight_candidate(
    capture: object,
    rule: dict[str, Any],
    current_state: dict[str, Any],
    now: datetime,
    *,
    chooser: Any = random.SystemRandom(),
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    """Check one random minute from 20:00–20:59 using only explicit same-day data."""
    state = validate_runtime_state(current_state)
    zone = ZoneInfo(rule["timezone"])
    local_now = now.astimezone(zone)
    today = local_now.date()
    if state.get("sunlight_check_date") != today.isoformat():
        minute = chooser.randrange(60)
        slot = datetime.combine(today, time(20, 0), tzinfo=zone) + timedelta(minutes=minute)
        state.update(sunlight_check_date=today.isoformat(), sunlight_check_at=slot.isoformat(), sunlight_used=False)
    slot = _aware(state.get("sunlight_check_at"))
    end = datetime.combine(today, time(21, 0), tzinfo=zone)
    summary: dict[str, Any] = {
        "source": "sunlight_evening", "status": "unavailable", "data_date": None,
        "observed_at": None, "measured_at": None, "category": None,
        "quality": "来源未提供质量标签", "completeness": "未知",
        "evaluation_state": "paused_source", "reason": "当天日照记录暂不可用",
        "is_stale": None,
    }
    pages = capture.get("pages") if isinstance(capture, dict) else None
    page = pages.get("sun_exposure") if isinstance(pages, dict) else None
    data = page.get("data") if isinstance(page, dict) and page.get("status") == "ok" else None
    updated = _aware(data.get("observed_at")) if isinstance(data, dict) else None
    metrics = data.get("metrics") if isinstance(data, dict) and isinstance(data.get("metrics"), dict) else {}
    try:
        data_date = date.fromisoformat(data.get("date")) if isinstance(data, dict) else None
    except (TypeError, ValueError):
        data_date = None
    if isinstance(data, dict) and data.get("source") == "oppo_health_ui_ocr" and data.get("page") == "sun_exposure":
        summary.update(
            status="ok" if data_date else "error",
            data_date=data_date.isoformat() if data_date else None,
            observed_at=updated.isoformat() if updated else None,
            completeness="日期、更新时间、分钟数齐全" if data_date and updated and metrics.get("unit") == "min" and "duration_min" in metrics else "日照字段不完整",
        )
    if state.get("sunlight_used"):
        summary.update(evaluation_state="skipped", reason="今天的随机检查时段已处理")
        return state, None, summary
    if slot is None or now < slot:
        summary.update(evaluation_state="waiting_for_window", reason="等待 20:00–21:00 内的随机检查时段")
        return state, None, summary
    check_end = min(slot + timedelta(minutes=15), end)
    if now > check_end:
        state["sunlight_used"] = True
        summary.update(status="stale", evaluation_state="skipped", reason="错过今天的随机检查时段")
        return state, None, summary
    if (not isinstance(data, dict) or data.get("source") != "oppo_health_ui_ocr"
            or data.get("page") != "sun_exposure"):
        summary["reason"] = "当天日照记录不可用；缺记录不按 0 分钟处理"
        return state, None, summary
    if (data_date != today or updated is None or updated > now
            or now - updated > timedelta(minutes=rule["duplicate_window_minutes"])):
        summary.update(status="stale", evaluation_state="paused_source", reason="日照记录日期或更新时间已过期")
        return state, None, summary
    duration = metrics.get("duration_min")
    if (metrics.get("unit") != "min" or isinstance(duration, bool)
            or not isinstance(duration, (int, float)) or not math.isfinite(float(duration)) or duration < 0):
        summary.update(status="unavailable", evaluation_state="paused_source", reason="日照分钟数或单位不完整")
        return state, None, summary
    state["sunlight_used"] = True
    if duration > 5:
        summary.update(evaluation_state="skipped", reason="当天明确记录超过 5 分钟")
        return state, None, summary
    fact = {
        "event_id": f"sunlight:{today.isoformat()}",
        "kind": "sunlight_opportunity",
        "data_date": today.isoformat(),
        "observed_at": updated.isoformat(),
        "candidate_at": slot.isoformat(),
        "candidate_window_end": check_end.isoformat(),
    }
    summary.update(evaluation_state="candidate", reason="当天记录为 0–5 分钟，已安排一次温柔问候")
    return state, fact, summary


_REASONS = {
    "source_unavailable": "状态页来源暂不可用",
    "missing_update_time": "来源未提供有效更新时间",
    "future_update_time": "来源更新时间晚于当前时间",
    "missing_data_date": "来源未提供有效数据日期",
    "cross_day_data": "来源数据不是设备今天的记录",
    "unknown_measurement_kind": "来源是日均值而不是独立测量点",
    "missing_or_invalid_category": "来源分类缺失或无效",
    "missing_or_invalid_score": "数值分值缺失或无效",
    "missing_source_time": "来源未提供测量时间；不会用轮询时间代替",
    "future_source_time": "测量时间晚于当前时间",
    "stale_source_sample": "测量点或页面更新时间已过期",
    "out_of_order_source_sample": "来源测量时间没有向前，已暂停解释",
}


def explicit_weight_mode_command(text: object, *, own_private_plain_message: bool) -> str | None:
    """Recognize only direct, self-authored, unquoted mode commands."""
    if not own_private_plain_message or not isinstance(text, str) or "\n" in text or "\r" in text:
        return None
    if any(mark in text for mark in ('"', "'", "“", "”", "‘", "’", "「", "」", "『", "』", "<", ">", "《", "》")):
        return None
    normalized = re.sub(r"[，。！？!?.,；;：:]+$", "", text.strip())
    if normalized in {"我不减肥", "我不减肥了", "停止减肥模式", "关闭减肥模式"}:
        return "deactivate"
    if normalized in {"我想减肥", "我要减肥", "开启减肥模式", "开始减肥模式"}:
        return "activate"
    return None
