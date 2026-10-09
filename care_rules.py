"""Pure validation and dry-run planning for configurable health care rules."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


RULE_IDS = (
    "predicted_period_lead",
    "confirmed_period_start",
    "confirmed_period_end",
    "weight_date_linked",
)
_RULE_FACTS = {
    "predicted_period_lead": "predicted_period",
    "confirmed_period_start": "confirmed_period_start",
    "confirmed_period_end": "confirmed_period_end",
    "weight_date_linked": "weight_entry",
}
_TONES = {"gentle", "brief"}
_DEDUPLICATION = {"per_event", "per_local_date"}
_RESCHEDULE = {"next_allowed_window", "skip_if_late"}


def _rule_default(*, lead_days: int | None = None) -> dict[str, Any]:
    rule = {
        "enabled": False,
        "timezone": "Asia/Shanghai",
        "send_window": {"start": "09:00", "end": "21:00"},
        "max_per_day": 1,
        "duplicate_window_minutes": 30,
        "quiet_hours": {"start": "22:00", "end": "08:00"},
        "tone": "gentle",
        "stale_after_hours": 48,
        "deduplication": "per_event",
        "reschedule": "next_allowed_window",
    }
    if lead_days is not None:
        rule["lead_days"] = lead_days
    return rule


def default_config() -> dict[str, Any]:
    """Return schema-v1 defaults with every new care rule disabled.

    Returns:
        A fresh JSON-serializable default configuration.
    """
    return {
        "schema_version": 1,
        "rules": {
            "predicted_period_lead": _rule_default(lead_days=3),
            "confirmed_period_start": _rule_default(),
            "confirmed_period_end": _rule_default(),
            "weight_date_linked": _rule_default(),
        },
    }


def _clock_minutes(value: object, field: str) -> int:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError(f"{field} must use HH:MM")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must use HH:MM") from exc
    if parsed.second or parsed.microsecond:
        raise ValueError(f"{field} must use HH:MM")
    return parsed.hour * 60 + parsed.minute


def _validate_window(value: object, field: str, *, overnight: bool) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise ValueError(f"{field} must contain start and end")
    start = value["start"]
    end = value["end"]
    start_minutes = _clock_minutes(start, f"{field}.start")
    end_minutes = _clock_minutes(end, f"{field}.end")
    if start_minutes == end_minutes or (not overnight and start_minutes > end_minutes):
        raise ValueError(f"{field} has an invalid time range")
    return {"start": start, "end": end}


def _inside_quiet_hours(minute: int, start: int, end: int) -> bool:
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def _ensure_send_window_respects_quiet_hours(
    send_window: dict[str, str], quiet_hours: dict[str, str], rule_id: str
) -> None:
    start = _clock_minutes(send_window["start"], "send_window.start")
    end = _clock_minutes(send_window["end"], "send_window.end")
    quiet_start = _clock_minutes(quiet_hours["start"], "quiet_hours.start")
    quiet_end = _clock_minutes(quiet_hours["end"], "quiet_hours.end")
    if any(_inside_quiet_hours(minute, quiet_start, quiet_end) for minute in range(start, end)):
        raise ValueError(f"{rule_id} send window overlaps quiet hours")


def validate_config(payload: object) -> dict[str, Any]:
    """Validate and normalize the versioned care-rule configuration.

    Args:
        payload: JSON-compatible configuration received from the page.

    Returns:
        A new configuration containing only supported fields.

    Raises:
        ValueError: If the payload contains unsupported or invalid values.
    """
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "rules"}:
        raise ValueError("configuration must contain schema_version and rules")
    if payload.get("schema_version") != 1 or isinstance(payload.get("schema_version"), bool):
        raise ValueError("unsupported care-rule schema version")
    rules = payload.get("rules")
    if not isinstance(rules, dict) or set(rules) != set(RULE_IDS):
        raise ValueError("configuration must include all supported rule types")

    normalized: dict[str, Any] = {"schema_version": 1, "rules": {}}
    expected_fields = {
        "enabled", "timezone", "send_window", "max_per_day", "duplicate_window_minutes", "quiet_hours",
        "tone", "stale_after_hours", "deduplication", "reschedule",
    }
    for rule_id in RULE_IDS:
        rule = rules[rule_id]
        if not isinstance(rule, dict):
            raise ValueError(f"{rule_id} must be an object")
        fields = expected_fields | ({"lead_days"} if rule_id == "predicted_period_lead" else set())
        if set(rule) != fields:
            raise ValueError(f"{rule_id} contains missing or unsupported fields")
        if not isinstance(rule["enabled"], bool):
            raise ValueError(f"{rule_id}.enabled must be boolean")
        timezone_name = rule["timezone"]
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            raise ValueError(f"{rule_id}.timezone is required")
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"{rule_id}.timezone is not a known IANA timezone") from exc

        send_window = _validate_window(rule["send_window"], f"{rule_id}.send_window", overnight=False)
        quiet_hours = _validate_window(rule["quiet_hours"], f"{rule_id}.quiet_hours", overnight=True)
        _ensure_send_window_respects_quiet_hours(send_window, quiet_hours, rule_id)

        max_per_day = rule["max_per_day"]
        duplicate_window_minutes = rule["duplicate_window_minutes"]
        stale_after_hours = rule["stale_after_hours"]
        if isinstance(max_per_day, bool) or not isinstance(max_per_day, int) or not 1 <= max_per_day <= 5:
            raise ValueError(f"{rule_id}.max_per_day must be between 1 and 5")
        if (isinstance(duplicate_window_minutes, bool)
                or not isinstance(duplicate_window_minutes, int)
                or not 1 <= duplicate_window_minutes <= 1440):
            raise ValueError(f"{rule_id}.duplicate_window_minutes must be between 1 and 1440")
        if (isinstance(stale_after_hours, bool) or not isinstance(stale_after_hours, int)
                or not 1 <= stale_after_hours <= 336):
            raise ValueError(f"{rule_id}.stale_after_hours must be between 1 and 336")
        if not isinstance(rule["tone"], str) or rule["tone"] not in _TONES:
            raise ValueError(f"{rule_id}.tone is unsupported")
        if (not isinstance(rule["deduplication"], str)
                or rule["deduplication"] not in _DEDUPLICATION):
            raise ValueError(f"{rule_id}.deduplication is unsupported")
        if not isinstance(rule["reschedule"], str) or rule["reschedule"] not in _RESCHEDULE:
            raise ValueError(f"{rule_id}.reschedule is unsupported")

        normalized_rule = {
            "enabled": rule["enabled"],
            "timezone": timezone_name,
            "send_window": send_window,
            "max_per_day": max_per_day,
            "duplicate_window_minutes": duplicate_window_minutes,
            "quiet_hours": quiet_hours,
            "tone": rule["tone"],
            "stale_after_hours": stale_after_hours,
            "deduplication": rule["deduplication"],
            "reschedule": rule["reschedule"],
        }
        if rule_id == "predicted_period_lead":
            lead_days = rule["lead_days"]
            if isinstance(lead_days, bool) or not isinstance(lead_days, int) or not 1 <= lead_days <= 14:
                raise ValueError("predicted_period_lead.lead_days must be between 1 and 14")
            normalized_rule["lead_days"] = lead_days
        normalized["rules"][rule_id] = normalized_rule

    return normalized


def _parse_aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def build_preview_plan(
    config: object, event_facts: object, now: datetime
) -> dict[str, Any]:
    """Build a deterministic, side-effect-free preview from explicit event facts.

    The result is descriptive only. It does not reserve deduplication state,
    read health sources, call a model, enqueue an event, or send a message.

    Args:
        config: Versioned rule configuration.
        event_facts: Explicit, already-read source facts keyed by rule source.
        now: Time used to evaluate freshness; it must be timezone-aware.

    Returns:
        A preview result containing one state per rule.

    Raises:
        ValueError: If config or now is invalid.
    """
    validated = validate_config(config)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not isinstance(event_facts, dict):
        event_facts = {}

    plans = []
    for rule_id in RULE_IDS:
        rule = validated["rules"][rule_id]
        row: dict[str, Any] = {
            "rule_id": rule_id,
            "enabled": rule["enabled"],
            "state": "disabled",
            "reason": "规则未开启",
            "expected_at": None,
            "expected_window_end": None,
            "timezone": rule["timezone"],
            "deduplication": rule["deduplication"],
            "reschedule": rule["reschedule"],
            "preview_only": True,
            "is_actual_event": False,
        }
        if not rule["enabled"]:
            plans.append(row)
            continue

        fact = event_facts.get(_RULE_FACTS[rule_id])
        if not isinstance(fact, dict):
            row.update(state="no_source", reason="没有可用的来源记录")
            plans.append(row)
            continue

        observed_at = _parse_aware_datetime(fact.get("observed_at"))
        if observed_at is None or observed_at > now:
            row.update(state="invalid_source", reason="来源读取时间无效")
            plans.append(row)
            continue
        local_zone = ZoneInfo(rule["timezone"])
        freshness_at = observed_at
        source_date = None
        occurred_at = None
        if rule_id == "predicted_period_lead":
            if fact.get("kind") != "prediction":
                row.update(state="invalid_source", reason="需要明确标记为预测的数据")
                plans.append(row)
                continue
            source_date = _parse_date(fact.get("predicted_start_date"))
            if source_date is None:
                row.update(state="invalid_source", reason="预测日期无效")
                plans.append(row)
                continue
        elif rule_id in {"confirmed_period_start", "confirmed_period_end"}:
            if fact.get("confirmed") is not True:
                row.update(
                    state="not_confirmed",
                    reason="没有明确确认标记；不会根据日期推断",
                )
                plans.append(row)
                continue
            occurred_at = _parse_aware_datetime(fact.get("occurred_at"))
            if occurred_at is None or occurred_at > now:
                row.update(state="invalid_source", reason="确认事件时间无效")
                plans.append(row)
                continue
            freshness_at = occurred_at
        else:
            source_date = _parse_date(fact.get("record_date"))
            if source_date is None:
                row.update(state="invalid_source", reason="体重记录日期无效")
                plans.append(row)
                continue
            if source_date > now.astimezone(local_zone).date():
                row.update(state="invalid_source", reason="体重记录日期晚于当前日期")
                plans.append(row)
                continue
            recorded_at = _parse_aware_datetime(fact.get("recorded_at"))
            if fact.get("recorded_at") is not None and recorded_at is None:
                row.update(state="invalid_source", reason="体重记录时间无效")
                plans.append(row)
                continue
            if recorded_at is not None and recorded_at.astimezone(local_zone).date() != source_date:
                row.update(state="invalid_source", reason="体重记录时间与记录日期不一致")
                plans.append(row)
                continue
            freshness_at = recorded_at or datetime.combine(source_date, time.min, tzinfo=local_zone)
            if freshness_at > now:
                row.update(state="invalid_source", reason="体重记录时间晚于当前时间")
                plans.append(row)
                continue
        age = now.astimezone(local_zone) - freshness_at.astimezone(local_zone)
        if age > timedelta(hours=rule["stale_after_hours"]):
            row.update(state="stale_source", reason="来源记录已过期")
            plans.append(row)
            continue

        if rule_id == "predicted_period_lead":
            target_date = source_date - timedelta(days=rule["lead_days"])
            basis = "预测经期日期"
            is_actual = False
        elif rule_id in {"confirmed_period_start", "confirmed_period_end"}:
            target_date = occurred_at.astimezone(ZoneInfo(rule["timezone"])).date()
            basis = "明确确认的经期事件"
            is_actual = True
        else:
            target_date = source_date
            basis = "体重记录日期"
            is_actual = True

        window_start = time.fromisoformat(rule["send_window"]["start"])
        window_end = time.fromisoformat(rule["send_window"]["end"])
        start_at = datetime.combine(target_date, window_start, tzinfo=local_zone)
        end_at = datetime.combine(target_date, window_end, tzinfo=local_zone)
        row.update(
            state="preview_only",
            reason="仅按配置生成的时间预览；尚未检查运行时去重或频率状态",
            expected_at=start_at.isoformat(),
            expected_window_end=end_at.isoformat(),
            basis=basis,
            source_date=(source_date.isoformat() if rule_id == "predicted_period_lead" else target_date.isoformat()),
            target_date=target_date.isoformat(),
            is_actual_event=is_actual,
        )
        plans.append(row)

    return {
        "preview_only": True,
        "evaluated_at": now.isoformat(),
        "plans": plans,
        "notice": "这是规则预览，不会发送消息；预测日期与实际确认事件分开展示。",
    }
