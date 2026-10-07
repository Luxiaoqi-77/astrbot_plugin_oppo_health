import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import storage
import runtime
from run_auth_emulator import server_path


class PlatformTests(unittest.TestCase):
    def test_windows_default_root(self):
        with patch('storage.is_windows', return_value=True), patch.dict(os.environ, {'LOCALAPPDATA': '/sample/local'}):
            self.assertEqual(storage.private_root(), Path('/sample/local/AstrBot/oppo-health'))

    def test_windows_exe_and_sdk_override_with_spaces(self):
        with patch('runtime.is_windows', return_value=True), patch.dict(os.environ, {'OPPO_HEALTH_ANDROID_SDK': '/sdk with spaces'}):
            self.assertEqual(runtime.sdk_tool('platform-tools/adb'), Path('/sdk with spaces/platform-tools/adb.exe'))
            self.assertEqual(runtime.sdk_tool('emulator/emulator'), Path('/sdk with spaces/emulator/emulator.exe'))
            with patch('runtime.shutil.which', return_value='C:/Windows/System32/curl.exe') as which:
                self.assertEqual(runtime.curl_executable(), 'C:/Windows/System32/curl.exe')
                which.assert_called_once_with('curl.exe')

    def test_windows_plaintext_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'auth.json'
            p.write_text(json.dumps({'token': 'ACCESS_example'}))
            with patch('storage.is_windows', return_value=True), self.assertRaises(ValueError):
                storage.read_private_json(p)

    def test_encryption_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'auth.json'; p.write_bytes(b'previous ciphertext')
            with patch('storage.is_windows', return_value=True), patch('storage._dpapi', side_effect=ValueError('cannot encrypt')):
                with self.assertRaises(ValueError):storage.write_private_json({'token':'example'},p)
            self.assertEqual(p.read_bytes(), b'previous ciphertext')
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    @unittest.skipUnless(os.name == 'nt', 'Real Windows DPAPI requires Windows')
    def test_real_windows_encrypted_roundtrip_and_tamper(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'auth.json'
            secret = {'token': 'ACCESS_unit_test_secret', 'health': 'fixture_only'}
            storage.write_private_json(secret, p)
            self.assertNotIn('ACCESS_unit_test_secret',p.read_text())
            self.assertEqual(storage.read_private_json(p),secret)
            payload=json.loads(p.read_text());payload['payload']='AAAA'
            p.write_text(json.dumps(payload))
            with self.assertRaises(ValueError):storage.read_private_json(p)

    def test_frida_guest_architecture_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'downloads').mkdir()
            binary=root/'downloads/frida-server-16.7.19-android-x86_64';binary.touch()
            with patch('run_auth_emulator.ROOT',root), patch.dict(os.environ,{},clear=True):
                self.assertEqual(server_path('x86_64'),binary)
                with self.assertRaises(FileNotFoundError):server_path('arm64-v8a')
                with self.assertRaises(ValueError):server_path('unknown')

if __name__=='__main__':unittest.main()
