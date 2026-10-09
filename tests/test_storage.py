import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collector.storage import read_private_json, write_private_json


class PrivateStorageTests(unittest.TestCase):
    def test_private_json_is_fsynced_before_atomic_replace(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            real_fsync = os.fsync
            real_replace = os.replace
            operations = []

            def fsync_and_record(fd):
                operations.append("fsync")
                return real_fsync(fd)

            def replace_and_record(source, destination):
                operations.append("replace")
                return real_replace(source, destination)

            with patch("collector.storage.os.fsync", side_effect=fsync_and_record):
                with patch("collector.storage.os.replace", side_effect=replace_and_record):
                    write_private_json({"schema_version": 1}, path)

            self.assertEqual(operations, ["fsync", "replace"])
            self.assertEqual(read_private_json(path), {"schema_version": 1})

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
