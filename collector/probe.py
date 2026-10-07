"""Read-only OPPO cloud probe. Never print credentials or raw health records."""
import datetime
import json
import os
from pathlib import Path
import stat
import subprocess
import time

from oppo_sign import sign_headers

AUTH_FILE = Path(os.environ.get('OPPO_HEALTH_AUTH_FILE', str(Path.home() / '.local/share/astrbot-oppo-health/auth.json'))).expanduser()
HOST = 'sport.health.heytapmobi.com'
URL = 'https://' + HOST + '/sporthealth/v5/c2s/sport/steps/pullStepsDetailData'


def validate_auth(auth):
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
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise ValueError('凭证文件必须由当前用户拥有，且权限为 600')
    return validate_auth(json.loads(path.read_text()))


def save_auth(auth, path=AUTH_FILE):
    auth = validate_auth(auth)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise ValueError('凭证目录不能为符号链接')
    path.parent.chmod(0o700)
    temp = path.with_name('.auth-' + os.urandom(8).hex())
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(auth, out)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


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
    result = run(['curl', '-q', '--silent', '--show-error', '--max-time', '20',
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
