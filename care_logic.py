"""Pure scheduling rules: one wake-up care and one optional daytime care."""
import datetime as dt
import random


def wake_due(snapshot, now, minutes=60):
    if not snapshot or snapshot.get('date') != now.date().isoformat():
        return None
    value = snapshot.get('metrics', {}).get('sleep', {}).get('wake_time')
    try:
        wake = dt.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if wake.tzinfo is None or wake.astimezone(now.tzinfo).date() != now.date() or wake > now:
        return None
    return wake + dt.timedelta(minutes=minutes)


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
    if state.get('date') != now.date().isoformat():
        return None
    due = wake_due(snapshot, now)
    if (due and due >= activated_at and now >= due
            and not state.get('sleep_attempted') and now.hour < 23):
        return 'sleep'
    if state.get('activity_attempted') or not state.get('activity_due') or not 14 <= now.hour < 21:
        return None
    target = dt.datetime.fromisoformat(state['activity_due'])
    if now < target or (due and now < due + dt.timedelta(hours=2)):
        return None
    if last_user_activity and now - last_user_activity < dt.timedelta(minutes=30):
        return None
    return 'activity'
