"""Supervise only the dedicated authentication clone. No credential logging."""
import subprocess
import time
import socket
from start_emulator import ROOT, environment

ADB = ROOT / 'sdk/platform-tools/adb'
SERIAL = '127.0.0.1:5579'
SERVER = ROOT / 'downloads/frida-server-16.7.19-android-arm64'
REMOTE = '/data/local/tmp/astrbot-auth-inspector'

def adb(*args, check=True):
    return subprocess.run([str(ADB), '-P', '5038', '-s', SERIAL, *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=20, check=check)

def prepare():
    for attempt in range(90):
        adb('connect', SERIAL, check=False)
        r = adb('shell', 'getprop', 'sys.boot_completed', check=False)
        if r.returncode == 0 and r.stdout.strip() == '1':
            break
        time.sleep(2)
    else:
        raise RuntimeError('Dedicated emulator failed to boot')
    adb('root', check=False)
    time.sleep(3)
    adb('connect', SERIAL, check=False)
    adb('wait-for-device')
    adb('push', str(SERVER), REMOTE)
    adb('shell', 'chmod', '700', REMOTE)
    adb('shell', REMOTE + ' -D -l 127.0.0.1:27042 >/dev/null 2>&1 </dev/null', check=False)
    adb('forward', 'tcp:27742', 'tcp:27042')
    adb('shell', 'am', 'start', '-n', 'com.heytap.health/.oobe.LaunchActivity')

def main():
    # Never attach to an unknown instance using the same port.
    with socket.socket() as sock:
        if sock.connect_ex(('127.0.0.1', 5578)) == 0:
            raise RuntimeError('Dedicated emulator port is already occupied')
    subprocess.run([str(ADB), '-P', '5038', 'start-server'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    proc = subprocess.Popen([str(ROOT / 'sdk/emulator/emulator'),
        '-avd', 'AstrBot_OPPOHealth_authcheck30v2', '-port', '5578', '-memory', '2048',
        '-data', str(ROOT / 'auth-flat/userdata-flat.img'),
        '-encryption-key', str(ROOT / 'auth-flat/encryption-flat.img'),
        '-no-snapshot', '-no-audio', '-no-boot-anim', '-no-window', '-no-metrics'],
        env=environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        prepare()
        print('Dedicated health authentication assistant ready.', flush=True)
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=15)
            except subprocess.TimeoutExpired: proc.kill()

if __name__ == '__main__':
    main()
