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
    "period_late_inquiry",
    "weight_date_linked",
    "wellness_low_state",
    "sunlight_evening",
)
_RULE_FACTS = {
    "predicted_period_lead": "predicted_period",
    "confirmed_period_start": "confirmed_period_start",
    "confirmed_period_end": "confirmed_period_end",
    "period_late_inquiry": "period_late_inquiry",
    "weight_date_linked": "weight_entry",
    "wellness_low_state": "wellness_low_state",
    "sunlight_evening": "sunlight_evening",
}
_TONES = {"gentle", "brief"}
_DEDUPLICATION = {"per_event", "per_local_date"}


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
        "reschedule": "skip_if_late",
    }
    if lead_days is not None:
        rule["lead_days"] = lead_days
    return rule


def _wellness_rule_default() -> dict[str, Any]:
    """Return a closed status rule with deliberately unconfirmed thresholds."""
    return {
        **_rule_default(),
        "max_per_day": 7,
        "mode": "unconfirmed",
        "timezone_confirmed": False,
        "low_score_threshold": None,
        "confirmation_minutes": None,
        "minimum_independent_samples": None,
        "recovery_score_threshold": None,
        "recovery_debounce_minutes": None,
        "maximum_sample_age_minutes": None,
        "repeat_cooldown_minutes": None,
        "weekly_target": 5,
    }


