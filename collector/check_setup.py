"""Check local prerequisites without network access or revealing account values."""
import json
import platform
from probe import load_auth
from runtime import curl_executable
from storage import is_windows


def check():
    result = {'system': platform.system(), 'storage': 'Windows user DPAPI' if is_windows() else 'POSIX private files'}
    try:
        curl_executable()
        result['curl'] = 'ok'
    except OSError:
        result['curl'] = 'missing'
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo('Asia/Shanghai')
        result['timezone'] = 'ok'
    except (ImportError, KeyError):
        result['timezone'] = 'missing; install tzdata'
    try:
        load_auth()
        result['auth'] = 'ok'
    except FileNotFoundError:
        result['auth'] = 'not_configured'
    except (OSError, ValueError):
        result['auth'] = 'invalid_or_wrong_user'
    return result

if __name__ == '__main__':
    print(json.dumps(check(), ensure_ascii=False))
