"""Host executables and configurable emulator paths."""
import os
import shutil
from pathlib import Path
from storage import is_windows, private_root

ROOT = Path(os.environ.get('OPPO_HEALTH_EMULATOR_ROOT', str(private_root() / 'emulator'))).expanduser()

def sdk_root():
    return Path(os.environ.get('OPPO_HEALTH_ANDROID_SDK', str(ROOT / 'sdk'))).expanduser()

def sdk_tool(relative):
    suffix = '.exe' if is_windows() else ''
    return sdk_root() / (relative + suffix)

def curl_executable():
    name = 'curl.exe' if is_windows() else 'curl'
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError('需要安装 curl，并将其加入 PATH')
    return found

def environment():
    env = os.environ.copy()
    java = ROOT / 'java-home.txt'
    if java.exists():
        env['JAVA_HOME'] = java.read_text(encoding='utf-8').strip()
    env.update({'ANDROID_USER_HOME': str(ROOT / 'android-user'),
                'ANDROID_EMULATOR_HOME': str(ROOT / 'android-user'),
                'ANDROID_AVD_HOME': os.environ.get('OPPO_HEALTH_AVD_HOME', str(ROOT / 'avd')),
                'ANDROID_SDK_ROOT': str(sdk_root()), 'ANDROID_HOME': str(sdk_root()),
                'ANDROID_ADB_SERVER_PORT': '5038', 'ADB_SERVER_SOCKET': 'tcp:localhost:5038'})
    return env
