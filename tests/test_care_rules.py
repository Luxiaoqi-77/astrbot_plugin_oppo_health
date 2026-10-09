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
        self.assertEqual(config["rules"]["period_late_inquiry"]["start_day"], 4)
        self.assertEqual(config["rules"]["period_late_inquiry"]["probability"], 0.5)
        self.assertEqual(config["rules"]["period_late_inquiry"]["cooldown_days"], 1)
        self.assertEqual(config["rules"]["period_late_inquiry"]["observation_window_days"], 14)
        self.assertEqual(validate_config(config), config)
        self.assertNotIn("health_budget", config)
        self.assertEqual(config["rules"]["wellness_low_state"]["max_per_day"], 7)
        self.assertEqual(config["rules"]["wellness_low_state"]["mode"], "unconfirmed")
        self.assertTrue(all(rule["reschedule"] == "skip_if_late" for rule in config["rules"].values()))

    def test_wellness_cannot_be_enabled_until_source_parameters_are_confirmed(self):
        config = default_config()
        config["rules"]["wellness_low_state"]["enabled"] = True
        with self.assertRaisesRegex(ValueError, "cannot be enabled until"):
            validate_config(config)

        rule = config["rules"]["wellness_low_state"]
        rule.update(
            mode="slow_down_category", timezone_confirmed=True,
            confirmation_minutes=15, minimum_independent_samples=2,
            recovery_debounce_minutes=15, maximum_sample_age_minutes=30,
            repeat_cooldown_minutes=240,
        )
        self.assertTrue(validate_config(config)["rules"]["wellness_low_state"]["enabled"])

    def test_missed_windows_cannot_be_configured_to_roll_forward(self):
        config = default_config()
        config["rules"]["predicted_period_lead"]["reschedule"] = "next_allowed_window"
        with self.assertRaisesRegex(ValueError, "must skip missed windows"):
            validate_config(config)

    def test_global_health_budget_is_not_an_accepted_policy(self):
        config = default_config()
        with self.assertRaisesRegex(ValueError, "schema_version and rules"):
            validate_config({**config, "health_budget": {"enabled": True}})

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

    def test_sunlight_check_window_is_fixed_to_user_requested_hours(self):
        config = default_config()
        self.assertEqual(
            config["rules"]["sunlight_evening"]["send_window"],
            {"start": "20:00", "end": "21:00"},
        )
        config["rules"]["sunlight_evening"]["send_window"] = {
            "start": "19:00", "end": "20:00",
        }
        with self.assertRaisesRegex(ValueError, "fixed at 20:00"):
            validate_config(config)

    def test_late_period_rule_defaults_and_parameter_ranges_are_explicit(self):
        config = default_config()
        rule = config["rules"]["period_late_inquiry"]
        self.assertFalse(rule["enabled"])
        rule.update(enabled=True, start_day=4, probability=0.5, cooldown_days=1, observation_window_days=14)
        self.assertTrue(validate_config(config)["rules"]["period_late_inquiry"]["enabled"])

        for field, value, error in (
            ("start_day", 3, "start_day"),
            ("probability", 1.01, "probability"),
            ("cooldown_days", 0, "cooldown_days"),
            ("observation_window_days", 3, "observation_window_days"),
        ):
            invalid = deepcopy(config)
            invalid["rules"]["period_late_inquiry"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
                validate_config(invalid)

    def test_late_period_preview_requires_an_actual_start_and_excludes_d1_to_d3(self):
        config = default_config()
        config["rules"]["period_late_inquiry"]["enabled"] = True
        now = datetime(2026, 10, 9, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        fact = {
            "event_id": "a" * 64, "kind": "period_end_inquiry", "confirmed_start": True,
            "start_occurred_at": "2026-10-06T07:00:00+08:00",
            "observed_at": "2026-10-09T09:45:00+08:00", "data_date": "2026-10-09",
            "target_date": "2026-10-09", "period_day": 4,
            "candidate_at": "2026-10-09T09:00:00+08:00",
            "candidate_window_end": "2026-10-09T21:00:00+08:00",
        }
        plan = build_preview_plan(config, {"period_late_inquiry": fact}, now)
        row = next(item for item in plan["plans"] if item["rule_id"] == "period_late_inquiry")
        self.assertEqual(row["state"], "preview_only")
        self.assertTrue(row["is_actual_event"])

        early = deepcopy(fact)
        early.update(
            start_occurred_at="2026-10-08T07:00:00+08:00", period_day=2,
        )
        early_plan = build_preview_plan(config, {"period_late_inquiry": early}, now)
        early_row = next(item for item in early_plan["plans"] if item["rule_id"] == "period_late_inquiry")
        self.assertEqual(early_row["state"], "invalid_source")

        unconfirmed = deepcopy(fact)
        unconfirmed["confirmed_start"] = False
        unconfirmed_plan = build_preview_plan(config, {"period_late_inquiry": unconfirmed}, now)
        unconfirmed_row = next(item for item in unconfirmed_plan["plans"] if item["rule_id"] == "period_late_inquiry")
        self.assertEqual(unconfirmed_row["state"], "invalid_source")

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
                "measured_at": "2026-10-01T08:00:00+08:00",
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
            "measured_at": "2026-10-06T08:00:00+08:00",
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
