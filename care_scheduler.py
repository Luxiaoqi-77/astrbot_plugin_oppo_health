"""Side-effect-free rule reconciliation and an adapter-injected dispatch core.

This module deliberately has no AstrBot, health-source, model, QQ, or network
imports. Production must provide an explicit source adapter and sender before
any scheduling is enabled; the plugin candidate currently wires neither.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta
import hashlib
import inspect
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from .care_rules import RULE_IDS, build_preview_plan, validate_config
from .care_store import empty_scheduler_state


_SOURCE_BY_RULE = {
    "predicted_period_lead": "predicted_period",
    "confirmed_period_start": "confirmed_period_start",
    "confirmed_period_end": "confirmed_period_end",
    "weight_date_linked": "weight_entry",
}
_LIVE_JOB_STATES = {"pending", "paused_stale", "paused_source_missing", "paused_invalid", "paused_privacy"}
_COUNTED_ATTEMPTS = {"dispatching", "handed_off", "failed", "failed_uncertain"}
_TERMINAL_JOB_STATES = {
    "handed_off", "failed", "failed_uncertain", "skipped", "cancelled"
}


class DispatchAdapter(Protocol):
    def dispatch(self, plan: dict[str, Any]) -> None:
        """Hand an approved, metadata-only plan to the integration layer."""


class DispatchRejected(RuntimeError):
    """A safe, non-sensitive reason the plugin ingress refused a plan."""

    def __init__(self, code: str):
        self.code = code if code in {
            "rule_disabled", "event_not_due", "event_consumed", "private_session_unavailable",
            "privacy_preflight_unavailable", "runner_unsupported", "provider_unapproved",
            "source_context_unavailable", "handler_unavailable",
        } else "dispatch_rejected"
        super().__init__(self.code)


_REJECT_REASONS = {
    "rule_disabled": "规则已关闭",
    "event_not_due": "提醒时间尚未到",
    "event_consumed": "这条来源事件已处理，未重复提交",
    "private_session_unavailable": "本人私聊会话当前不可用",
    "privacy_preflight_unavailable": "核心隐私预检不可用，已暂停提交",
    "runner_unsupported": "当前回复运行方式不受支持，已暂停提交",
    "provider_unapproved": "当前模型服务未获本机健康数据授权，已暂停提交",
    "source_context_unavailable": "来源记录已不可用，未提交关怀",
    "handler_unavailable": "AstrBot 私聊回复入口不可用",
    "dispatch_rejected": "关怀入口拒绝了本次提交",
}


def _parse_aware(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None and result.utcoffset() is not None else None


def _event_key(rule_id: str, stable_event_id: str) -> str:
    return hashlib.sha256(f"{rule_id}\0{stable_event_id}".encode("utf-8")).hexdigest()


def _fact_list(value: object) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _append_attempt(state: dict[str, Any], attempt: dict[str, Any]) -> None:
    attempts = state["attempts"]
    attempts.append(attempt)
    del attempts[:-5000]


def _pause_rule_jobs(state: dict[str, Any], rule_id: str, status: str, reason: str) -> None:
    for job in state["jobs"].values():
        if job.get("rule_id") == rule_id and job.get("state") in _LIVE_JOB_STATES:
            job.update(state=status, reason=reason)


def _plan_for_fact(config: dict[str, Any], rule_id: str, fact: dict[str, Any], now: datetime) -> dict[str, Any]:
    preview = build_preview_plan(config, {_SOURCE_BY_RULE[rule_id]: fact}, now)
    return next(row for row in preview["plans"] if row["rule_id"] == rule_id)


def reconcile_jobs(
    config: object,
    event_facts: object,
    current_state: object,
    now: datetime,
) -> dict[str, Any]:
    """Reconcile explicit source facts into durable pending jobs.

    Event IDs must be stable across forecast updates. A source that changes a
    forecast should reuse its event ID so a pending job is rescheduled in place.
    Cancellation is only recognized from an explicit ``cancelled: true`` fact.
    Missing end markers and missing sources never imply an event or completion.
    """
    validated = validate_config(config)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if current_state is None or current_state == {}:
        state = empty_scheduler_state()
    elif not isinstance(current_state, dict):
        state = empty_scheduler_state()
    else:
        state = deepcopy(current_state)
    if (state.get("schema_version") != 1 or not isinstance(state.get("jobs"), dict)
            or not isinstance(state.get("attempts"), list)):
        raise ValueError("scheduler state is invalid")
    facts = event_facts if isinstance(event_facts, dict) else {}

    for rule_id in RULE_IDS:
        rule = validated["rules"][rule_id]
        if not rule["enabled"]:
            _pause_rule_jobs(state, rule_id, "cancelled", "规则已关闭")
            continue

        source = _SOURCE_BY_RULE[rule_id]
        received = _fact_list(facts.get(source))
        if not received:
            _pause_rule_jobs(state, rule_id, "paused_source_missing", "来源记录暂不可用")
            continue

        seen_event_keys: set[str] = set()
        for fact in received:
            stable_id = fact.get("event_id")
            if not isinstance(stable_id, str) or not stable_id.strip() or len(stable_id) > 256:
                continue
            event_key = _event_key(rule_id, stable_id.strip())
            seen_event_keys.add(event_key)
            job = state["jobs"].get(event_key)
            if fact.get("cancelled") is True:
                if job is not None and job.get("state") in _LIVE_JOB_STATES:
                    job.update(state="cancelled", reason="来源明确取消了这条记录", updated_at=now.isoformat())
                continue

            plan = _plan_for_fact(validated, rule_id, fact, now)
            if plan["state"] == "stale_source":
                if job is not None and job.get("state") in _LIVE_JOB_STATES:
                    job.update(state="paused_stale", reason=plan["reason"], updated_at=now.isoformat())
                continue
            if plan["state"] != "preview_only":
                if job is not None and job.get("state") in _LIVE_JOB_STATES:
                    job.update(state="paused_invalid", reason=plan["reason"], updated_at=now.isoformat())
                continue
            if job is not None and job.get("state") in _TERMINAL_JOB_STATES:
                continue

            expected_at = _parse_aware(plan.get("expected_at"))
            window_end = _parse_aware(plan.get("expected_window_end"))
            if expected_at is None or window_end is None:
                continue
            next_state = "pending"
            reason = "等待可发送时段"
            now_local = now.astimezone(ZoneInfo(rule["timezone"]))
            if now_local >= window_end:
                if rule["reschedule"] == "skip_if_late":
                    next_state = "skipped"
                    reason = "已错过发送时段，按设置跳过"
                else:
                    next_date = max(expected_at.date() + timedelta(days=1), now_local.date())
                    end_time = time.fromisoformat(rule["send_window"]["end"])
                    if next_date == now_local.date() and now_local.timetz().replace(tzinfo=None) >= end_time:
                        next_date += timedelta(days=1)
                    expected_at = datetime.combine(
                        next_date, time.fromisoformat(rule["send_window"]["start"]),
                        tzinfo=ZoneInfo(rule["timezone"]),
                    )
                    window_end = datetime.combine(
                        next_date, time.fromisoformat(rule["send_window"]["end"]),
                        tzinfo=ZoneInfo(rule["timezone"]),
                    )
                    reason = "已错过原时段，顺延到下一个可发送时段"

            values = {
                "event_key": event_key,
                "rule_id": rule_id,
                "target_date": plan.get("target_date"),
                "basis": plan.get("basis"),
                "expected_at": expected_at.isoformat(),
                "window_end": window_end.isoformat(),
                "timezone": rule["timezone"],
                "source_observed_at": fact.get("observed_at"),
                "is_actual_event": plan.get("is_actual_event") is True,
                "state": next_state,
                "reason": reason,
                "updated_at": now.isoformat(),
            }
            if job is None:
                values["created_at"] = now.isoformat()
                state["jobs"][event_key] = values
            else:
                job.update(values)
            if next_state == "skipped":
                _append_attempt(state, {
                    "job_id": event_key,
                    "rule_id": rule_id,
                    "at": now.isoformat(),
                    "outcome": "skipped",
                    "reason": reason,
                })

        # A current source snapshot may omit an old event because it was
        # removed. That is not a cancellation signal, so keep pending jobs and
        # pause them until a source explicitly confirms their state.
        if not seen_event_keys:
            _pause_rule_jobs(state, rule_id, "paused_source_missing", "来源未提供可识别的事件")

    return state


def recover_interrupted_attempts(current_state: object, now: datetime) -> dict[str, Any]:
    """Mark a persisted in-flight handoff uncertain so restart cannot resend it."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if current_state is None or current_state == {}:
        state = empty_scheduler_state()
    else:
        state = deepcopy(current_state) if isinstance(current_state, dict) else empty_scheduler_state()
    jobs = state.get("jobs", {})
    attempts = state.get("attempts", [])
    for job_id, job in jobs.items():
        if job.get("state") != "dispatching":
            continue
        job.update(
            state="failed_uncertain",
            reason="进程在提交后中断，无法确认是否交给回复流程；为避免重复，本次不重试",
            updated_at=now.isoformat(),
        )
        for attempt in reversed(attempts):
            if attempt.get("job_id") == job_id and attempt.get("outcome") == "dispatching":
                attempt.update(
                    outcome="failed_uncertain",
                    reason="进程中断，无法确认交接结果",
                )
                break
    return state


