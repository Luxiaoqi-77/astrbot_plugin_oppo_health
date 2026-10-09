import json
from pathlib import Path
import tempfile
import unittest

from collector.storage import read_private_json, write_private_json


class PrivateStorageTests(unittest.TestCase):
    def test_private_json_write_is_atomic_and_owner_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "state.json"

            write_private_json({"schema_version": 1, "jobs": {}}, path)

            self.assertEqual(
                read_private_json(path), {"schema_version": 1, "jobs": {}}
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["schema_version"], 1
            )
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
