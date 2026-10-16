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

try:
    from .period_flow import (
        calendar_months_from_capture,
        confirmed_end_record,
        current_period_cluster,
        period_end_inquiry_fact,
        period_clusters,
        post_period_care_fact,
        period_cycle_id as period_cycle_id_for_start,
        start_care_fact,
    )
except ImportError:  # Support focused tests importing the plugin directory directly.
    from period_flow import (
        calendar_months_from_capture,
        confirmed_end_record,
        current_period_cluster,
        period_end_inquiry_fact,
        period_clusters,
        post_period_care_fact,
        period_cycle_id as period_cycle_id_for_start,
        start_care_fact,
    )


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
            "predicted_period_lead", "confirmed_period_start", "confirmed_period_end", "post_period_care",
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


def _period_start_date_event_id(source: str, event_date: str) -> str:
    return hashlib.sha256(f"{source}\0period-start\0{event_date}".encode("utf-8")).hexdigest()


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
    requested_fields: list[str] | tuple[str, ...] | None = None,
    random_draw: Callable[[str, date], float] | None = None,
    period_end_confirmation: object = None,
    period_inquiry_sent_dates: set[str] | None = None,
    period_confirmed_cycle_ids: set[str] | None = None,
    period_postcare_cycle_ids: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    """Return minimal rule facts, private prompt contexts, and safe status rows.

    Period dates are inferred only from fully classified, legend-verified
    actual Calendar clusters with visible non-Period boundaries. End records
    additionally require an explicit private user confirmation and a fresh
    post-confirmation Calendar sync.
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
    calendar_months = calendar_months_from_capture(capture)
    confirmed_cycles = period_confirmed_cycle_ids or set()
    postcare_cycles = period_postcare_cycle_ids or set()
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
                        f"OPPO 预测经期还有 {lead_days} 天。请温和提醒，并自然问一句是否已经来。"
                        "不要把预测当作实际开始；是否记录由本人手动完成，机器人只读同步、不修改记录。"
                        "不要在关怀话术里复述这些限制。"
                    )
                if stage_facts:
                    facts["predicted_period"] = stage_facts

        explicit_seen = {
            "confirmed_period_start": False,
            "confirmed_period_end": False,
            "post_period_care": False,
        }
        period_rule_reasons = {}
        for rule_id, source, source_field, topic in (
            ("confirmed_period_start", "confirmed_period_start", "confirmed_period_start_events", "经期已明确确认开始"),
            ("confirmed_period_end", "confirmed_period_end", "confirmed_period_end_events", "经期已明确确认结束"),
        ):
            rule = rules.get(rule_id)
            explicit = metrics.get(source_field)
            if source == "confirmed_period_start" and isinstance(rule, dict):
                derived_start = None
                if rule.get("timezone") == "Asia/Shanghai":
                    derived_start = start_care_fact(
                        calendar_months, now.astimezone(ZoneInfo("Asia/Shanghai")).date()
                    )
                if derived_start is not None:
                    explicit = list(explicit) if isinstance(explicit, list) else []
                    explicit.append({
                        "event_id": derived_start["event_id"],
                        "confirmed": True,
                        "event_date": derived_start["event_date"],
                        "date_precision": "date",
                        "timezone": "Asia/Shanghai",
                    })
                else:
                    navigation = capture.get("period_calendar_navigation") if isinstance(capture, dict) else None
                    if not isinstance(navigation, dict) or navigation.get("status") != "complete":
                        period_rule_reasons[source] = "上月完整 Calendar 不可用；经期开始边界缺少证据"
                    else:
                        period_rule_reasons[source] = "需要图例已验证、覆盖完整且前一日明确非 Period 的实际日期簇"
            collected = []
            if isinstance(rule, dict) and rule.get("enabled") is True and isinstance(explicit, list):
                seen_event_ids: set[str] = set()
                for row in explicit:
                    if not isinstance(row, dict) or row.get("confirmed") is not True:
                        continue
                    event_id = row.get("event_id")
                    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 128:
                        continue
                    if event_id.strip() in seen_event_ids:
                        continue
                    seen_event_ids.add(event_id.strip())
                    zone = ZoneInfo(rule["timezone"])
                    local_today = now.astimezone(zone).date()
                    window_end = time.fromisoformat(rule["send_window"]["end"])
                    within_current_send_day = now.astimezone(zone).timetz().replace(tzinfo=None) < window_end
                    if row.get("date_precision") == "date":
                        event_date = _valid_date(row.get("event_date"))
                        expected_id = (
                            _period_start_date_event_id("oppo_health_ui_ocr", event_date)
                            if event_date is not None else None
                        )
                        if (source != "confirmed_period_start"
                                or rule.get("timezone") != "Asia/Shanghai"
                                or row.get("timezone") != "Asia/Shanghai"
                                or event_date is None or event_id.strip() != expected_id
                                or derived_start is None
                                or event_date != derived_start.get("event_date")):
                            continue
                        explicit_seen[source] = True
                        day_number = (local_today - date.fromisoformat(event_date)).days + 1
                        if not (1 <= day_number <= 3 and within_current_send_day):
                            period_rule_reasons[source] = "只有明确 Day 1 日期之后的第 1–3 个 Asia/Shanghai 本地日会安排当日关怀"
                            continue
                        target_date = local_today
                        stage_id = f"{event_id.strip()}:D{day_number}"
                        collected.append({
                            "event_id": stage_id,
                            "confirmed": True,
                            "event_date": event_date,
                            "date_precision": "date",
                            "timezone": "Asia/Shanghai",
                            "observed_at": cycle["observed_at"],
                            "target_date": target_date.isoformat(),
                            "stage": day_number,
                        })
                        contexts[event_key(source, stage_id)] = (
                            f"OPPO Calendar 的完整实际 Period 连续日期簇从记录日期开始；"
                            f"今天是第 {day_number} 天。请用温和、不诊断的语气简短关心。"
                        )
                        continue

                    occurred_at = _valid_timestamp(row.get("occurred_at"))
                    if occurred_at is None:
                        continue
                    occurred_dt = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                    explicit_seen[source] = True
                    happened_date = occurred_dt.astimezone(zone).date()
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

        if cycle is not None and period_end_confirmation is not None:
            end_record = confirmed_end_record(
                calendar_months,
                period_end_confirmation,
                sync_observed_at=cycle["observed_at"],
            )
            if end_record is not None:
                facts["confirmed_period_end"] = [end_record]
                post_fact = post_period_care_fact(end_record)
                if (post_fact is not None
                        and post_fact.get("cycle_id") not in postcare_cycles):
                    facts["post_period_care"] = [post_fact]
                    contexts[event_key("post_period_care", post_fact["event_id"])] = (
                        "本人已确认这次经期结束，且确认后的只读 Calendar 同步保留了实际日期簇末日。"
                        "请用温和、不诊断的语气简短关心；不要提及或修改 OPPO 记录。"
                    )

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
            ("post_period_care", "post_period_care", bool(facts.get("post_period_care"))),
        ):
            if source_id == "predicted_period":
                continue
            enabled = isinstance(rules.get(rule_id), dict) and rules[rule_id].get("enabled") is True
            statuses[source_id] = {
                "source": source_id,
                "status": "ok" if has_fact or explicit_seen[source_id] else ("unsupported" if enabled else "not_configured"),
                "data_date": (
                    facts.get(source_id, [{}])[0].get("event_date")
                    if facts.get(source_id) else None
                ),
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
                reason="本次经期来源读取不可用；等待完整 Calendar 实际日期簇",
            )
        else:
            zone = ZoneInfo(late_rule["timezone"])
            local_now = now.astimezone(zone)
            today = local_now.date()
            current = current_period_cluster(calendar_months, today)
            late_status.update(status="ok", observed_at=cycle["observed_at"])
            if current is None:
                navigation = capture.get("period_calendar_navigation") if isinstance(capture, dict) else None
                missing_reason = (
                    "上月完整 Calendar 不可用，跨月边界缺少证据"
                    if not isinstance(navigation, dict) or navigation.get("status") != "complete"
                    else "等待图例和边界均可确认的实际 Period 连续日期簇"
                )
                late_status.update(
                    evaluation_state="waiting_for_start",
                    reason=missing_reason,
                )
            else:
                cluster, period_day = current
                start_text = cluster["start_date"]
                late_status["data_date"] = start_text
                confirmation = (
                    period_end_confirmation
                    if isinstance(period_end_confirmation, dict)
                    and period_end_confirmation.get("confirmed") is True
                    and period_end_confirmation.get("period_start_date") == start_text
                    else None
                )
                end_record = next((
                    row for row in facts.get("confirmed_period_end", [])
                    if isinstance(row, dict) and row.get("period_start_date") == start_text
                ), None)
                if period_cycle_id_for_start(start_text) in confirmed_cycles:
                    late_status.update(
                        evaluation_state="confirmation_processed",
                        reason="本人已确认；后续不再询问，也不会重试确认后的同步",
                    )
                elif confirmation is not None:
                    if end_record is not None:
                        late_status.update(
                            evaluation_state="ended",
                            reason="本人在目标私聊明确确认；确认后的 Calendar 同步已更新本地结束记录",
                        )
                    else:
                        late_status.update(
                            evaluation_state="confirmation_sync_pending",
                            reason="已收到明确确认；等待确认后的完整本地 Calendar 同步",
                        )
                elif period_day < 6:
                    late_status.update(
                        evaluation_state="waiting_for_late_phase",
                        reason="后段问询仅在经期第 6–8 天进行",
                    )
                elif period_day > 8:
                    late_status.update(
                        evaluation_state="paused_window",
                        reason="已超出经期第 6–8 天问询窗口；不再补问",
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
                            reason="已错过今日问询时段；不补发",
                        )
                    elif period_inquiry_sent_dates and today.isoformat() in period_inquiry_sent_dates:
                        late_status.update(
                            evaluation_state="already_asked_today",
                            reason="今日问询已发送；每天最多一次",
                        )
                    else:
                        fact = period_end_inquiry_fact(
                            calendar_months,
                            today,
                            confirmed_end_start_dates={start_text} if confirmation else set(),
                            already_asked_dates=period_inquiry_sent_dates,
                        )
                        if fact is not None:
                            fact.update(
                                confirmed_start=True,
                                data_date=today.isoformat(),
                                observed_at=cycle["observed_at"],
                                candidate_at=send_start.isoformat(),
                                candidate_window_end=send_end.isoformat(),
                            )
                            facts["period_late_inquiry"] = [fact]
                            contexts[event_key("period_late_inquiry", fact["event_id"])] = (
                                "请只问本人这次经期是否已经结束；不要推断结束日期，不把预测日期当成结束，"
                                "不要把引用、否定或其他事情结束的话识别为确认。"
                            )
                            late_status.update(
                                evaluation_state="candidate",
                                reason=f"当前为经期第 {period_day} 天；今日问询事实已按日期生成，最多一次",
                            )
                        else:
                            late_status.update(
                                evaluation_state="no_confirmed_cluster",
                                reason="实际 Period 连续日期簇或其边界未完整确认；不问询",
                            )
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
    page_by_source = {
        "predicted_period": "cycle_calendar",
        "confirmed_period_start": "cycle_calendar",
        "confirmed_period_end": "cycle_calendar",
        "period_late_inquiry": "cycle_calendar",
        "weight_entry": "weight_history",
    }
    rule_by_source = {
        "predicted_period": "predicted_period_lead",
        "confirmed_period_start": "confirmed_period_start",
        "confirmed_period_end": "confirmed_period_end",
        "period_late_inquiry": "period_late_inquiry",
        "weight_entry": "weight_date_linked",
    }
    pages = capture.get("pages", {}) if isinstance(capture, dict) else {}
    requested = set(requested_fields or ())
    for row in source_statuses:
        source = row["source"]
        page_name = page_by_source[source]
        rule = rules.get(rule_by_source[source])
        page_item = pages.get(page_name) if isinstance(pages, dict) else None
        page_data = page_item.get("data") if isinstance(page_item, dict) else None
        metrics = page_data.get("metrics") if isinstance(page_data, dict) else None
        if not isinstance(rule, dict) or rule.get("enabled") is not True:
            collection_state = (
                "rule_disabled_shared_capture"
                if requested_fields is not None and page_name in requested
                else "rule_disabled"
            )
        elif requested_fields is not None and page_name not in requested:
            collection_state = "not_collected"
        elif not isinstance(page_item, dict):
            collection_state = "collection_failed"
        elif source in {"confirmed_period_start", "confirmed_period_end", "period_late_inquiry"}:
            explicit_key = {
                "confirmed_period_start": "confirmed_period_start_events",
                "confirmed_period_end": "confirmed_period_end_events",
                "period_late_inquiry": "confirmed_period_start_events",
            }[source]
            explicit = metrics.get(explicit_key) if isinstance(metrics, dict) else None
            visible_days = metrics.get("period_dates") if isinstance(metrics, dict) else None
            if facts.get(source):
                collection_state = "usable"
            elif isinstance(explicit, list) and explicit:
                collection_state = "usable" if facts.get(source) else "filtered"
            elif isinstance(visible_days, list) and visible_days:
                cluster_starts = [
                    item for item in period_clusters(calendar_months)
                    if item.get("start_confirmed") is True
                ]
                collection_state = "filtered" if cluster_starts else "evidence_unavailable"
            elif page_item.get("status") == "ok":
                collection_state = "no_records"
            else:
                collection_state = "collection_failed"
        elif source == "predicted_period":
            predictions = metrics.get("predicted_period_dates") if isinstance(metrics, dict) else None
            if facts.get("predicted_period"):
                collection_state = "usable"
            elif isinstance(predictions, list) and predictions:
                collection_state = "filtered"
            elif page_item.get("status") == "ok":
                collection_state = "no_records"
            else:
                collection_state = "collection_failed"
        elif source == "weight_entry":
            records = metrics.get("weight_history_records") if isinstance(metrics, dict) else None
            if facts.get("weight_entry"):
                collection_state = "usable"
            elif isinstance(records, list) and records:
                collection_state = "filtered"
            elif isinstance(metrics, dict) and page_item.get("status") in {"ok", "no_visible_value"}:
                collection_state = "no_records"
            else:
                collection_state = "collection_failed"
        else:
            collection_state = "collection_failed"
        row["collection_state"] = collection_state
        if collection_state == "evidence_unavailable":
            row["reason"] = "日历覆盖、图例或实际 Period 簇边界未完整确认"
        elif collection_state == "no_records":
            row["reason"] = "已读取来源页面；当前页面没有该规则所需的记录"
        elif collection_state == "filtered" and not row.get("reason"):
            row["reason"] = "来源存在记录，但没有记录通过日期、时间或授权筛选"

    return facts, contexts, source_statuses
