"""Normalize only the requested day; never expose account or device identifiers."""
import concurrent.futures
import datetime as dt
import json

from cloud_client import TZ, day_range, fetch
from storage import private_root, write_private_json

CACHE_FILE = private_root() / 'snapshot.json'
METRICS = ('steps', 'heart_rate', 'blood_oxygen', 'sleep')


def number(value):
    return value if type(value) in (int, float) else None


def measured_at(value):
    if type(value) not in (int, float):
        return None
    try:
        return dt.datetime.fromtimestamp(value / 1000, TZ).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def summarize(metric, rows, date):
    bounds = day_range(date)
    start, end = bounds['startTimestamp'], bounds['endTimestamp']
    if metric == 'steps':
        unique = {}
        for row in rows:
            stamp = number(row.get('startTimestamp'))
            value = number(row.get('steps'))
            if row.get('display') != 1 or stamp is None or not start <= stamp <= end or value is None or value < 0 or number(row.get('endTimestamp')) is None:
                continue
            key = (row.get('clientDataId'), row.get('deviceUniqueId'), stamp, row.get('endTimestamp'))
            if key not in unique or (number(row.get('modifiedTime')) or 0) > (number(unique[key].get('modifiedTime')) or 0):
                unique[key] = row
        if not unique:
            return None
        return {'total': sum(r['steps'] for r in unique.values()),
                'measured_at': measured_at(max(r['endTimestamp'] for r in unique.values()))}
    if metric in ('heart_rate', 'blood_oxygen'):
        value_key = 'heartRateValue' if metric == 'heart_rate' else 'bloodOxygenSaturationValue'
        valid = [r for r in rows if number(r.get('dataCreatedTimestamp')) is not None
                 and start <= r['dataCreatedTimestamp'] <= end
                 and number(r.get(value_key)) is not None
                 and r[value_key] > 0 and r.get('display', 1) == 1]
        if metric == 'heart_rate':
            resting = [r for r in valid if r.get('heartRateType') == 1]
            valid = [r for r in valid if r.get('heartRateType') == 0]
        if not valid:
            return None
        latest = max(valid, key=lambda r: r['dataCreatedTimestamp'])
        result = {'latest': latest[value_key], 'measured_at': measured_at(latest['dataCreatedTimestamp'])}
        if metric == 'heart_rate' and resting:
            result['resting'] = max(resting, key=lambda r: r['dataCreatedTimestamp'])[value_key]
        return result
    if metric == 'sleep':
        dates = {date.isoformat(), date.strftime('%Y%m%d')}
        valid = [r for r in rows if str(r.get('date')) in dates
                 and number(r.get('totalSleepTime')) is not None and r['totalSleepTime'] > 0]
        if not valid:
            return None
        row = max(valid, key=lambda r: r.get('modifiedTimestamp', 0))
        main = row.get('sleepMainData') or row
        return {'minutes': main.get('totalSleepTime', row['totalSleepTime']),
                'bedtime': measured_at(main.get('sleepInTime')),
                'wake_time': measured_at(main.get('sleepOutTime')),
                'record_date': date.isoformat(),
                'record_modified_at': measured_at(number(row.get('modifiedTimestamp')))}
    raise ValueError('Unsupported metric')


def collect(date=None):
    date = date or dt.datetime.now(TZ).date()
    snapshot = {'date': date.isoformat(), 'fetched_at': dt.datetime.now(TZ).isoformat(),
                'source': 'OPPO 健康云端', 'metrics': {}, 'errors': {}}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {metric: pool.submit(fetch, metric, date) for metric in METRICS}
        for metric, job in jobs.items():
            try:
                result = job.result()
            except (OSError, ValueError, KeyError):
                snapshot['errors'][metric] = 'read_failed'
                continue
            if result['status'] != 'ok':
                snapshot['errors'][metric] = {key: result[key] for key in ('status', 'error_code') if key in result}
                continue
            value = summarize(metric, result.get('records', []), date)
            if value is not None:
                snapshot['metrics'][metric] = value
    # Timestamp the completed fetch so scheduler freshness checks compare
    # source-record modification time with the actual snapshot observation.
    snapshot['fetched_at'] = dt.datetime.now(TZ).isoformat()
    return snapshot


def save(snapshot, path=CACHE_FILE):
    write_private_json(snapshot, path)


def format_snapshot(snapshot):
    metrics = snapshot.get('metrics', {})
    lines = ['健康数据日期：' + snapshot['date'] + '；读取时间：' + snapshot['fetched_at']]
    if 'steps' in metrics:
        lines.append('今日步数：' + str(metrics['steps']['total']) + ' 步')
    if 'heart_rate' in metrics:
        r = metrics['heart_rate']
        lines.append('最近一次心率：' + str(r['latest']) + ' 次/分；测量时间：' + str(r['measured_at']))
        if 'resting' in r:
            lines.append('静息心率：' + str(r['resting']) + ' 次/分')
    if 'blood_oxygen' in metrics:
        r = metrics['blood_oxygen']
        lines.append('最近一次血氧：' + str(r['latest']) + '%；测量时间：' + str(r['measured_at']))
    if 'sleep' in metrics:
        mins = int(metrics['sleep']['minutes'])
        lines.append(f'本日睡眠记录：{mins // 60} 小时 {mins % 60} 分钟')
    if snapshot.get('errors'):
        lines.append('部分指标读取失败：' + '、'.join(snapshot['errors']))
    if not metrics:
        lines.append('当前没有可用记录，不能把缺失数据当作 0。')
    return '\n'.join(lines)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--date')
    args = parser.parse_args()
    today = dt.datetime.now(TZ).date()
    requested = dt.date.fromisoformat(args.date) if args.date else today
    if not today - dt.timedelta(days=6) <= requested <= today:
        parser.error('仅支持最近七天')
    result = collect(requested)
    if requested == today:
        save(result)
    output = result if args.json else {'metrics': list(result['metrics']), 'errors': result['errors']}
    print(json.dumps(output, ensure_ascii=False))
