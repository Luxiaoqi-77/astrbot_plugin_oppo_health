import importlib
from datetime import datetime
from pathlib import Path
import sys
import types
import unittest
from zoneinfo import ZoneInfo


PLUGIN_ROOT = Path(__file__).parents[1]
if "oppo_health_candidate" not in sys.modules:
    candidate = types.ModuleType("oppo_health_candidate")
    candidate.__path__ = [str(PLUGIN_ROOT)]
    sys.modules["oppo_health_candidate"] = candidate

care_rules = importlib.import_module("oppo_health_candidate.care_rules")
integration = importlib.import_module("oppo_health_candidate.care_integration")
care_scheduler = importlib.import_module("oppo_health_candidate.care_scheduler")


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 9, 8, 30, tzinfo=TZ)


def _config(*enabled):
    config = care_rules.default_config()
    for rule_id in enabled:
        config["rules"][rule_id]["enabled"] = True
    return config


def _capture(predicted_dates=None, actual_dates=None, weight_records=None):
    return {
        "status": "ok",
        "reason": None,
        "pages": {
            "cycle_calendar": {
                "status": "ok",
                "data": {
                    "source": "oppo_health_ui_ocr",
                    "page": "cycle_calendar",
                    "period": "2026-10",
                    "observed_at": "2026-10-09T08:00:00+08:00",
                    "metrics": {
                        "period_dates": actual_dates or [],
                        "predicted_period_dates": predicted_dates or [],
                        "calendar_coverage": "31/31",
                        "legend_verified": True,
                    },
                },
            },
            "weight_history": {
                "status": "ok",
                "data": {
                    "source": "oppo_health_ui_ocr",
                    "page": "weight_history",
                    "observed_at": "2026-10-09T08:05:00+08:00",
                    "metrics": {"weight_history_records": weight_records or []},
                },
            },
        },
    }


class CareIntegrationTests(unittest.TestCase):
    def test_capture_fields_are_limited_to_enabled_rule_sources(self):
        self.assertEqual(integration.enabled_capture_fields(_config()), [])
        self.assertEqual(
            integration.enabled_capture_fields(_config("predicted_period_lead", "weight_date_linked")),
            ["cycle_calendar", "weight_history"],
        )

    def test_prediction_uses_actual_prediction_labels_and_month_stable_event_id(self):
        config = _config("predicted_period_lead")
        capture = _capture(predicted_dates=["2026-10-12", "2026-10-13", "2026-10-14"])
        facts, contexts, statuses = integration.facts_from_capture(capture, config, NOW)
        prediction = facts["predicted_period"][0]
        self.assertEqual(prediction["kind"], "prediction")
        self.assertEqual(prediction["predicted_start_date"], "2026-10-12")
        self.assertEqual(prediction["event_id"], "cycle-month:2026-10:predicted")
        self.assertIn("预测，不代表经期已经开始", contexts[next(iter(contexts))])
        updated, _contexts, _statuses = integration.facts_from_capture(
            _capture(predicted_dates=["2026-10-14", "2026-10-15"]), config, NOW
        )
        self.assertEqual(updated["predicted_period"][0]["event_id"], prediction["event_id"])
        self.assertEqual(statuses[0]["data_date"], "2026-10-12")

    def test_actual_day_markers_do_not_infer_start_or_end(self):
        config = _config("confirmed_period_start", "confirmed_period_end")
        facts, _contexts, statuses = integration.facts_from_capture(
            _capture(actual_dates=["2026-10-03", "2026-10-04", "2026-10-05"]), config, NOW
        )
        self.assertNotIn("confirmed_period_start", facts)
        self.assertNotIn("confirmed_period_end", facts)
        self.assertEqual(statuses[1]["status"], "unsupported")
        self.assertEqual(statuses[2]["status"], "unsupported")
        self.assertIn("未提供明确的开始/结束确认事件", statuses[2]["reason"])

    def test_weight_facts_and_contexts_never_include_numeric_measurement(self):
        config = _config("weight_date_linked")
        facts, contexts, statuses = integration.facts_from_capture(
            _capture(weight_records=[{
                "record_date": "2026-10-08",
                "measured_at_local": "07:45",
                "recorded_at": None,
                "observed_at": "2026-10-09T08:05:00+08:00",
                "value": 61.7,
                "unit": "kg",
            }]), config, NOW
        )
        fact_text = repr(facts["weight_entry"])
        context_text = repr(contexts)
        self.assertIn("2026-10-08", fact_text)
        self.assertIn("2026-10-08", context_text)
        self.assertNotIn("61.7", fact_text)
        self.assertNotIn("61.7", context_text)
        self.assertNotIn("value", fact_text)
        self.assertEqual(statuses[-1]["data_date"], "2026-10-08")

    def test_malformed_or_non_owned_capture_is_unavailable(self):
        config = _config("weight_date_linked")
        capture = _capture(weight_records=[{
            "record_date": "2026-10-08", "observed_at": "2026-10-09T08:05:00+08:00", "value": 61.7,
        }])
        capture["pages"]["weight_history"]["data"]["source"] = "untrusted"
        facts, contexts, statuses = integration.facts_from_capture(capture, config, NOW)
        self.assertEqual(facts, {})
        self.assertEqual(contexts, {})
        self.assertEqual(statuses[-1]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
