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
        self.assertEqual(config["rules"]["wellness_low_state"]["mode"], "unconfirmed")
        self.assertTrue(all(item["reschedule"] == "skip_if_late" for item in config["rules"].values()))
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

    def test_v03_config_migrates_without_changing_legacy_rules(self):
        legacy = care_rules.default_config()
        legacy["rules"] = {key: legacy["rules"][key] for key in (
            "predicted_period_lead", "confirmed_period_start", "confirmed_period_end", "weight_date_linked",
        )}
        legacy["rules"]["predicted_period_lead"]["lead_days"] = 5
        legacy["rules"]["weight_date_linked"]["enabled"] = True
        self.repository.config_path.write_text(
            __import__("json").dumps({"schema_version": 1, "revision": 3, "config": legacy}),
            encoding="utf-8",
        )
        upgraded, revision = self.repository.load_config()
        self.assertEqual(revision, 3)
        self.assertEqual(upgraded["rules"]["predicted_period_lead"]["lead_days"], 3)
        self.assertTrue(upgraded["rules"]["weight_date_linked"]["enabled"])
        self.assertFalse(upgraded["rules"]["wellness_low_state"]["enabled"])
        self.assertEqual(upgraded["rules"]["wellness_low_state"]["low_score_threshold"], None)
        self.assertTrue(all(item["reschedule"] == "skip_if_late" for item in upgraded["rules"].values()))

    def test_previous_candidate_migrates_away_global_budget_and_restores_fixed_caps(self):
        previous_candidate = care_rules.default_config()
        previous_candidate["rules"].pop("period_late_inquiry")
        previous_candidate["health_budget"] = {
            "enabled": True,
            "timezone": "Asia/Shanghai",
            "timezone_confirmed": True,
            "max_per_day": 2,
            "minimum_interval_minutes": 240,
        }
        previous_candidate["rules"]["predicted_period_lead"]["lead_days"] = 5
        previous_candidate["rules"]["wellness_low_state"]["max_per_day"] = 5
        previous_candidate["rules"]["wellness_low_state"]["enabled"] = True
        for rule_id in ("confirmed_period_start", "confirmed_period_end", "weight_date_linked", "sunlight_evening"):
            previous_candidate["rules"][rule_id]["max_per_day"] = 2
        previous_candidate["rules"]["sunlight_evening"]["send_window"] = {
            "start": "19:00", "end": "20:00",
        }
        self.repository.config_path.write_text(
            __import__("json").dumps({
                "schema_version": 1, "revision": 4, "config": previous_candidate,
            }),
            encoding="utf-8",
        )
        upgraded, revision = self.repository.load_config()
        self.assertEqual(revision, 4)
        self.assertNotIn("health_budget", upgraded)
        self.assertEqual(upgraded["rules"]["predicted_period_lead"]["lead_days"], 3)
        self.assertEqual(upgraded["rules"]["wellness_low_state"]["max_per_day"], 7)
        self.assertFalse(upgraded["rules"]["wellness_low_state"]["enabled"])
        for rule_id in ("confirmed_period_start", "confirmed_period_end", "weight_date_linked", "sunlight_evening"):
            self.assertEqual(upgraded["rules"][rule_id]["max_per_day"], 1)
        self.assertEqual(
            upgraded["rules"]["sunlight_evening"]["send_window"],
            {"start": "20:00", "end": "21:00"},
        )
        self.assertEqual(
            upgraded["rules"]["period_late_inquiry"],
            care_rules.default_config()["rules"]["period_late_inquiry"],
        )
        self.assertTrue(all(item["reschedule"] == "skip_if_late" for item in upgraded["rules"].values()))

    def test_previous_six_rule_config_migrates_with_late_period_rule_disabled(self):
        previous = care_rules.default_config()
        previous["rules"].pop("period_late_inquiry")
        self.repository.config_path.write_text(
            __import__("json").dumps({"schema_version": 1, "revision": 6, "config": previous}),
            encoding="utf-8",
        )
        upgraded, revision = self.repository.load_config()
        self.assertEqual(revision, 6)
        self.assertFalse(upgraded["rules"]["period_late_inquiry"]["enabled"])
        self.assertEqual(upgraded["rules"]["period_late_inquiry"]["probability"], 0.5)

    def test_wellness_runtime_state_is_private_score_free_and_survives_restart(self):
        state = self.repository.load_wellness_state()
        state["current_episode_id"] = "a" * 64
        state["last_sample_id"] = "2026-10-09T09:15@Asia/Shanghai"
        state["last_sample_at"] = "2026-10-09T09:15:00+08:00"
        self.repository.save_wellness_state(state)
        loaded = self.repository.load_wellness_state()
        self.assertEqual(loaded["last_sample_id"], state["last_sample_id"])
        self.assertNotIn("score", loaded)
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(self.repository.wellness_state_path.stat().st_mode), 0o600)
        state["score"] = 24
        with self.assertRaises(care_store.CareStorageError):
            self.repository.save_wellness_state(state)


if __name__ == "__main__":
    unittest.main()
