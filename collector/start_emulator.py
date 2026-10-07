"""Start the dedicated login AVD on macOS or Windows."""
import os
import subprocess
from runtime import ROOT, environment, sdk_tool

if __name__ == '__main__':
    subprocess.run([str(sdk_tool('platform-tools/adb')), '-P', '5038', 'start-server'], check=True)
    subprocess.run([str(sdk_tool('emulator/emulator')),
                    '-avd', os.environ.get('OPPO_HEALTH_LOGIN_AVD', 'AstrBot_OPPOHealth_user30'),
                    '-port', '5576', '-memory', '3072', '-no-boot-anim', '-no-snapshot-save',
                    '-no-audio', '-no-metrics'], env=environment(), check=True)