def _local_day(value: datetime, timezone_name: str) -> date:
    return value.astimezone(ZoneInfo(timezone_name)).date()


def _is_duplicate(
    state: dict[str, Any], job: dict[str, Any], rule: dict[str, Any], now: datetime
) -> str | None:
    zone = rule["timezone"]
    local_day = _local_day(now, zone)
    for attempt in reversed(state["attempts"]):
        if attempt.get("rule_id") != job["rule_id"] or attempt.get("outcome") not in _COUNTED_ATTEMPTS:
            continue
        attempted_at = _parse_aware(attempt.get("at"))
        if attempted_at is None:
            continue
        if rule["deduplication"] == "per_local_date" and _local_day(attempted_at, zone) == local_day:
            return "本地日期内已有一次提醒尝试"
        elapsed = (now - attempted_at).total_seconds()
        if 0 <= elapsed < rule["duplicate_window_minutes"] * 60:
            return "处于重复提醒保护时段"
    return None


def _counted_today(state: dict[str, Any], rule_id: str, now: datetime, timezone_name: str) -> int:
    local_day = _local_day(now, timezone_name)
    count = 0
    for attempt in state["attempts"]:
        if attempt.get("rule_id") != rule_id or attempt.get("outcome") not in _COUNTED_ATTEMPTS:
            continue
        attempted_at = _parse_aware(attempt.get("at"))
        if attempted_at is not None and _local_day(attempted_at, timezone_name) == local_day:
            count += 1
    return count


