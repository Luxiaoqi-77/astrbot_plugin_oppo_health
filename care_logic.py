"""Conservative scheduling rules for daily health care messages."""
import datetime as dt
import hashlib
import math
import random
import re
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo('Asia/Shanghai')
MAX_SNAPSHOT_AGE = dt.timedelta(minutes=20)
STABILITY_WINDOW = dt.timedelta(minutes=15)
LATE_GRACE = dt.timedelta(minutes=30)


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def sleep_record(snapshot, now):
    """Return a validated, minimal sleep candidate or None."""
    if (not isinstance(snapshot, dict) or not isinstance(now, dt.datetime)
            or now.tzinfo is None or now.utcoffset() is None):
        return None

    local_now = now.astimezone(SHANGHAI)
    record_date = local_now.date().isoformat()
    if snapshot.get('date') != record_date:
        return None

    fetched_at = _timestamp(snapshot.get('fetched_at'))
    if fetched_at is None or fetched_at > now or now - fetched_at > MAX_SNAPSHOT_AGE:
        return None

    metrics = snapshot.get('metrics')
    if not isinstance(metrics, dict):
        return None
    sleep = metrics.get('sleep')
    if not isinstance(sleep, dict):
        return None
    minutes = sleep.get('minutes')
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0:
        return None
    try:
        if not math.isfinite(float(minutes)):
            return None
    except (OverflowError, ValueError):
        return None

    bedtime = _timestamp(sleep.get('bedtime'))
    wake_time = _timestamp(sleep.get('wake_time'))
    modified_at = _timestamp(sleep.get('record_modified_at'))
    if (bedtime is None or wake_time is None or modified_at is None
            or sleep.get('record_date') != record_date):
        return None

    if (bedtime >= wake_time or wake_time > now or modified_at > now
            or modified_at < wake_time or modified_at > fetched_at
            or wake_time.astimezone(SHANGHAI).date().isoformat() != record_date):
        return None

    parts = (record_date, bedtime.isoformat(), wake_time.isoformat(),
             format(float(minutes), '.12g'), modified_at.isoformat())
    record_id = hashlib.sha256('\0'.join(parts).encode()).hexdigest()[:24]
    return {
        'record_id': record_id,
        'date': record_date,
        'fetched_at': fetched_at,
        'wake_at': wake_time,
        'due_at': wake_time + dt.timedelta(minutes=60),
    }


def preflight_sleep_record(previous_snapshot, fresh_snapshot, now):
    """Accept only a fresh fetch of the exact same validated sleep record."""
    previous = sleep_record(previous_snapshot, now)
    fresh = sleep_record(fresh_snapshot, now)
    if (previous is None or fresh is None or previous['record_id'] != fresh['record_id']
            or fresh['fetched_at'] <= previous['fetched_at']):
        return None
    return fresh


def observe_sleep_candidate(state, snapshot, now):
    """Track independent fresh snapshots without storing raw sleep values."""
    record = sleep_record(snapshot, now)
    keys = ('sleep_candidate_id', 'sleep_candidate_since', 'sleep_candidate_last_fetch_at')
    if record is None:
        changed = any(key in state for key in keys)
        for key in keys:
            state.pop(key, None)
        return None, False, changed

    fetched = record['fetched_at']
    if state.get('sleep_candidate_id') != record['record_id']:
        state['sleep_candidate_id'] = record['record_id']
        state['sleep_candidate_since'] = fetched.isoformat()
        state['sleep_candidate_last_fetch_at'] = fetched.isoformat()
        return record, False, True

    first = _timestamp(state.get('sleep_candidate_since'))
    last = _timestamp(state.get('sleep_candidate_last_fetch_at'))
    if first is None or last is None or last < first:
        state['sleep_candidate_since'] = fetched.isoformat()
        state['sleep_candidate_last_fetch_at'] = fetched.isoformat()
        return record, False, True

    changed = False
    if fetched > last:
        last = fetched
        state['sleep_candidate_last_fetch_at'] = fetched.isoformat()
        changed = True
    stable = last - first >= STABILITY_WINDOW and last > first
    return record, stable, changed


def sleep_candidate_is_stable(state, record_id):
    if state.get('sleep_candidate_id') != record_id:
        return False
    first = _timestamp(state.get('sleep_candidate_since'))
    last = _timestamp(state.get('sleep_candidate_last_fetch_at'))
    return bool(first and last and last > first and last - first >= STABILITY_WINDOW)


def preflight_cooldown_active(state, now, minutes=5):
    checked_at = _timestamp(state.get('sleep_preflight_at'))
    return bool(checked_at and now < checked_at + dt.timedelta(minutes=minutes))


