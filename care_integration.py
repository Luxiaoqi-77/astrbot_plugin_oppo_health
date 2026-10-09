"""Minimize local OPPO captures into scheduler facts and private prompts.

This adapter intentionally accepts only the structured collector result. Raw
screenshots and OCR observations are not handled here. The returned event facts
contain dates and timestamps only; weight values never leave the capture object.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import hashlib
from typing import Any
from zoneinfo import ZoneInfo


def event_key(rule_id: str, stable_event_id: str) -> str:
    return hashlib.sha256(f"{rule_id}\0{stable_event_id}".encode("utf-8")).hexdigest()


def enabled_capture_fields(config: object) -> list[str]:
    rules = config.get("rules", {}) if isinstance(config, dict) else {}
    if not isinstance(rules, dict):
        return []
    fields = []
    if any(
        isinstance(rules.get(rule_id), dict) and rules[rule_id].get("enabled") is True
        for rule_id in (
            "predicted_period_lead", "confirmed_period_start", "confirmed_period_end"
        )
    ):
        fields.append("cycle_calendar")
    if isinstance(rules.get("weight_date_linked"), dict) and rules["weight_date_linked"].get("enabled") is True:
        fields.append("weight_history")
    return fields


def _valid_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _prediction_starts(values: list[str]) -> list[str]:
    """Reduce colored day markers to the first day of each contiguous block."""
    parsed = sorted({date.fromisoformat(value) for value in values})
    starts = []
    previous = None
    for current in parsed:
        if previous is None or current != previous + timedelta(days=1):
            starts.append(current.isoformat())
        previous = current
    return starts


def _valid_timestamp(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.isoformat()


def _page(capture: object, name: str) -> tuple[dict[str, Any] | None, str]:
    pages = capture.get("pages", {}) if isinstance(capture, dict) else {}
    item = pages.get(name) if isinstance(pages, dict) else None
    if not isinstance(item, dict):
        return None, "unavailable"
    if item.get("status") != "ok":
        return None, "unavailable"
    data = item.get("data")
    if not isinstance(data, dict) or data.get("source") != "oppo_health_ui_ocr":
        return None, "unavailable"
    observed_at = _valid_timestamp(data.get("observed_at"))
    metrics = data.get("metrics")
    if observed_at is None or not isinstance(metrics, dict):
        return None, "unavailable"
    return {"data": data, "metrics": metrics, "observed_at": observed_at}, "ok"


def _is_stale(
    observed_at: str | None,
    data_date: str | None,
    rules: list[dict[str, Any]],
    now: datetime,
) -> bool | None:
    if not rules:
        return None
    thresholds = [
        item.get("stale_after_hours")
        for item in rules
        if isinstance(item.get("stale_after_hours"), int)
        and not isinstance(item.get("stale_after_hours"), bool)
    ]
    if not thresholds:
        return None
    rule_zone = ZoneInfo(rules[0]["timezone"])
    freshness = None
    if data_date:
        try:
            freshness = datetime.combine(date.fromisoformat(data_date), time.min, tzinfo=rule_zone)
        except ValueError:
            freshness = None
    if freshness is None and observed_at:
        freshness = datetime.fromisoformat(observed_at).astimezone(rule_zone)
    if freshness is None:
        return None
    return now.astimezone(rule_zone) - freshness > timedelta(hours=min(thresholds))


def facts_from_capture(
    capture: object,
    config: object,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    """Return minimal rule facts, private prompt contexts, and safe status rows.

    The function intentionally does not infer a period end from a missing next
    marker. Current cycle-calendar OCR exposes per-day marks, not explicit
    confirmed start/end events, so those rules remain source-unavailable unless
    a future adapter emits explicit event records.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    rules = config.get("rules", {}) if isinstance(config, dict) else {}
    if not isinstance(rules, dict):
        rules = {}
    facts: dict[str, Any] = {}
    contexts: dict[str, str] = {}
    statuses: dict[str, dict[str, Any]] = {}

    cycle, cycle_status = _page(capture, "cycle_calendar")
    cycle_rules = [
        rules.get(rule_id) for rule_id in (
            "predicted_period_lead", "confirmed_period_start", "confirmed_period_end"
        )
        if isinstance(rules.get(rule_id), dict) and rules[rule_id].get("enabled") is True
    ]
    if cycle is not None:
        metrics = cycle["metrics"]
        period = cycle["data"].get("period")
        period = period if isinstance(period, str) and len(period) <= 16 else None
        predicted = sorted({
            parsed for value in metrics.get("predicted_period_dates", [])
            if (parsed := _valid_date(value)) is not None
        }) if isinstance(metrics.get("predicted_period_dates", []), list) else []
        actual = sorted({
            parsed for value in metrics.get("period_dates", [])
            if (parsed := _valid_date(value)) is not None
        }) if isinstance(metrics.get("period_dates", []), list) else []

        # The collector exposes a single visible calendar month. A month-scoped
        # opaque ID stays stable when the predicted day moves inside that view.
        predicted_rule = rules.get("predicted_period_lead")
        if (
            isinstance(predicted_rule, dict)
            and predicted_rule.get("enabled") is True
            and predicted
            and period
            and metrics.get("legend_verified") is True
            and metrics.get("calendar_coverage")
        ):
            zone = ZoneInfo(predicted_rule["timezone"])
            local_today = now.astimezone(zone).date()
            future_starts = [
                item for item in _prediction_starts(predicted)
                if date.fromisoformat(item) >= local_today
            ]
            if future_starts:
                event_id = f"cycle-month:{period}:predicted"
                fact = {
                    "event_id": event_id,
                    "kind": "prediction",
                    "predicted_start_date": future_starts[0],
                    "observed_at": cycle["observed_at"],
                }
                facts["predicted_period"] = [fact]
                contexts[event_key("predicted_period_lead", event_id)] = (
                    f"OPPO 健康应用显示了一条经期预测日期：{future_starts[0]}。"
                    "这是预测，不代表经期已经开始。请用温和、不诊断的语气简短关心。"
                )

        # Only a future adapter with explicit event records may activate these
        # rules. The current parser intentionally does not synthesize them from
        # individual colored calendar days.
        for rule_id, source, source_field, topic in (
            ("confirmed_period_start", "confirmed_period_start", "confirmed_period_start_events", "经期已明确确认开始"),
            ("confirmed_period_end", "confirmed_period_end", "confirmed_period_end_events", "经期已明确确认结束"),
        ):
            rule = rules.get(rule_id)
            explicit = metrics.get(source_field)
            collected = []
            if isinstance(rule, dict) and rule.get("enabled") is True and isinstance(explicit, list):
                for row in explicit:
                    if not isinstance(row, dict) or row.get("confirmed") is not True:
                        continue
                    occurred_at = _valid_timestamp(row.get("occurred_at"))
                    event_id = row.get("event_id")
                    if occurred_at is None or not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 128:
                        continue
                    fact = {
                        "event_id": event_id.strip(),
                        "confirmed": True,
                        "occurred_at": occurred_at,
                        "observed_at": cycle["observed_at"],
                    }
                    collected.append(fact)
                    contexts[event_key(source, event_id.strip())] = (
                        f"{topic}，时间为 {occurred_at}。"
                        "请用温和、不诊断的语气简短关心。"
                    )
            if collected:
                facts[source] = collected

        cycle_date = period or (max(predicted + actual) if predicted or actual else None)
        predicted_rule_rows = [
            rule for rule_id, rule in rules.items()
            if rule_id in {"predicted_period_lead", "confirmed_period_start", "confirmed_period_end"}
            and isinstance(rule, dict) and rule.get("enabled") is True
        ]
        statuses["cycle_calendar"] = {
            "source": "cycle_calendar",
            "status": "ok",
            "data_date": cycle_date,
            "observed_at": cycle["observed_at"],
            "is_stale": _is_stale(cycle["observed_at"], cycle_date, predicted_rule_rows, now),
            "reason": None,
        }
        for source_id, rule_id, has_fact in (
            ("predicted_period", "predicted_period_lead", bool(facts.get("predicted_period"))),
            ("confirmed_period_start", "confirmed_period_start", bool(facts.get("confirmed_period_start"))),
            ("confirmed_period_end", "confirmed_period_end", bool(facts.get("confirmed_period_end"))),
        ):
            if source_id == "predicted_period":
                continue
            enabled = isinstance(rules.get(rule_id), dict) and rules[rule_id].get("enabled") is True
            statuses[source_id] = {
                "source": source_id,
                "status": "ok" if has_fact else ("unsupported" if enabled else "not_configured"),
                "data_date": None,
                "observed_at": cycle["observed_at"],
                "is_stale": None,
                "reason": None if has_fact else "当前来源只有逐日标记，未提供明确的开始/结束确认事件",
            }
        statuses["predicted_period"] = {
            "source": "predicted_period",
            "status": "ok" if facts.get("predicted_period") else "unavailable",
            "data_date": facts.get("predicted_period", [{}])[0].get("predicted_start_date") if facts.get("predicted_period") else None,
            "observed_at": cycle["observed_at"],
            "is_stale": _is_stale(cycle["observed_at"], None, [rules["predicted_period_lead"]] if isinstance(rules.get("predicted_period_lead"), dict) and rules["predicted_period_lead"].get("enabled") is True else [], now),
            "reason": None if facts.get("predicted_period") else "当前可见日历没有可用的未来预测日期",
        }
    else:
        for source_id, rule_id in (
            ("predicted_period", "predicted_period_lead"),
            ("confirmed_period_start", "confirmed_period_start"),
            ("confirmed_period_end", "confirmed_period_end"),
        ):
            enabled = isinstance(rules.get(rule_id), dict) and rules[rule_id].get("enabled") is True
            statuses[source_id] = {
                "source": source_id,
                "status": "unavailable" if enabled else "not_configured",
                "data_date": None,
                "observed_at": None,
                "is_stale": None,
                "reason": "本次本机来源读取不可用" if enabled else None,
            }

    weight, weight_status = _page(capture, "weight_history")
    weight_rule = rules.get("weight_date_linked")
    weight_enabled = isinstance(weight_rule, dict) and weight_rule.get("enabled") is True
    if weight is not None:
        metrics = weight["metrics"]
        records = metrics.get("weight_history_records", [])
        weight_facts = []
        if weight_enabled and isinstance(records, list):
            for row in records:
                if not isinstance(row, dict):
                    continue
                record_date = _valid_date(row.get("record_date"))
                observed_at = _valid_timestamp(row.get("observed_at"))
                if record_date is None or observed_at is None:
                    continue
                measured_at = row.get("measured_at_local")
                if not isinstance(measured_at, str) or len(measured_at) > 8:
                    measured_at = "unknown-time"
                event_id = f"weight:{record_date}:{measured_at}"
                fact = {
                    "event_id": event_id,
                    "record_date": record_date,
                    "observed_at": observed_at,
                }
                recorded_at = _valid_timestamp(row.get("recorded_at"))
                if recorded_at is not None:
                    fact["recorded_at"] = recorded_at
                weight_facts.append(fact)
                contexts[event_key("weight_date_linked", event_id)] = (
                    f"本人在 {record_date} 有一条体重记录。不要提及或评价体重数值，"
                    "只用温和、不评判的语气简短关心。"
                )
        if weight_facts:
            facts["weight_entry"] = weight_facts
        valid_record_dates = [
            _valid_date(row.get("record_date")) for row in records if isinstance(row, dict)
        ] if isinstance(records, list) else []
        valid_record_dates = [item for item in valid_record_dates if item]
        latest_date = max(valid_record_dates) if valid_record_dates else None
        statuses["weight_entry"] = {
            "source": "weight_entry",
            "status": "ok" if weight_facts else ("unavailable" if weight_enabled else "not_configured"),
            "data_date": latest_date,
            "observed_at": weight["observed_at"],
            "is_stale": _is_stale(weight["observed_at"], latest_date, [weight_rule] if weight_enabled else [], now),
            "reason": None if weight_facts else "没有可用于规则的记录日期",
        }
    else:
        statuses["weight_entry"] = {
            "source": "weight_entry",
            "status": "unavailable" if weight_enabled else "not_configured",
            "data_date": None,
            "observed_at": None,
            "is_stale": None,
            "reason": "本次本机来源读取不可用" if weight_enabled else None,
        }

    order = ("predicted_period", "confirmed_period_start", "confirmed_period_end", "weight_entry")
    source_statuses = [statuses[key] for key in order]
    return facts, contexts, source_statuses
