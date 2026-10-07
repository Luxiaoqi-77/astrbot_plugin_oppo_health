"""Read-only OPPO cloud probe. Never print credentials or raw health records."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import time

from oppo_sign import sign_headers
from storage import private_root, read_private_json, write_private_json
from runtime import curl_executable

AUTH_FILE = Path(os.environ.get('OPPO_HEALTH_AUTH_FILE', str(private_root() / 'auth.json'))).expanduser()
HOST = 'sport.health.heytapmobi.com'
URL = 'https://' + HOST + '/sporthealth/v5/c2s/sport/steps/pullStepsDetailData'


def validate_auth(auth):
    if not isinstance(auth, dict):
        raise ValueError('登录凭证必须是字段对象')
    for key in ('token', 'token_auth_id', 'ssoid'):
        value = auth.get(key)
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError('登录凭证字段不完整')
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('登录凭证含非法字符')
    if not auth['token'].startswith('ACCESS_'):
        raise ValueError('登录凭证格式不符合上游接口要求')
    return {key: auth[key] for key in ('token', 'token_auth_id', 'ssoid')}


def load_auth(path=AUTH_FILE):
    return validate_auth(read_private_json(path))


def save_auth(auth, path=AUTH_FILE):
    write_private_json(validate_auth(auth), path)


def quote_config(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def read_today(auth, run=subprocess.run):
    auth = validate_auth(auth)
    start = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
    end = start + datetime.timedelta(days=1)
    body = json.dumps({'startTimestamp': int(start.timestamp() * 1000),
                       'endTimestamp': int(end.timestamp() * 1000) - 1}, separators=(',', ':'))
    headers = {'appid': 'M2QfBThPRpGcGovBva3ENX', 'app-package': 'com.heytap.health',
               'app-version': '6091900', 'nonce': os.urandom(16).hex(),
               'timestamp': str(int(time.time() * 1000)), 'token': auth['token'],
               'token-auth-id': auth['token_auth_id']}
    signature = sign_headers(headers, body)
    headers.update({'signature': signature, 'app-key-version': '1732499446015',
                    'versionName': '6.9.19_b95456c_260918', 'lang': 'zh-CN',
                    'os-type': '1', 'risk-sign': '2',
                    'Content-Type': 'application/json; charset=UTF-8',
                    'User-Agent': 'okhttp/4.9.3.7'})
    config = '\n'.join(['url = ' + quote_config(URL), 'request = "POST"'] +
                       ['header = ' + quote_config(k + ': ' + v) for k, v in headers.items()] +
                       ['data-binary = ' + quote_config(body)])
    # -q ignores user curlrc; config travels via stdin, never process argv.
    result = run([curl_executable(), '-q', '--silent', '--show-error', '--max-time', '20',
                  '--proto', '=https', '--noproxy', '*', '--config', '-',
                  '--write-out', '\n%{http_code}'], input=config,
                 text=True, capture_output=True, timeout=25)
    if result.returncode:
        return {'status': 'network_error', 'curl_code': result.returncode}
    content, _, http_status = result.stdout.rpartition('\n')
    if http_status != '200':
        return {'status': 'http_error', 'http_status': http_status if http_status.isdigit() else 'unknown'}
    try:
        response = json.loads(content)
    except ValueError:
        return {'status': 'invalid_response'}
    if not isinstance(response, dict):
        return {'status': 'invalid_response'}
    code = response.get('errorCode')
    if type(code) is not int:
        return {'status': 'invalid_response'}
    if code != 0:
        return {'status': 'api_error', 'error_code': code}
    data = response.get('body')
    if isinstance(data, list):
        rows = len(data)
    elif isinstance(data, dict):
        rows = sum(len(v) for v in data.values() if isinstance(v, list))
    else:
        return {'status': 'unexpected_data_shape'}
    return {'status': 'ok', 'metric': 'steps', 'rows': rows,
            'date': datetime.date.today().isoformat()}


if __name__ == '__main__':
    try:
        result = read_today(load_auth())
    except FileNotFoundError:
        result = {'status': 'auth_not_configured', 'note': '尚未获取手机登录凭证'}
    except (ValueError, OSError):
        result = {'status': 'invalid_auth_file', 'note': '请检查凭证字段和文件权限'}
    except subprocess.TimeoutExpired:
        result = {'status': 'network_timeout'}
    print(json.dumps(result, ensure_ascii=False))
