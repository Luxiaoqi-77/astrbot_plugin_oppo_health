import importlib
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest


PLUGIN_ROOT = Path(__file__).parents[1]
if "oppo_health_candidate" not in sys.modules:
    candidate = types.ModuleType("oppo_health_candidate")
    candidate.__path__ = [str(PLUGIN_ROOT)]
    sys.modules["oppo_health_candidate"] = candidate
care_rules = importlib.import_module("oppo_health_candidate.care_rules")
care_store = importlib.import_module("oppo_health_candidate.care_store")


class CareStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "private-care"
        self.repository = care_store.CareRepository(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_defaults_disabled_and_revisioned_save_is_atomic(self):
        config, revision = self.repository.load_config()
        self.assertEqual(revision, 0)
        self.assertTrue(all(not item["enabled"] for item in config["rules"].values()))
        config["rules"]["weight_date_linked"]["enabled"] = True
        saved, new_revision = self.repository.save_config(config, revision)
        self.assertEqual(new_revision, 1)
        self.assertTrue(saved["rules"]["weight_date_linked"]["enabled"])
        loaded, loaded_revision = self.repository.load_config()
        self.assertEqual((loaded, loaded_revision), (saved, 1))
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(self.repository.config_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_stale_revision_is_rejected_without_overwriting_newer_config(self):
        config, revision = self.repository.load_config()
        self.repository.save_config(config, revision)
        config["rules"]["weight_date_linked"]["enabled"] = True
        with self.assertRaises(care_store.CareRevisionConflict):
            self.repository.save_config(config, revision)
        current, current_revision = self.repository.load_config()
        self.assertEqual(current_revision, 1)
        self.assertFalse(current["rules"]["weight_date_linked"]["enabled"])

    def test_corrupt_config_and_scheduler_state_fail_closed(self):
        self.repository.config_path.write_text("not json", encoding="utf-8")
        with self.assertRaises(care_store.CareStorageError):
            self.repository.load_config()
        self.repository.config_path.unlink()
        self.repository.state_path.write_text('{"schema_version":1,"jobs":[],"attempts":[]}', encoding="utf-8")
        with self.assertRaises(care_store.CareStorageError):
            self.repository.load_scheduler_state()

    def test_scheduler_state_persists_only_supported_fields(self):
        state = care_store.empty_scheduler_state()
        state["jobs"]["job-key"] = {
            "event_key": "event-key", "rule_id": "confirmed_period_start",
            "state": "pending", "expected_at": "2026-10-09T09:00:00+08:00",
        }
        state["attempts"].append({
            "job_id": "job-key", "rule_id": "confirmed_period_start",
            "at": "2026-10-09T09:00:00+08:00", "outcome": "handed_off",
        })
        self.repository.save_scheduler_state(state)
        self.assertEqual(self.repository.load_scheduler_state(), state)

    def test_scheduler_state_rejects_unknown_health_payload_fields(self):
        state = care_store.empty_scheduler_state()
        state["attempts"].append({
            "job_id": "job-key", "rule_id": "weight_date_linked",
            "at": "2026-10-09T09:00:00+08:00", "outcome": "failed",
            "weight_value": 63.0,
        })
        with self.assertRaises(care_store.CareStorageError):
            self.repository.save_scheduler_state(state)


if __name__ == "__main__":
    unittest.main()