def dispatch_due(
    config: object,
    event_facts: object,
    current_state: object,
    now: datetime,
    sender: DispatchAdapter,
    *,
    persist: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Run due jobs via an explicitly injected adapter and persist reservation first.

    Any normal return from the adapter is recorded as ``handed_off``; this says
    only that the adapter accepted the plan, not that a platform delivered it.
    Adapter exceptions are reduced to their type name so exception text cannot
    leak health data, credentials, or request contents into persisted state.
    """
    validated = validate_config(config)
    state = recover_interrupted_attempts(current_state, now)
    state = reconcile_jobs(validated, event_facts, state, now)
    results: list[dict[str, str]] = []
    if persist is not None:
        persist(deepcopy(state))

    due_jobs = sorted(
        (
            (job_id, job) for job_id, job in state["jobs"].items()
            if job.get("state") == "pending"
            and (_parse_aware(job.get("expected_at")) is not None)
            and _parse_aware(job.get("expected_at")) <= now
        ),
        key=lambda pair: pair[1]["expected_at"],
    )
    for job_id, job in due_jobs:
        rule = validated["rules"][job["rule_id"]]
        if _counted_today(state, job["rule_id"], now, rule["timezone"]) >= rule["max_per_day"]:
            reason = "已达到这条规则的每日提醒上限"
            job.update(state="skipped", reason=reason, updated_at=now.isoformat())
            _append_attempt(state, {
                "job_id": job_id, "rule_id": job["rule_id"], "at": now.isoformat(),
                "outcome": "skipped", "reason": reason,
            })
            results.append({"job_id": job_id, "outcome": "skipped", "reason": reason})
            if persist is not None:
                persist(deepcopy(state))
            continue

        duplicate_reason = _is_duplicate(state, job, rule, now)
        if duplicate_reason:
            job.update(state="skipped", reason=duplicate_reason, updated_at=now.isoformat())
            _append_attempt(state, {
                "job_id": job_id, "rule_id": job["rule_id"], "at": now.isoformat(),
                "outcome": "skipped", "reason": duplicate_reason,
            })
            results.append({"job_id": job_id, "outcome": "skipped", "reason": duplicate_reason})
            if persist is not None:
                persist(deepcopy(state))
            continue

        # Reserve and persist before crossing the adapter boundary. If the
        # process then exits, recovery marks it uncertain and suppresses retry.
        job.update(state="dispatching", reason="已开始交给适配器", updated_at=now.isoformat())
        attempt = {
            "job_id": job_id, "rule_id": job["rule_id"], "at": now.isoformat(),
            "outcome": "dispatching", "reason": "适配器调用中",
        }
        _append_attempt(state, attempt)
        if persist is not None:
            persist(deepcopy(state))
        adapter_plan = {
            "rule_id": job["rule_id"],
            "tone": rule["tone"],
            "target_date": job.get("target_date"),
            "basis": job.get("basis"),
            "is_actual_event": job.get("is_actual_event") is True,
            "expected_at": job.get("expected_at"),
            "timezone": job.get("timezone"),
        }
        try:
            sender.dispatch(adapter_plan)
        except Exception as exc:  # Keep external exception text out of private state.
            outcome = "failed"
            reason = f"适配器未完成交接（{type(exc).__name__}）"
        else:
            outcome = "handed_off"
            reason = "已交给适配器；平台送达状态未确认"
        job.update(state=outcome, reason=reason, updated_at=now.isoformat())
        attempt.update(outcome=outcome, reason=reason)
        results.append({"job_id": job_id, "outcome": outcome, "reason": reason})
        if persist is not None:
            persist(deepcopy(state))

    return state, results


async def dispatch_due_async(
    config: object,
    event_facts: object,
    current_state: object,
    now: datetime,
    sender: DispatchAdapter,
    *,
    persist: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Async counterpart used by the plugin-owned safe care ingress.

    A normal return means only that AstrBot's existing reply handler accepted
    the synthetic private event. It does not assert platform delivery.
    """
    validated = validate_config(config)
    state = recover_interrupted_attempts(current_state, now)
    state = reconcile_jobs(validated, event_facts, state, now)
    results: list[dict[str, str]] = []
    if persist is not None:
        persist(deepcopy(state))

    due_jobs = sorted(
        (
            (job_id, job) for job_id, job in state["jobs"].items()
            if job.get("state") == "pending"
            and (_parse_aware(job.get("expected_at")) is not None)
            and _parse_aware(job.get("expected_at")) <= now
        ),
        key=lambda pair: pair[1]["expected_at"],
    )
    for job_id, job in due_jobs:
        rule = validated["rules"][job["rule_id"]]
        if _counted_today(state, job["rule_id"], now, rule["timezone"]) >= rule["max_per_day"]:
            reason = "已达到这条规则的每日提醒上限"
            job.update(state="skipped", reason=reason, updated_at=now.isoformat())
            attempt = {
                "job_id": job_id, "rule_id": job["rule_id"], "at": now.isoformat(),
                "outcome": "skipped", "reason": reason,
            }
            _append_attempt(state, attempt)
            results.append({"job_id": job_id, "outcome": "skipped", "reason": reason})
            if persist is not None:
                persist(deepcopy(state))
            continue

        duplicate_reason = _is_duplicate(state, job, rule, now)
        if duplicate_reason:
            job.update(state="skipped", reason=duplicate_reason, updated_at=now.isoformat())
            attempt = {
                "job_id": job_id, "rule_id": job["rule_id"], "at": now.isoformat(),
                "outcome": "skipped", "reason": duplicate_reason,
            }
            _append_attempt(state, attempt)
            results.append({"job_id": job_id, "outcome": "skipped", "reason": duplicate_reason})
            if persist is not None:
                persist(deepcopy(state))
            continue

        job.update(state="dispatching", reason="已开始交给本人私聊回复流程", updated_at=now.isoformat())
        attempt = {
            "job_id": job_id, "rule_id": job["rule_id"], "at": now.isoformat(),
            "outcome": "dispatching", "reason": "本人私聊回复流程调用中",
        }
        _append_attempt(state, attempt)
        if persist is not None:
            persist(deepcopy(state))
        plan = {
            "rule_id": job["rule_id"],
            "event_id": job_id,
            "target_at": job.get("expected_at"),
            "source_ref": "oppo_health_local_page",
            "tone": rule["tone"],
            "target_date": job.get("target_date"),
            "basis": job.get("basis"),
            "is_actual_event": job.get("is_actual_event") is True,
            "timezone": job.get("timezone"),
        }
        try:
            outcome = sender.dispatch(plan)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except DispatchRejected as exc:
            outcome_name = "failed"
            reason = _REJECT_REASONS.get(exc.code, _REJECT_REASONS["dispatch_rejected"])
        except Exception as exc:  # Do not persist exception text or request contents.
            outcome_name = "failed"
            reason = f"本人私聊回复流程未完成（{type(exc).__name__}）"
        else:
            if isinstance(outcome, dict) and outcome.get("outcome") == "failed":
                outcome_name = "failed"
                code = outcome.get("reason_code")
                reason = _REJECT_REASONS.get(code, "本人私聊回复流程拒绝了本次提交")
            else:
                outcome_name = "handed_off"
                reason = "已交给 AstrBot 私聊回复流程；平台送达状态未确认"
        job.update(state=outcome_name, reason=reason, updated_at=now.isoformat())
        attempt.update(outcome=outcome_name, reason=reason)
        results.append({"job_id": job_id, "outcome": outcome_name, "reason": reason})
        if persist is not None:
            persist(deepcopy(state))

    return state, results


def pause_for_privacy(state: object, now: datetime, reason: str) -> dict[str, Any]:
    """Pause pending jobs while route or runner privacy preflight is unavailable."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if state is None or state == {}:
        result = empty_scheduler_state()
    else:
        result = deepcopy(state) if isinstance(state, dict) else empty_scheduler_state()
    safe_reason = reason if reason in set(_REJECT_REASONS.values()) else "核心隐私预检不可用，已暂停提交"
    for job in result.get("jobs", {}).values():
        if job.get("state") in _LIVE_JOB_STATES:
            job.update(state="paused_privacy", reason=safe_reason, updated_at=now.isoformat())
    return result
