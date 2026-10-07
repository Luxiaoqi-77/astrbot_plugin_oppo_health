import datetime
import json
from pathlib import Path
import stat
import tempfile
import unittest
from types import SimpleNamespace

import probe
from storage import is_windows


class ProbeTests(unittest.TestCase):
    auth = {'token': 'ACCESS_dummy_secret', 'token_auth_id': 'device_secret', 'ssoid': 'dummy_user'}

    def test_private_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'private' / 'auth.json'
            probe.save_auth(self.auth, path)
            if not is_windows():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(probe.load_auth(path), self.auth)
            if not is_windows():
                path.chmod(0o644)
                with self.assertRaises(ValueError):
                    probe.load_auth(path)

    def test_bad_credentials_rejected(self):
        for invalid in (None, [], 'not-a-map'):
            with self.assertRaises(ValueError):
                probe.validate_auth(invalid)
        for change in ({'token': 'wrong'}, {'ssoid': ''}, {'token_auth_id': 'secret\nheader'}):
            with self.assertRaises(ValueError):
                probe.read_today(dict(self.auth, **change), lambda *a, **k: self.fail('must not send'))

    def test_one_day_request_and_redaction(self):
        def run(argv, **kwargs):
            self.assertNotIn(self.auth['token'], ' '.join(argv))
            self.assertNotIn(self.auth['token_auth_id'], ' '.join(argv))
            self.assertIn(probe.URL, kwargs['input'])
            self.assertNotIn('t-route-tag', kwargs['input'])
            data_line = next(v for v in kwargs['input'].splitlines() if v.startswith('data-binary = '))
            body = json.loads(json.loads(data_line.split(' = ', 1)[1]))
            start = datetime.datetime.fromtimestamp(body['startTimestamp'] / 1000)
            end = datetime.datetime.fromtimestamp((body['endTimestamp'] + 1) / 1000)
            self.assertEqual(start.date(), datetime.date.today())
            self.assertEqual(end.date(), start.date() + datetime.timedelta(days=1))
            return SimpleNamespace(returncode=0, stdout=json.dumps({'errorCode': 0, 'body': {'data': [{'steps': 999}]}}) + '\n200')
        result = probe.read_today(self.auth, run)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['rows'], 1)
        self.assertNotIn('999', json.dumps(result))

    def test_error_never_claims_success(self):
        samples = [({'errorCode': 10100, 'message': self.auth['token']}, '200', 'api_error'),
                   ({'errorCode': 0, 'body': []}, '403', 'http_error'),
                   ({'body': []}, '200', 'invalid_response')]
        for payload, status, expected in samples:
            def run(*a, **k):
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload) + '\n' + status)
            result = probe.read_today(self.auth, run)
            self.assertEqual(result['status'], expected)
            self.assertNotIn(self.auth['token'], json.dumps(result))


if __name__ == '__main__':
    unittest.main()