def wake_due(snapshot, now, minutes=60):
    record = sleep_record(snapshot, now)
    if record is None:
        return None
    return record['wake_at'] + dt.timedelta(minutes=minutes)


def new_activity_due(now, sleep_due=None, chooser=random.randint):
    start = now.replace(hour=14, minute=0, second=0, microsecond=0)
    end = now.replace(hour=21, minute=0, second=0, microsecond=0)
    start = max(start, now + dt.timedelta(minutes=15))
    if sleep_due:
        start = max(start, sleep_due + dt.timedelta(hours=2))
    if start >= end:
        return None
    return (start + dt.timedelta(seconds=chooser(0, max(0, int((end-start).total_seconds()) - 1)))).isoformat()


def choose_sleep_care(record_id, wake_time, rng=random):
    """Persist one 50% choice and 60–120 minute delay per new sleep record."""
    if not isinstance(record_id, str) or not record_id:
        raise ValueError('record_id is required')
    if not isinstance(wake_time, dt.datetime) or wake_time.tzinfo is None or wake_time.utcoffset() is None:
        raise ValueError('wake_time must be timezone-aware')
    selected = rng.random() < 0.5
    delay = rng.randint(60, 120)
    return {
        'record_id': record_id,
        'selected': selected,
        'delay_minutes': delay,
        'due_at': (wake_time + dt.timedelta(minutes=delay)).isoformat() if selected else None,
    }


def due_kind(state, snapshot, now, activated_at, last_user_activity=None):
    if (not isinstance(now, dt.datetime) or now.tzinfo is None
            or now.utcoffset() is None):
        return None
    local_now = now.astimezone(SHANGHAI)
    if state.get('date') != local_now.date().isoformat():
        return None
    if state.get('goodnight_date') == local_now.date().isoformat():
        return None

    record = sleep_record(snapshot, now)
    due = record['due_at'] if record else None
    if record and state.get('sleep_plan_record_id') == record['record_id']:
        due = _timestamp(state.get('sleep_care_due_at'))
        if state.get('sleep_care_selected') is not True:
            due = None
    if (record and due is not None and sleep_candidate_is_stable(state, record['record_id'])
            and due >= activated_at and now >= due and now <= due + LATE_GRACE
            and not state.get('sleep_attempted')
            and state.get('sleep_attempted_record_id') != record['record_id']
            and not (local_now.hour >= 22 or local_now.hour < 8)):
        return 'sleep'

    if state.get('activity_attempted') or not state.get('activity_due') or not 14 <= local_now.hour < 21:
        return None
    target = _timestamp(state.get('activity_due'))
    if (target is None or target < activated_at or target.astimezone(SHANGHAI).date() != local_now.date()
            or now < target or (due and now < due + dt.timedelta(hours=2))):
        return None
    if last_user_activity and now - last_user_activity < dt.timedelta(minutes=30):
        return None
    return 'activity'


def legacy_dispatch_skip_reason(kind, target_at, now, activated_at):
    """Fail closed if a legacy sleep/activity candidate crosses its send window."""
    if (kind not in {'sleep', 'activity'} or not isinstance(now, dt.datetime)
            or now.tzinfo is None or now.utcoffset() is None
            or not isinstance(activated_at, dt.datetime)
            or activated_at.tzinfo is None or activated_at.utcoffset() is None):
        return 'invalid_window'
    target = _timestamp(target_at)
    if target is None:
        return 'invalid_window'
    if target < activated_at:
        return 'before_activation'
    if target > now:
        return 'not_due'
    local_now = now.astimezone(SHANGHAI)
    if target.astimezone(SHANGHAI).date() != local_now.date():
        return 'expired'
    if kind == 'sleep':
        if local_now.hour < 8 or local_now.hour >= 22 or now > target + LATE_GRACE:
            return 'quiet_or_expired'
    elif not 14 <= local_now.hour < 21:
        return 'quiet_or_expired'
    return None


def explicit_goodnight_message(text, *, own_private_plain_message):
    """Recognize a direct bedtime sign-off without retaining the message text."""
    if (not own_private_plain_message or not isinstance(text, str)
            or "\n" in text or "\r" in text
            or any(mark in text for mark in ('"', "'", "“", "”", "‘", "’", "「", "」", "『", "』", "<", ">", "《", "》"))):
        return False
    normalized = re.sub(r"[，。！？!?.,；;：:…]+$", "", text.strip()).casefold()
    if normalized in {"晚安", "晚安啦", "晚安哦", "good night", "goodnight"}:
        return True
    return bool(re.search(r"(?:我|先)(?:准备|要|去|先)?(?:睡了|睡觉了|休息了)$", normalized))
