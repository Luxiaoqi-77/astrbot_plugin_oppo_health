"""Bounded, read-only OPPO cloud calls; credentials never enter process argv."""
import datetime as dt
import json
import os
import subprocess
import time
from zoneinfo import ZoneInfo

from probe import HOST, load_auth, quote_config, validate_auth
from oppo_sign import sign_headers

TZ = ZoneInfo('Asia/Shanghai')
ENDPOINTS = {
    'steps': 'v5/c2s/sport/steps/pullStepsDetailData',
    'heart_rate': 'v5/c2s/health/heartRate/pullHeartRateDetailData',
    'blood_oxygen': 'v5/c2s/health/bloodOxygen/pullBloodOxygenDetailData',
    'sleep_version': 'v3/c2s/health/sleep/querySleepDayStatVersion',
    'sleep': 'v5/c2s/health/sleep/querySleepDayStatData',
}


def post(metric, body_obj, auth=None, run=subprocess.run):
    auth = validate_auth(auth or load_auth())
    endpoint = ENDPOINTS[metric]
    body = json.dumps(body_obj, separators=(',', ':'))
    headers = {'appid': 'M2QfBThPRpGcGovBva3ENX', 'app-package': 'com.heytap.health',
               'app-version': '6091900', 'nonce': os.urandom(16).hex(),
               'timestamp': str(int(time.time() * 1000)), 'token': auth['token'],
               'token-auth-id': auth['token_auth_id']}
    headers['signature'] = sign_headers(headers, body)
    headers.update({'app-key-version': '1732499446015', 'versionName': '6.9.19_b95456c_260918',
                    'lang': 'zh-CN', 'os-type': '1', 'risk-sign': '2',
                    'Content-Type': 'application/json; charset=UTF-8',
                    'User-Agent': 'okhttp/4.9.3.7'})
    config = '\n'.join(['url = ' + quote_config('https://' + HOST + '/sporthealth/' + endpoint),
                        'request = "POST"'] +
                       ['header = ' + quote_config(k + ': ' + v) for k, v in headers.items()] +
                       ['data-binary = ' + quote_config(body)])
    try:
        result = run(['curl', '-q', '--silent', '--show-error', '--max-time', '20',
                      '--proto', '=https', '--noproxy', '*', '--config', '-',
                      '--write-out', '\n%{http_code}'], input=config,
                     text=True, capture_output=True, timeout=25)
    except subprocess.TimeoutExpired:
        return {'status': 'network_timeout'}
    if result.returncode:
        return {'status': 'network_error'}
    content, _, http_status = result.stdout.rpartition('\n')
    try:
        response = json.loads(content)
    except ValueError:
        return {'status': 'invalid_response'}
    if not isinstance(response, dict) or type(response.get('errorCode')) is not int:
        return {'status': 'invalid_response'}
    if http_status != '200' or response['errorCode'] != 0:
        return {'status': 'api_error', 'http_status': http_status,
                'error_code': response['errorCode']}
    return {'status': 'ok', 'body': response.get('body')}


def day_range(date=None):
    date = date or dt.datetime.now(TZ).date()
    start = dt.datetime.combine(date, dt.time.min, TZ)
    return {'startTimestamp': int(start.timestamp() * 1000),
            'endTimestamp': int((start + dt.timedelta(days=1)).timestamp() * 1000) - 1}


def records(body):
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    if isinstance(body, dict):
        return [r for value in body.values() if isinstance(value, list)
                for r in value if isinstance(r, dict)]
    return []


def fetch(metric, date=None):
    if metric in ('steps', 'heart_rate', 'blood_oxygen'):
        result = post(metric, day_range(date))
        if result['status'] == 'ok':
            result['records'] = records(result.pop('body'))
        return result
    if metric != 'sleep':
        raise ValueError('Unsupported health metric')
    date = date or dt.datetime.now(TZ).date()
    lower = day_range(date - dt.timedelta(days=2))['startTimestamp']
    # Historical nights may have been uploaded or edited today.
    upper = day_range(dt.datetime.now(TZ).date())['endTimestamp']
    versions = post('sleep_version', {'queryFlag': 0, 'modifiedTimestamp': lower})
    if versions['status'] != 'ok':
        return versions
    body = versions.get('body')
    if not isinstance(body, dict) or not isinstance(body.get('modifiedTimestampList'), list):
        return {'status': 'invalid_response'}
    timestamps = sorted({v for v in body['modifiedTimestampList']
                         if type(v) is int and lower <= v <= upper}, reverse=True)[:10]
    items = []
    for stamp in timestamps:
        result = post('sleep', {'queryFlag': 1, 'modifiedTimestamp': stamp})
        if result['status'] != 'ok':
            return result
        items.extend(records(result.get('body')))
    return {'status': 'ok', 'records': items}
