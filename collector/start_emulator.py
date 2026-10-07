"""Start only the dedicated OPPO health test virtual device."""
import os
from pathlib import Path
import subprocess

ROOT = Path.home() / '.local/share/astrbot-oppo-health/emulator'


def environment():
    env = os.environ.copy()
    env.update({'JAVA_HOME': (ROOT / 'java-home.txt').read_text().strip(),
                'ANDROID_USER_HOME': str(ROOT / 'android-user'),
                'ANDROID_EMULATOR_HOME': str(ROOT / 'android-user'),
                'ANDROID_AVD_HOME': str(ROOT / 'avd'),
                'ANDROID_SDK_ROOT': str(ROOT / 'sdk'),
                'ANDROID_HOME': str(ROOT / 'sdk'),
                'ANDROID_ADB_SERVER_PORT': '5038',
                'ADB_SERVER_SOCKET': 'tcp:localhost:5038'})
    return env


if __name__ == '__main__':
    subprocess.run([str(ROOT / 'sdk/platform-tools/adb'), '-P', '5038', 'start-server'], check=True)
    subprocess.run([str(ROOT / 'sdk/emulator/emulator'),
                    '-avd', 'AstrBot_OPPOHealth_user30', '-port', '5576',
                    '-memory', '3072', '-no-boot-anim', '-no-snapshot-save',
                    '-no-audio', '-no-metrics'], env=environment(), check=True)
