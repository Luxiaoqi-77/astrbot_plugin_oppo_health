"""Minimize local OPPO captures into scheduler facts and private prompts.

This adapter intentionally accepts only the structured collector result. Raw
screenshots and OCR observations are not handled here. The returned event facts
contain dates and timestamps only; weight values never leave the capture object.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import hashlib
import re
from typing import Any, Callable
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
            "predicted_period_lead", "confirmed_period_start", "confirmed_period_end",
            "period_late_inquiry",
        )
    ):
        fields.append("cycle_calendar")
    if isinstance(rules.get("weight_date_linked"), dict) and rules["weight_date_linked"].get("enabled") is True:
        fields.append("weight_history")
    if isinstance(rules.get("wellness_low_state"), dict) and rules["wellness_low_state"].get("enabled") is True:
        fields.append("wellness_home")
    if isinstance(rules.get("sunlight_evening"), dict) and rules["sunlight_evening"].get("enabled") is True:
        fields.append("sun_exposure")
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


def _period_inquiry_event_id(start_event_id: str, target_date: date) -> str:
    cycle_key = hashlib.sha256(start_event_id.encode("utf-8")).hexdigest()
    return hashlib.sha256(
        f"period-late-inquiry-v1\0{cycle_key}\0{target_date.isoformat()}".encode("utf-8")
    ).hexdigest()


def _period_inquiry_draw(start_event_id: str, target_date: date) -> float:
    """Return a stable pseudo-random draw so refreshes cannot reroll a day."""
    cycle_key = hashlib.sha256(start_event_id.encode("utf-8")).hexdigest()
    digest = hashlib.sha256(
        f"period-late-draw-v1\0{cycle_key}\0{target_date.isoformat()}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


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
    *,
    weight_mode_enabled_at: str | None = None,
    random_draw: Callable[[str, date], float] | None = None,
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
            "predicted_period_lead", "confirmed_period_start", "confirmed_period_end",
            "period_late_inquiry",
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
                start_date = date.fromisoformat(future_starts[0])
                max_lead = predicted_rule.get("lead_days", 3)
                stage_facts = []
                for lead_days in range(max_lead, 0, -1):
                    target_date = start_date - timedelta(days=lead_days)
                    if target_date < local_today:
                        continue
                    event_id = f"cycle-month:{period}:predicted:D-{lead_days}"
                    fact = {
                        "event_id": event_id,
                        "kind": "prediction",
                        "predicted_start_date": start_date.isoformat(),
                        "lead_days": lead_days,
                        "observed_at": cycle["observed_at"],
                    }
                    stage_facts.append(fact)
                    contexts[event_key("predicted_period_lead", event_id)] = (
                        f"OPPO 健康应用显示的预测日期还有 {lead_days} 天。"
                        "这是预测，不代表经期已经开始。请用温和、不诊断的语气简短关心。"
                    )
                if stage_facts:
                    facts["predicted_period"] = stage_facts

        # Only a future adapter with explicit event records may activate these
        # rules. The current parser intentionally does not synthesize them from
        # individual colored calendar days.
        explicit_seen = {"confirmed_period_start": False, "confirmed_period_end": False}
        period_rule_reasons = {}
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
                    occurred_dt = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                    explicit_seen[source] = True
                    zone = ZoneInfo(rule["timezone"])
                    local_today = now.astimezone(zone).date()
                    happened_date = occurred_dt.astimezone(zone).date()
                    window_end = time.fromisoformat(rule["send_window"]["end"])
                    within_current_send_day = now.astimezone(zone).timetz().replace(tzinfo=None) < window_end
                    if source == "confirmed_period_start":
                        day_number = (local_today - happened_date).days + 1
                        if not (1 <= day_number <= 3 and within_current_send_day):
                            period_rule_reasons[source] = "只有明确开始事件后的第 1–3 天会安排当日关怀"
                            continue
                        target_date = local_today
                        stage_id = f"{event_id.strip()}:D{day_number}"
                        fact = {
                            "event_id": stage_id,
                            "confirmed": True,
                            "occurred_at": occurred_at,
                            "observed_at": cycle["observed_at"],
                            "target_date": target_date.isoformat(),
                            "stage": day_number,
                        }
                        collected.append(fact)
                        contexts[event_key(source, stage_id)] = (
                            f"OPPO 健康应用明确记录了经期开始；今天是第 {day_number} 天。"
                            "请用温和、不诊断的语气简短关心。"
                        )
                    elif happened_date == local_today and within_current_send_day:
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
                    else:
                        period_rule_reasons[source] = "只对当天明确记录的结束事件安排一次关怀"
            if collected:
                facts[source] = collected

        cycle_date = period or (max(predicted + actual) if predicted or actual else None)
        predicted_rule_rows = [
            rule for rule_id, rule in rules.items()
            if rule_id in {
                "predicted_period_lead", "confirmed_period_start", "confirmed_period_end",
                "period_late_inquiry",
            }
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
                "status": "ok" if has_fact or explicit_seen[source_id] else ("unsupported" if enabled else "not_configured"),
                "data_date": None,
                "observed_at": cycle["observed_at"],
                "is_stale": None,
                "reason": (
                    None if has_fact
                    else period_rule_reasons.get(source_id)
                    or (None if explicit_seen[source_id] else "当前来源只有逐日标记，未提供明确的开始/结束确认事件")
                ),
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

    late_rule = rules.get("period_late_inquiry")
    late_enabled = isinstance(late_rule, dict) and late_rule.get("enabled") is True
    late_status: dict[str, Any] = {
        "source": "period_late_inquiry",
        "status": "not_configured",
        "evaluation_state": "disabled",
        "data_date": None,
        "observed_at": cycle["observed_at"] if cycle else None,
        "is_stale": None,
        "reason": None,
    }
    if late_enabled:
        if cycle is None:
            late_status.update(
                status="unavailable", evaluation_state="unavailable",
                reason="本次经期来源读取不可用；等待明确开始与结束事件",
            )
        else:
            zone = ZoneInfo(late_rule["timezone"])
            local_now = now.astimezone(zone)
            today = local_now.date()
            metrics = cycle["metrics"]

            def valid_events(field: str) -> list[dict[str, Any]] | None:
                raw_events = metrics.get(field)
                result = []
                if not isinstance(raw_events, list):
                    return None
                seen = set()
                for raw in raw_events:
                    if not isinstance(raw, dict):
                        return None
                    if raw.get("confirmed") is not True:
                        continue
                    occurred_at = _valid_timestamp(raw.get("occurred_at"))
                    event_id = raw.get("event_id")
                    if (occurred_at is None or not isinstance(event_id, str)
                            or not event_id.strip() or len(event_id) > 128):
                        return None
                    occurred_dt = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                    if occurred_dt > now:
                        return None
                    if event_id.strip() in seen:
                        continue
                    seen.add(event_id.strip())
                    result.append({
                        "event_id": event_id.strip(),
                        "occurred_at": occurred_at,
                        "occurred_dt": occurred_dt,
                    })
                return result

            starts = valid_events("confirmed_period_start_events")
            ends = valid_events("confirmed_period_end_events")
            eligible_starts = [
                event for event in (starts or [])
                if event["occurred_dt"].astimezone(zone).date() <= today
            ]
            late_status.update(status="ok", data_date=None, observed_at=cycle["observed_at"])
            if starts is None or ends is None:
                late_status.update(
                    status="unsupported", evaluation_state="unsupported",
                    reason="来源没有同时提供明确经期开始和结束事件通道，或事件内容无效；后段询问保持暂停",
                )
            elif not eligible_starts:
                late_status.update(
                    evaluation_state="waiting_for_start",
                    reason="等待本人在 OPPO 明确记录经期开始；逐日标记不能代替确认",
                )
            else:
                start = max(eligible_starts, key=lambda event: event["occurred_dt"])
                start_date = start["occurred_dt"].astimezone(zone).date()
                period_day = (today - start_date).days + 1
                late_status["data_date"] = start_date.isoformat()
                ended = any(
                    event["occurred_dt"] >= start["occurred_dt"] for event in ends
                )
                event_id = _period_inquiry_event_id(start["event_id"], today)
                if ended:
                    # The stable current-day ID cancels a pending inquiry immediately.
                    facts["period_late_inquiry"] = [{
                        "event_id": event_id,
                        "cancelled": True,
                        "confirmed_start": True,
                        "start_occurred_at": start["occurred_at"],
                        "observed_at": cycle["observed_at"],
                        "data_date": today.isoformat(),
                        "target_date": today.isoformat(),
                        "period_day": period_day,
                        "kind": "period_end_inquiry",
                        "candidate_at": datetime.combine(
                            today, time.fromisoformat(late_rule["send_window"]["start"]), tzinfo=zone
                        ).isoformat(),
                        "candidate_window_end": datetime.combine(
                            today, time.fromisoformat(late_rule["send_window"]["end"]), tzinfo=zone
                        ).isoformat(),
                    }]
                    late_status.update(
                        evaluation_state="ended",
                        reason="OPPO 已明确记录经期结束；后段询问立即停止",
                    )
                elif period_day > late_rule["observation_window_days"]:
                    late_status.update(
                        evaluation_state="paused_window",
                        reason=(
                            f"已超过配置的 D{late_rule['observation_window_days']} 观察窗；"
                            "暂停询问，等待 OPPO 明确结束记录"
                        ),
                    )
                elif period_day < late_rule["start_day"]:
                    late_status.update(
                        evaluation_state="waiting_for_late_phase",
                        reason=f"当前为经期第 {period_day} 天；后段询问从 D{late_rule['start_day']} 开始",
                    )
                else:
                    send_start = datetime.combine(
                        today, time.fromisoformat(late_rule["send_window"]["start"]), tzinfo=zone
                    )
                    send_end = datetime.combine(
                        today, time.fromisoformat(late_rule["send_window"]["end"]), tzinfo=zone
                    )
                    late_status.update(evaluation_state="waiting_for_window")
                    if local_now < send_start:
                        late_status["reason"] = "等待今日可发送时段"
                    elif local_now >= send_end:
                        late_status.update(
                            evaluation_state="missed_window",
                            reason="已错过今日询问时段；不补发，明日重新按概率判断",
                        )
                    else:
                        draw = (
                            random_draw(start["event_id"], today)
                            if random_draw is not None
                            else _period_inquiry_draw(start["event_id"], today)
                        )
                        if (isinstance(draw, bool) or not isinstance(draw, (int, float))
                                or not 0 <= draw < 1):
                            raise ValueError("period inquiry random draw must be in [0, 1)")
                        if draw < late_rule["probability"]:
                            fact = {
                                "event_id": event_id,
                                "kind": "period_end_inquiry",
                                "confirmed_start": True,
                                "start_occurred_at": start["occurred_at"],
                                "observed_at": cycle["observed_at"],
                                "data_date": today.isoformat(),
                                "target_date": today.isoformat(),
                                "period_day": period_day,
                                "candidate_at": send_start.isoformat(),
                                "candidate_window_end": send_end.isoformat(),
                            }
                            facts["period_late_inquiry"] = [fact]
                            contexts[event_key("period_late_inquiry", event_id)] = (
                                "OPPO 健康应用有明确的经期开始记录，目前没有明确结束记录。"
                                "只温和询问本人是否已结束；"
                                "不要把未记录说成已结束，不要代替本人修改或记录 OPPO 经期数据，也不要诊断。"
                            )
                            late_status.update(
                                evaluation_state="candidate",
                                reason=f"今日 D{period_day} 随机机会已选中；仍受 {late_rule['cooldown_days']} 天冷却和每日一次限制",
                            )
                        else:
                            late_status.update(
                                evaluation_state="not_selected",
                                reason=f"今日 D{period_day} 未命中 {late_rule['probability']:.0%} 随机机会；不补发，明日重新判断",
                            )
    else:
        late_status["reason"] = None
    if late_enabled and cycle is not None and _is_stale(
        cycle["observed_at"], None, [late_rule], now
    ):
        facts.pop("period_late_inquiry", None)
        late_status.update(
            status="stale", evaluation_state="paused_stale", is_stale=True,
            reason="经期来源快照已超过数据过期阈值；暂停后段询问",
        )
    elif late_enabled:
        late_status["is_stale"] = False
    statuses["period_late_inquiry"] = late_status

    weight, weight_status = _page(capture, "weight_history")
    weight_rule = rules.get("weight_date_linked")
    weight_enabled = isinstance(weight_rule, dict) and weight_rule.get("enabled") is True
    if weight is not None:
        metrics = weight["metrics"]
        records = metrics.get("weight_history_records", [])
        weight_facts = []
        opted_in_at = _valid_timestamp(weight_mode_enabled_at)
        weight_zone = ZoneInfo(weight_rule["timezone"]) if isinstance(weight_rule, dict) else ZoneInfo("UTC")
        if weight_enabled and opted_in_at is not None and isinstance(records, list):
            for row in records:
                if not isinstance(row, dict):
                    continue
                record_date = _valid_date(row.get("record_date"))
                observed_at = _valid_timestamp(row.get("observed_at"))
                if record_date is None or observed_at is None:
                    continue
                measured_text = row.get("measured_at_local")
                if not isinstance(measured_text, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", measured_text):
                    continue
                measured_at = datetime.combine(
                    date.fromisoformat(record_date), time.fromisoformat(measured_text), tzinfo=weight_zone
                )
                observed_dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                enabled_dt = datetime.fromisoformat(opted_in_at.replace("Z", "+00:00"))
                if measured_at > observed_dt or measured_at <= enabled_dt:
                    continue
                measured_at_iso = measured_at.isoformat()
                event_id = f"weight:{record_date}:{measured_text}"
                fact = {
                    "event_id": event_id,
                    "record_date": record_date,
                    "observed_at": observed_at,
                    "measured_at": measured_at_iso,
                }
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
        latest_measured = max(
            (fact["measured_at"] for fact in weight_facts), default=None
        )
        statuses["weight_entry"] = {
            "source": "weight_entry",
            "status": "ok" if weight_facts else ("unavailable" if weight_enabled else "not_configured"),
            "data_date": latest_date,
            "observed_at": weight["observed_at"],
            "measured_at": latest_measured,
            "completeness": (
                "测量日期、实际测量时刻和页面读取时间齐全；来源未提供独立记录创建时刻"
                if latest_measured else None
            ),
            "is_stale": _is_stale(weight["observed_at"], latest_date, [weight_rule] if weight_enabled else [], now),
            "reason": None if weight_facts else (
                "本人尚未明确开启减肥模式"
                if weight_enabled and weight_mode_enabled_at is None
                else "没有测量时刻晚于开启模式时间的有效新记录；来源未提供独立记录创建时刻"
                if weight_enabled and isinstance(records, list) and records
                else "没有可用于规则的日期和测量时间"
            ),
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

    order = (
        "predicted_period", "confirmed_period_start", "confirmed_period_end",
        "period_late_inquiry", "weight_entry",
    )
    source_statuses = [statuses[key] for key in order]
    return facts, contexts, source_statuses
