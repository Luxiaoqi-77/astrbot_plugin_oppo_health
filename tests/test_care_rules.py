import unittest
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from care_rules import RULE_IDS, build_preview_plan, default_config, validate_config


class CareRulesTests(unittest.TestCase):
    def test_all_new_rules_default_disabled_and_lead_is_three_days(self):
        config = default_config()
        self.assertEqual(set(config["rules"]), set(RULE_IDS))
        self.assertTrue(all(rule["enabled"] is False for rule in config["rules"].values()))
        self.assertEqual(config["rules"]["predicted_period_lead"]["lead_days"], 3)
        self.assertEqual(validate_config(config), config)

    def test_rejects_invalid_timezone_and_overlapping_quiet_hours(self):
        config = default_config()
        config["rules"]["weight_date_linked"]["timezone"] = "Mars/Olympus"
        with self.assertRaisesRegex(ValueError, "IANA timezone"):
            validate_config(config)

        config = default_config()
        config["rules"]["weight_date_linked"]["quiet_hours"] = {"start": "20:00", "end": "08:00"}
        with self.assertRaisesRegex(ValueError, "overlaps quiet hours"):
            validate_config(config)

    def test_rejects_non_string_policy_fields_as_validation_errors(self):
        config = default_config()
        config["rules"]["weight_date_linked"]["tone"] = []
        with self.assertRaisesRegex(ValueError, "tone is unsupported"):
            validate_config(config)

    def test_requires_explicit_confirmation_for_start_and_end(self):
        config = default_config()
        config["rules"]["confirmed_period_start"]["enabled"] = True
        config["rules"]["confirmed_period_end"]["enabled"] = True
        facts = {
            "confirmed_period_start": {
                "confirmed": False,
                "occurred_at": "2026-10-09T07:50:00+08:00",
                "observed_at": "2026-10-09T08:00:00+08:00",
            },
            "confirmed_period_end": {
                "confirmed": True,
                "occurred_at": "2026-10-09T07:55:00+08:00",
                "observed_at": "2026-10-09T08:00:00+08:00",
            },
        }
        plan = build_preview_plan(config, facts, datetime(2026, 10, 9, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
        states = {item["rule_id"]: item for item in plan["plans"]}
        self.assertEqual(states["confirmed_period_start"]["state"], "not_confirmed")
        self.assertEqual(states["confirmed_period_end"]["state"], "preview_only")
        self.assertTrue(states["confirmed_period_end"]["is_actual_event"])

    def test_missing_confirmed_end_never_infers_end_from_start(self):
        config = default_config()
        config["rules"]["confirmed_period_start"]["enabled"] = True
        config["rules"]["confirmed_period_end"]["enabled"] = True
        facts = {
            "confirmed_period_start": {
                "confirmed": True,
                "occurred_at": "2026-10-09T07:50:00+08:00",
                "observed_at": "2026-10-09T08:00:00+08:00",
            },
        }
        plan = build_preview_plan(config, facts, datetime(2026, 10, 9, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
        states = {item["rule_id"]: item for item in plan["plans"]}
        self.assertEqual(states["confirmed_period_end"]["state"], "no_source")
        self.assertIsNone(states["confirmed_period_end"]["expected_at"])

    def test_forecast_preview_is_labeled_prediction_and_not_actual(self):
        config = default_config()
        config["rules"]["predicted_period_lead"]["enabled"] = True
        facts = {
            "predicted_period": {
                "kind": "prediction",
                "predicted_start_date": "2026-10-12",
                "observed_at": "2026-10-09T08:00:00+08:00",
            },
        }
        plan = build_preview_plan(config, facts, datetime(2026, 10, 9, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
        result = next(item for item in plan["plans"] if item["rule_id"] == "predicted_period_lead")
        self.assertEqual(result["target_date"], "2026-10-09")
        self.assertEqual(result["basis"], "预测经期日期")
        self.assertFalse(result["is_actual_event"])
        self.assertTrue(result["preview_only"])

    def test_preview_is_side_effect_free_and_rejects_stale_sources(self):
        config = default_config()
        config["rules"]["weight_date_linked"]["enabled"] = True
        before = deepcopy(config)
        facts = {
            "weight_entry": {
                "record_date": "2026-10-01",
                "observed_at": "2026-10-01T08:00:00+08:00",
            },
        }
        plan = build_preview_plan(config, facts, datetime(2026, 10, 9, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
        result = next(item for item in plan["plans"] if item["rule_id"] == "weight_date_linked")
        self.assertEqual(result["state"], "stale_source")
        self.assertTrue(plan["preview_only"])
        self.assertEqual(config, before)

    def test_weight_freshness_uses_record_date_and_plan_keeps_no_weight_value(self):
        config = default_config()
        config["rules"]["weight_date_linked"]["enabled"] = True
        facts = {"weight_entry": {
            "event_id": "synthetic-weight-row",
            "record_date": "2026-10-06",
            "observed_at": "2026-10-09T08:00:00+08:00",
            "value": 63.0,
        }}
        plan = build_preview_plan(config, facts, datetime(2026, 10, 9, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
        result = next(item for item in plan["plans"] if item["rule_id"] == "weight_date_linked")
        self.assertEqual(result["state"], "stale_source")
        self.assertNotIn("value", result)

    def test_rejects_naive_now(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            build_preview_plan(default_config(), {}, datetime(2026, 10, 9, 8, 30))


if __name__ == "__main__":
    unittest.main()
