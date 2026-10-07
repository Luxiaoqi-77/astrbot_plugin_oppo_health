"""Read only three authentication fields from the isolated emulator copy."""
import subprocess
import threading
import json

import frida

from probe import save_auth, validate_auth
from start_emulator import ROOT


SCRIPT = """
Java.perform(function () {
    try {
        var token = Java.use('com.heytap.health.base.account.TokenHelper').getInstance();
        var account = Java.use('com.heytap.health.account.impl.manager.OppoAccountManager').getInstance();
        account.tryRefreshTokenIfNeeded();
        send({status: 'ok', auth: {
            token: String(token.getToken()),
            token_auth_id: String(token.getDeviceId()),
            ssoid: String(account.getSsoid())
        }});
    } catch (e) { send({status: 'read_failed'}); }
});
"""


def extract():
    adb = ROOT / 'sdk/platform-tools/adb'
    pid = subprocess.check_output(
        [str(adb), '-P', '5038', '-s', '127.0.0.1:5579', 'shell', 'pidof', 'com.heytap.health'],
        text=True, timeout=10).strip().split()[0]
    device = frida.get_device_manager().add_remote_device('127.0.0.1:27742')
    session = device.attach(int(pid))
    done = threading.Event()
    result = {'status': 'timeout'}

    def on_message(message, _data):
        if message.get('type') != 'send':
            result['status'] = 'instrumentation_failed'
            done.set()
            return
        payload = message.get('payload', {})
        try:
            if payload.get('status') == 'ok':
                auth = validate_auth(payload['auth'])
                if any(v == 'null' for v in auth.values()):
                    raise ValueError('empty account field')
                save_auth(auth)
                auth.clear()
                result['status'] = 'saved'
            else:
                result['status'] = 'read_failed'
        except (KeyError, ValueError, OSError):
            result['status'] = 'invalid_account_fields'
        finally:
            payload.clear()
            done.set()

    try:
        script = session.create_script(SCRIPT)
        script.on('message', on_message)
        script.load()
        done.wait(20)
    finally:
        session.detach()
    return result


if __name__ == '__main__':
    print(json.dumps(extract()))
