"""Import your own health credentials interactively; never echo their values."""
import getpass
import json
from probe import save_auth

def main():
    try:
        auth = {key: getpass.getpass(label + '（输入不显示）: ')
                for key, label in [('token', '健康 Token'),
                                   ('token_auth_id', 'Token Auth ID / deviceId'),
                                   ('ssoid', 'SSOID')]}
        save_auth(auth)
        auth.clear()
        print(json.dumps({'status': 'saved'}))
    except (OSError, ValueError, EOFError, KeyboardInterrupt):
        print(json.dumps({'status': 'not_saved'}))
        raise SystemExit(1)

if __name__ == '__main__':
    main()
