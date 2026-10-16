"""Event-scoped bridge from real OneBot action responses to local ledgers.

AstrBot's respond stage can send one event in multiple chunks. This helper
aggregates outcomes on the same AstrMessageEvent so after-send hooks can settle
the matching durable attempt. Sleep keeps its at-most-once chunk policy; period
inquiries require every chunk to receive a success response. It is deliberately
transport agnostic except for the OneBot result classifier in sleep_dispatch.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

try:
    from .sleep_dispatch import classify_onebot_result, finish_attempt
except ImportError:  # Allow the offline candidate tests to import this file directly.
    from sleep_dispatch import classify_onebot_result, finish_attempt


ATTEMPT_EXTRA = "_oppo_sleep_attempt_id"
OUTCOME_EXTRA = "_oppo_sleep_transport_outcome"
CALLS_EXTRA = "_oppo_sleep_transport_call_count"
CALLBACK_EXTRA = "_oppo_sleep_transport_callback"
PERIOD_INQUIRY_EXTRA = "_oppo_period_inquiry_event_id"
PERIOD_OUTCOME_EXTRA = "_oppo_period_transport_outcome"
PERIOD_CALLS_EXTRA = "_oppo_period_transport_call_count"
RANDOM_ACTIVITY_ATTEMPT_EXTRA = "_oppo_random_activity_attempt_id"
RANDOM_ACTIVITY_OUTCOME_EXTRA = "_oppo_random_activity_transport_outcome"
RANDOM_ACTIVITY_CALLS_EXTRA = "_oppo_random_activity_transport_call_count"


def attach_attempt(event: Any, attempt_id: str) -> None:
    """Attach correlation metadata to the synthetic inbound AstrBot event."""
    event.set_extra(ATTEMPT_EXTRA, attempt_id)
    event.set_extra(OUTCOME_EXTRA, None)
    event.set_extra(CALLS_EXTRA, 0)

    def callback(response=None, error_kind=None):
        return record_onebot_action(event, response=response, error_kind=error_kind)

    # Candidate adapter glue reads this callback from the same AstrMessageEvent
    # when an actual OneBot action resolves. No plugin import is needed in core.
    event.set_extra(CALLBACK_EXTRA, callback)


def attach_period_inquiry(event: Any, event_id: str) -> None:
    """Attach transport receipt tracking for a synthetic period inquiry."""
    if not isinstance(event_id, str) or len(event_id) != 64 or any(char not in "0123456789abcdef" for char in event_id):
        raise ValueError("invalid period inquiry event ID")
    event.set_extra(PERIOD_INQUIRY_EXTRA, event_id)
    event.set_extra(PERIOD_OUTCOME_EXTRA, None)
    event.set_extra(PERIOD_CALLS_EXTRA, 0)

    def callback(response=None, error_kind=None):
        return record_period_inquiry_action(event, response=response, error_kind=error_kind)

    event.set_extra(CALLBACK_EXTRA, callback)


def attach_random_activity_attempt(event: Any, attempt_id: str) -> None:
    """Attach metadata-only receipt tracking to one random-care event."""
    if not isinstance(attempt_id, str) or len(attempt_id) != 32 or any(c not in "0123456789abcdef" for c in attempt_id):
        raise ValueError("invalid random activity attempt ID")
    event.set_extra(RANDOM_ACTIVITY_ATTEMPT_EXTRA, attempt_id)
    event.set_extra(RANDOM_ACTIVITY_OUTCOME_EXTRA, None)
    event.set_extra(RANDOM_ACTIVITY_CALLS_EXTRA, 0)

    def callback(response=None, error_kind=None):
        return record_onebot_action(event, response=response, error_kind=error_kind)

    event.set_extra(CALLBACK_EXTRA, callback)


def record_period_inquiry_action(
    event: Any,
    response: dict[str, Any] | None = None,
    error_kind: str | None = None,
) -> str | None:
    """Accept an inquiry only when every transport chunk has a success receipt."""
    if not event.get_extra(PERIOD_INQUIRY_EXTRA):
        return None
    incoming = classify_onebot_result(response=response, error_kind=error_kind)
    current = event.get_extra(PERIOD_OUTCOME_EXTRA)
    if incoming != "sent" or (current is not None and current != "sent"):
        outcome = "unknown"
    else:
        outcome = "sent"
    event.set_extra(PERIOD_OUTCOME_EXTRA, outcome)
    event.set_extra(PERIOD_CALLS_EXTRA, int(event.get_extra(PERIOD_CALLS_EXTRA, 0)) + 1)
    return outcome


def _merge(current: str | None, incoming: str) -> str:
    # Any accepted chunk means a message was accepted and must consume quota.
    if current == "sent" or incoming == "sent":
        return "sent"
    # One ambiguous chunk means some part may have been delivered; a later
    # explicit rejection cannot make the whole event safe to retry.
    if current == "unknown" or incoming == "unknown":
        return "unknown"
    return "failed_not_sent"


def record_onebot_action(
    event: Any,
    response: dict[str, Any] | None = None,
    error_kind: str | None = None,
) -> str | None:
    """Aggregate one actual transport result for a correlated care event."""
    if not (event.get_extra(ATTEMPT_EXTRA) or event.get_extra(PERIOD_INQUIRY_EXTRA)
            or event.get_extra(RANDOM_ACTIVITY_ATTEMPT_EXTRA)):
        return None
    is_random_activity = bool(event.get_extra(RANDOM_ACTIVITY_ATTEMPT_EXTRA))
    outcome = (
        classify_random_activity_result(response=response, error_kind=error_kind)
        if is_random_activity
        else classify_onebot_result(response=response, error_kind=error_kind)
    )
    if is_random_activity:
        current = event.get_extra(RANDOM_ACTIVITY_OUTCOME_EXTRA)
        merged = _merge(current, outcome)
        event.set_extra(RANDOM_ACTIVITY_OUTCOME_EXTRA, merged)
        event.set_extra(
            RANDOM_ACTIVITY_CALLS_EXTRA,
            int(event.get_extra(RANDOM_ACTIVITY_CALLS_EXTRA, 0)) + 1,
        )
        return merged
    current = event.get_extra(OUTCOME_EXTRA)
    merged = _merge(current, outcome)
    event.set_extra(OUTCOME_EXTRA, merged)
    event.set_extra(CALLS_EXTRA, int(event.get_extra(CALLS_EXTRA, 0)) + 1)
    return merged


def classify_random_activity_result(
    response: dict[str, Any] | None = None, error_kind: str | None = None
) -> str:
    """Classify an activity send using explicit OneBot success receipts.

    Args:
        response: Result returned by the OneBot send action.
        error_kind: Exception type raised by the transport.

    Returns:
        ``sent`` only for a positive action receipt, ``failed_not_sent`` for an
        explicit rejection, and ``unknown`` when delivery is ambiguous.
    """
    if error_kind == "ActionFailed":
        return "failed_not_sent"
    if error_kind is not None or not isinstance(response, dict):
        return "unknown"
    status = response.get("status")
    retcode = response.get("retcode")
    has_retcode = "retcode" in response
    if status not in (None, "ok", "failed"):
        return "unknown"
    if has_retcode and type(retcode) is not int:
        return "unknown"
    if status == "failed":
        return "unknown" if has_retcode and retcode == 0 else "failed_not_sent"
    if status == "ok":
        return "unknown" if has_retcode and retcode != 0 else "sent"
    if has_retcode:
        return "sent" if retcode == 0 else "failed_not_sent"
    return "unknown"


def report_onebot_action(
    event: Any,
    response: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> str | None:
    """Adapter-side hook to call immediately after each actual OneBot action.

    This function is the tested contract for the small core adapter patch. In
    AstrBot core, the equivalent can be inlined to avoid importing a plugin.
    """
    callback = event.get_extra(CALLBACK_EXTRA)
    if not callable(callback):
        return None
    error_kind = None
    if error is not None:
        error_kind = "ActionFailed" if type(error).__name__ == "ActionFailed" else type(error).__name__
    return callback(response=response, error_kind=error_kind)


def settle_after_send(ledger: dict[str, Any], event: Any, at: str | datetime) -> tuple[bool, str]:
    """Persist the aggregate result; missing transport evidence is unknown."""
    attempt_id = event.get_extra(ATTEMPT_EXTRA)
    if not attempt_id:
        return False, "not_a_sleep_dispatch"
    outcome = event.get_extra(OUTCOME_EXTRA) or "unknown"
    return finish_attempt(ledger, attempt_id, outcome, at)


def settle_random_activity_after_send(
    ledger: dict[str, Any], event: Any, at: str | datetime, finish_fn,
) -> tuple[bool, str]:
    """Settle one activity attempt; absent receipts remain unknown."""
    attempt_id = event.get_extra(RANDOM_ACTIVITY_ATTEMPT_EXTRA)
    if not attempt_id:
        return False, "not_a_random_activity_dispatch"
    outcome = event.get_extra(RANDOM_ACTIVITY_OUTCOME_EXTRA) or "unknown"
    return finish_fn(
        ledger,
        attempt_id,
        outcome,
        at,
        transport_calls=int(event.get_extra(RANDOM_ACTIVITY_CALLS_EXTRA, 0)),
    )

