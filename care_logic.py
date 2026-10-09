"""Conservative scheduling rules for daily health care messages."""
import datetime as dt
import hashlib
import math
import random
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


def due_kind(state, snapshot, now, activated_at, last_user_activity=None):
    if (not isinstance(now, dt.datetime) or now.tzinfo is None
            or now.utcoffset() is None):
        return None
    local_now = now.astimezone(SHANGHAI)
    if state.get('date') != local_now.date().isoformat():
        return None

    record = sleep_record(snapshot, now)
    due = record['due_at'] if record else None
    if (record and sleep_candidate_is_stable(state, record['record_id'])
            and due >= activated_at and now >= due and now <= due + LATE_GRACE
            and not state.get('sleep_attempted')
            and state.get('sleep_attempted_record_id') != record['record_id']
            and local_now.hour < 23):
        return 'sleep'

    if state.get('activity_attempted') or not state.get('activity_due') or not 14 <= local_now.hour < 21:
        return None
    target = _timestamp(state.get('activity_due'))
    if target is None or now < target or (due and now < due + dt.timedelta(hours=2)):
        return None
    if last_user_activity and now - last_user_activity < dt.timedelta(minutes=30):
        return None
    return 'activity'