def _period_late_inquiry_default() -> dict[str, Any]:
    """Return a closed, explicitly event-gated late-cycle inquiry rule."""
    return {
        **_rule_default(),
        "start_day": 4,
        "probability": 0.5,
        "cooldown_days": 1,
        "observation_window_days": 14,
    }


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
            "period_late_inquiry": _period_late_inquiry_default(),
            "weight_date_linked": _rule_default(),
            "wellness_low_state": _wellness_rule_default(),
            "sunlight_evening": {
                **_rule_default(),
                "send_window": {"start": "20:00", "end": "21:00"},
                "quiet_hours": {"start": "21:00", "end": "08:00"},
                "max_per_day": 1,
            },
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
        extra_fields = set()
        if rule_id == "predicted_period_lead":
            extra_fields = {"lead_days"}
        elif rule_id == "period_late_inquiry":
            extra_fields = {"start_day", "probability", "cooldown_days", "observation_window_days"}
        elif rule_id == "wellness_low_state":
            extra_fields = {
                "mode", "timezone_confirmed", "low_score_threshold", "confirmation_minutes",
                "minimum_independent_samples", "recovery_score_threshold",
                "recovery_debounce_minutes", "maximum_sample_age_minutes",
                "repeat_cooldown_minutes", "weekly_target",
            }
        fields = expected_fields | extra_fields
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
        if rule_id == "sunlight_evening" and send_window != {"start": "20:00", "end": "21:00"}:
            raise ValueError("sunlight_evening check window is fixed at 20:00–21:00")
        quiet_hours = _validate_window(rule["quiet_hours"], f"{rule_id}.quiet_hours", overnight=True)
        _ensure_send_window_respects_quiet_hours(send_window, quiet_hours, rule_id)

        max_per_day = rule["max_per_day"]
        duplicate_window_minutes = rule["duplicate_window_minutes"]
        stale_after_hours = rule["stale_after_hours"]
        if isinstance(max_per_day, bool) or not isinstance(max_per_day, int) or not 1 <= max_per_day <= 7:
            raise ValueError(f"{rule_id}.max_per_day must be between 1 and 7")
        required_daily_cap = 7 if rule_id == "wellness_low_state" else 1
        if max_per_day != required_daily_cap:
            raise ValueError(f"{rule_id}.max_per_day is fixed at {required_daily_cap}")
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
        if rule["reschedule"] != "skip_if_late":
            raise ValueError(f"{rule_id}.reschedule must skip missed windows; history is never replayed")

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
            if isinstance(lead_days, bool) or not isinstance(lead_days, int) or lead_days != 3:
                raise ValueError("predicted_period_lead.lead_days is fixed at 3 days")
            normalized_rule["lead_days"] = lead_days
        elif rule_id == "period_late_inquiry":
            start_day = rule["start_day"]
            cooldown_days = rule["cooldown_days"]
            observation_window_days = rule["observation_window_days"]
            probability = rule["probability"]
            if isinstance(start_day, bool) or not isinstance(start_day, int) or not 4 <= start_day <= 30:
                raise ValueError("period_late_inquiry.start_day must be between 4 and 30")
            if isinstance(cooldown_days, bool) or not isinstance(cooldown_days, int) or not 1 <= cooldown_days <= 30:
                raise ValueError("period_late_inquiry.cooldown_days must be between 1 and 30")
            if (isinstance(observation_window_days, bool)
                    or not isinstance(observation_window_days, int)
                    or not start_day <= observation_window_days <= 60):
                raise ValueError("period_late_inquiry.observation_window_days must be at least start_day and at most 60")
            if (isinstance(probability, bool) or not isinstance(probability, (int, float))
                    or not 0 <= probability <= 1):
                raise ValueError("period_late_inquiry.probability must be between 0 and 1")
            normalized_rule.update(
                start_day=start_day,
                probability=float(probability),
                cooldown_days=cooldown_days,
                observation_window_days=observation_window_days,
            )
        elif rule_id == "wellness_low_state":
            mode = rule["mode"]
            if not isinstance(mode, str) or mode not in {"unconfirmed", "numeric", "slow_down_category"}:
                raise ValueError("wellness_low_state.mode is unsupported")
            if not isinstance(rule["timezone_confirmed"], bool):
                raise ValueError("wellness_low_state.timezone_confirmed must be boolean")
            weekly_target = rule["weekly_target"]
            if isinstance(weekly_target, bool) or not isinstance(weekly_target, int) or not 1 <= weekly_target <= 7:
                raise ValueError("wellness_low_state.weekly_target must be between 1 and 7")
            nullable_ranges = {
                "low_score_threshold": (0, 999),
                "recovery_score_threshold": (0, 999),
                "confirmation_minutes": (1, 1440),
                "minimum_independent_samples": (2, 100),
                "recovery_debounce_minutes": (1, 1440),
                "maximum_sample_age_minutes": (1, 1440),
                "repeat_cooldown_minutes": (1, 10080),
            }
            for name, (minimum, maximum) in nullable_ranges.items():
                value = rule[name]
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int)
                    or not minimum <= value <= maximum
                ):
                    raise ValueError(f"wellness_low_state.{name} is outside its supported range")
            if mode == "numeric":
                low, recovery = rule["low_score_threshold"], rule["recovery_score_threshold"]
                if low is not None and recovery is not None and recovery <= low:
                    raise ValueError("wellness recovery threshold must exceed the low threshold")
            for name in nullable_ranges:
                normalized_rule[name] = rule[name]
            required_common = (
                "confirmation_minutes", "minimum_independent_samples",
                "recovery_debounce_minutes", "maximum_sample_age_minutes",
                "repeat_cooldown_minutes",
            )
            configured = (
                mode in {"numeric", "slow_down_category"}
                and rule["timezone_confirmed"] is True
                and all(rule[name] is not None for name in required_common)
                and (mode != "numeric" or (
                    rule["low_score_threshold"] is not None
                    and rule["recovery_score_threshold"] is not None
                ))
            )
            if rule["enabled"] and not configured:
                raise ValueError(
                    "wellness_low_state cannot be enabled until its mode, timezone, and thresholds are confirmed"
                )
            normalized_rule.update(
                mode=mode,
                timezone_confirmed=rule["timezone_confirmed"],
                weekly_target=weekly_target,
            )
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
            fact_lead_days = fact.get("lead_days", rule["lead_days"])
            if isinstance(fact_lead_days, bool) or not isinstance(fact_lead_days, int) or not 1 <= fact_lead_days <= 14:
                row.update(state="invalid_source", reason="预测阶段天数无效")
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
            stage = fact.get("stage")
            if rule_id == "confirmed_period_start" and stage is not None:
                stage_date = _parse_date(fact.get("target_date"))
                source_start_date = occurred_at.astimezone(local_zone).date()
                if (isinstance(stage, bool) or not isinstance(stage, int) or not 1 <= stage <= 3
                        or stage_date != source_start_date + timedelta(days=stage - 1)
                        or stage_date != now.astimezone(local_zone).date()):
                    row.update(state="invalid_source", reason="经期阶段日期与明确开始事件不一致")
                    plans.append(row)
                    continue
                freshness_at = observed_at
            else:
                freshness_at = occurred_at
        elif rule_id in {"wellness_low_state", "sunlight_evening", "period_late_inquiry"}:
            source_date = _parse_date(fact.get("data_date"))
            candidate_at = _parse_aware_datetime(fact.get("candidate_at"))
            candidate_end = _parse_aware_datetime(fact.get("candidate_window_end"))
            expected_kind = {
                "wellness_low_state": "sustained_low_state",
                "sunlight_evening": "sunlight_opportunity",
                "period_late_inquiry": "period_end_inquiry",
            }[rule_id]
            if fact.get("kind") != expected_kind or source_date is None:
                row.update(state="invalid_source", reason="来源记录格式无效")
                plans.append(row)
                continue
            if candidate_at is None or candidate_end is None or candidate_at > now or candidate_end <= candidate_at:
                row.update(state="invalid_source", reason="随机候选时段无效或尚未到达")
                plans.append(row)
                continue
            if candidate_at.astimezone(local_zone).date() != source_date:
                row.update(state="invalid_source", reason="候选时段与数据日期不一致")
                plans.append(row)
                continue
            if rule_id == "period_late_inquiry":
                occurred_at = _parse_aware_datetime(fact.get("start_occurred_at"))
                target_date = _parse_date(fact.get("target_date"))
                period_day = fact.get("period_day")
                ended = fact.get("ended")
                actual_start_date = occurred_at.astimezone(local_zone).date() if occurred_at else None
                if (fact.get("confirmed_start") is not True or occurred_at is None
                        or occurred_at > now or target_date != source_date
                        or target_date != now.astimezone(local_zone).date()
                        or isinstance(period_day, bool) or not isinstance(period_day, int)
                        or period_day < rule["start_day"]
                        or period_day > rule["observation_window_days"]
                        or actual_start_date is None
                        or (target_date - actual_start_date).days + 1 != period_day):
                    row.update(state="invalid_source", reason="需要观察窗内的明确经期开始事件")
                    plans.append(row)
                    continue
                if ended is True:
                    row.update(state="ended", reason="OPPO 已明确记录经期结束；后段询问立即停止")
                    plans.append(row)
                    continue
                if fact.get("cancelled") is True:
                    row.update(state="ended", reason="OPPO 已明确记录经期结束；未安排后段询问")
                    plans.append(row)
                    continue
            freshness_at = observed_at
            expected_at = candidate_at
            end_at = candidate_end
            basis = {
                "wellness_low_state": "连续低状态来源样本",
                "sunlight_evening": "当天明确日照记录",
                "period_late_inquiry": "本人在 OPPO 明确记录的经期开始日之后的后段询问机会",
            }[rule_id]
            target_date = source_date
            is_actual = True
            if now > candidate_end:
                row.update(state="stale_source", reason="已错过随机发送时段")
                plans.append(row)
                continue
            age = now.astimezone(local_zone) - freshness_at.astimezone(local_zone)
            if age > timedelta(hours=rule["stale_after_hours"]):
                row.update(state="stale_source", reason="来源记录已过期")
                plans.append(row)
                continue
            row.update(
                state="preview_only",
                reason=(
                    "满足后段随机询问条件；最终仍由冷却、每日去重和隐私预检把关"
                    if rule_id == "period_late_inquiry"
                    else "满足候选条件；最终仍由隐私预检和去重状态把关"
                ),
                expected_at=expected_at.isoformat(),
                expected_window_end=end_at.isoformat(),
                basis=basis,
                source_date=source_date.isoformat(),
                target_date=target_date.isoformat(),
                is_actual_event=is_actual,
            )
            plans.append(row)
            continue
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
            measured_at = _parse_aware_datetime(fact.get("measured_at"))
            if measured_at is None:
                row.update(state="invalid_source", reason="体重记录缺少实际测量日期和时刻")
                plans.append(row)
                continue
            if measured_at.astimezone(local_zone).date() != source_date:
                row.update(state="invalid_source", reason="体重测量时刻与记录日期不一致")
                plans.append(row)
                continue
            freshness_at = measured_at
            if freshness_at > now:
                row.update(state="invalid_source", reason="体重测量时刻晚于当前时间")
                plans.append(row)
                continue
        age = now.astimezone(local_zone) - freshness_at.astimezone(local_zone)
        if age > timedelta(hours=rule["stale_after_hours"]):
            row.update(state="stale_source", reason="来源记录已过期")
            plans.append(row)
            continue

        if rule_id == "predicted_period_lead":
            target_date = source_date - timedelta(days=fact_lead_days)
            basis = "预测经期日期"
            is_actual = False
        elif rule_id in {"confirmed_period_start", "confirmed_period_end"}:
            target_date = _parse_date(fact.get("target_date")) or occurred_at.astimezone(ZoneInfo(rule["timezone"])).date()
            basis = f"明确开始事件第{fact['stage']}日" if rule_id == "confirmed_period_start" and fact.get("stage") else "明确确认的经期事件"
            is_actual = True
        else:
            target_date = source_date
            basis = "减肥模式开启后的新体重测量日期"
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
