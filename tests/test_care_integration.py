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


def _capture(predicted_dates=None, actual_dates=None, weight_records=None, start_events=None, end_events=None, observed_at="2026-10-09T08:00:00+08:00"):
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
                    "observed_at": observed_at,
                    "metrics": {
                        "period_dates": actual_dates or [],
                        "predicted_period_dates": predicted_dates or [],
                        "confirmed_period_start_events": start_events or [],
                        "confirmed_period_end_events": end_events or [],
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
                    "observed_at": observed_at,
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
        self.assertEqual(
            integration.enabled_capture_fields(_config("period_late_inquiry")),
            ["cycle_calendar"],
        )

    def test_status_and_sunlight_pages_are_selected_only_when_their_rules_are_enabled(self):
        config = _config("wellness_low_state", "sunlight_evening")
        self.assertEqual(
            integration.enabled_capture_fields(config),
            ["wellness_home", "sun_exposure"],
        )

    def test_prediction_uses_actual_prediction_labels_and_month_stable_event_id(self):
        config = _config("predicted_period_lead")
        capture = _capture(predicted_dates=["2026-10-12", "2026-10-13", "2026-10-14"])
        facts, contexts, statuses = integration.facts_from_capture(capture, config, NOW)
        self.assertEqual(len(facts["predicted_period"]), 3)
        prediction = facts["predicted_period"][0]
        self.assertEqual(prediction["kind"], "prediction")
        self.assertEqual(prediction["predicted_start_date"], "2026-10-12")
        self.assertEqual(prediction["event_id"], "cycle-month:2026-10:predicted:D-3")
        self.assertEqual([item["lead_days"] for item in facts["predicted_period"]], [3, 2, 1])
        self.assertIn("预测，不代表经期已经开始", contexts[next(iter(contexts))])
        updated, _contexts, _statuses = integration.facts_from_capture(
            _capture(predicted_dates=["2026-10-14", "2026-10-15"]), config, NOW
        )
        self.assertEqual(updated["predicted_period"][0]["event_id"], prediction["event_id"])
        self.assertEqual(statuses[0]["data_date"], "2026-10-12")

    def test_actual_day_markers_do_not_infer_start_or_end(self):
        config = _config("confirmed_period_start", "confirmed_period_end", "period_late_inquiry")
        facts, _contexts, statuses = integration.facts_from_capture(
            _capture(actual_dates=["2026-10-03", "2026-10-04", "2026-10-05"]), config, NOW
        )
        self.assertNotIn("confirmed_period_start", facts)
        self.assertNotIn("confirmed_period_end", facts)
        self.assertEqual(statuses[1]["status"], "unsupported")
        self.assertEqual(statuses[2]["status"], "unsupported")
        self.assertIn("未提供明确的开始/结束确认事件", statuses[2]["reason"])
        self.assertNotIn("period_late_inquiry", facts)
        late_status = next(row for row in statuses if row["source"] == "period_late_inquiry")
        self.assertEqual(late_status["evaluation_state"], "waiting_for_start")
        self.assertIn("明确记录经期开始", late_status["reason"])

    def test_late_period_probability_selects_or_skips_once_for_a_stable_day(self):
        config = _config("period_late_inquiry")
        start = {"event_id": "cycle-start-a", "confirmed": True,
                 "occurred_at": "2026-10-06T07:10:00+08:00"}
        now = datetime(2026, 10, 9, 10, 0, tzinfo=TZ)
        capture = _capture(start_events=[start], observed_at="2026-10-09T09:45:00+08:00")
        selected, contexts, statuses = integration.facts_from_capture(
            capture, config, now, random_draw=lambda _event, _day: 0.2,
        )
        skipped, _contexts, skipped_statuses = integration.facts_from_capture(
            capture, config, now, random_draw=lambda _event, _day: 0.8,
        )
        self.assertEqual(selected["period_late_inquiry"][0]["period_day"], 4)
        self.assertEqual(len(selected["period_late_inquiry"][0]["event_id"]), 64)
        context = contexts[integration.event_key("period_late_inquiry", selected["period_late_inquiry"][0]["event_id"])]
        self.assertIn("只温和询问本人是否已结束", context)
        self.assertIn("不要代替本人修改或记录 OPPO", context)
        self.assertNotIn("cycle-start-a", context)
        self.assertNotIn("2026-10-06", context)
        self.assertNotIn("period_late_inquiry", skipped)
        self.assertEqual(
            next(row for row in statuses if row["source"] == "period_late_inquiry")["evaluation_state"],
            "candidate",
        )
        self.assertEqual(
            next(row for row in skipped_statuses if row["source"] == "period_late_inquiry")["evaluation_state"],
            "not_selected",
        )

    def test_late_period_default_draw_is_reproducible_across_repeated_reads(self):
        config = _config("period_late_inquiry")
        start = {"event_id": "cycle-start-stable", "confirmed": True,
                 "occurred_at": "2026-10-06T07:10:00+08:00"}
        capture = _capture(start_events=[start], observed_at="2026-10-09T09:45:00+08:00")
        now = datetime(2026, 10, 9, 10, 0, tzinfo=TZ)
        first, _, first_status = integration.facts_from_capture(capture, config, now)
        second, _, second_status = integration.facts_from_capture(capture, config, now)
        self.assertEqual(first, second)
        self.assertEqual(first_status, second_status)

    def test_late_period_makes_one_independent_daily_opportunity_without_catch_up(self):
        config = _config("period_late_inquiry")
        start = {"event_id": "cycle-start-multi-day", "confirmed": True,
                 "occurred_at": "2026-10-06T07:10:00+08:00"}
        draws = []

        def draw(_event, day):
            draws.append(day)
            return 0.1 if day.day == 9 else 0.9

        day4, _, _ = integration.facts_from_capture(
            _capture(start_events=[start], observed_at="2026-10-09T09:45:00+08:00"),
            config, datetime(2026, 10, 9, 10, 0, tzinfo=TZ), random_draw=draw,
        )
        day5, _, statuses = integration.facts_from_capture(
            _capture(start_events=[start], observed_at="2026-10-10T09:45:00+08:00"),
            config, datetime(2026, 10, 10, 10, 0, tzinfo=TZ), random_draw=draw,
        )
        self.assertEqual(len(day4["period_late_inquiry"]), 1)
        self.assertNotIn("period_late_inquiry", day5)
        self.assertEqual(draws, [datetime(2026, 10, 9).date(), datetime(2026, 10, 10).date()])
        self.assertEqual(
            next(row for row in statuses if row["source"] == "period_late_inquiry")["evaluation_state"],
            "not_selected",
        )

    def test_late_period_waits_until_after_d3_and_never_overlaps_start_stage(self):
        config = _config("confirmed_period_start", "period_late_inquiry")
        start = {"event_id": "cycle-start-d2", "confirmed": True,
                 "occurred_at": "2026-10-08T07:10:00+08:00"}
        facts, _contexts, statuses = integration.facts_from_capture(
            _capture(start_events=[start], observed_at="2026-10-09T09:45:00+08:00"),
            config, datetime(2026, 10, 9, 10, 0, tzinfo=TZ),
            random_draw=lambda _event, _day: 0.1,
        )
        self.assertEqual(facts["confirmed_period_start"][0]["stage"], 2)
        self.assertNotIn("period_late_inquiry", facts)
        self.assertEqual(
            next(row for row in statuses if row["source"] == "period_late_inquiry")["evaluation_state"],
            "waiting_for_late_phase",
        )

    def test_late_period_explicit_end_cancels_today_and_stops_inquiry(self):
        config = _config("period_late_inquiry")
        start = {"event_id": "cycle-start-end", "confirmed": True,
                 "occurred_at": "2026-10-06T07:10:00+08:00"}
        end = {"event_id": "cycle-end-end", "confirmed": True,
               "occurred_at": "2026-10-09T09:50:00+08:00"}
        facts, _contexts, statuses = integration.facts_from_capture(
            _capture(start_events=[start], end_events=[end], observed_at="2026-10-09T09:55:00+08:00"),
            config, datetime(2026, 10, 9, 10, 0, tzinfo=TZ),
        )
        self.assertTrue(facts["period_late_inquiry"][0]["cancelled"])
        self.assertEqual(_contexts, {})
        late_status = next(row for row in statuses if row["source"] == "period_late_inquiry")
        self.assertEqual(late_status["evaluation_state"], "ended")
        self.assertIn("立即停止", late_status["reason"])

    def test_late_period_fails_closed_when_either_explicit_event_channel_is_missing(self):
        config = _config("period_late_inquiry")
        start = {"event_id": "cycle-start-no-end-channel", "confirmed": True,
                 "occurred_at": "2026-10-06T07:10:00+08:00"}
        for missing_channel in ("confirmed_period_start_events", "confirmed_period_end_events"):
            with self.subTest(missing_channel=missing_channel):
                capture = _capture(start_events=[start], observed_at="2026-10-09T09:45:00+08:00")
                capture["pages"]["cycle_calendar"]["data"]["metrics"].pop(missing_channel)
                facts, _contexts, statuses = integration.facts_from_capture(
                    capture, config, datetime(2026, 10, 9, 10, 0, tzinfo=TZ),
                    random_draw=lambda _event, _day: 0.1,
                )
                self.assertNotIn("period_late_inquiry", facts)
                late_status = next(row for row in statuses if row["source"] == "period_late_inquiry")
                self.assertEqual(late_status["status"], "unsupported")
                self.assertIn("明确经期开始和结束事件通道", late_status["reason"])

    def test_late_period_fails_closed_on_an_invalid_confirmed_end_event(self):
        config = _config("period_late_inquiry")
        start = {"event_id": "cycle-start-invalid-end", "confirmed": True,
                 "occurred_at": "2026-10-06T07:10:00+08:00"}
        future_end = {"event_id": "cycle-end-future", "confirmed": True,
                      "occurred_at": "2026-10-10T10:00:00+08:00"}
        facts, _contexts, statuses = integration.facts_from_capture(
            _capture(start_events=[start], end_events=[future_end], observed_at="2026-10-09T09:45:00+08:00"),
            config, datetime(2026, 10, 9, 10, 0, tzinfo=TZ),
            random_draw=lambda _event, _day: 0.1,
        )
        self.assertNotIn("period_late_inquiry", facts)
        late_status = next(row for row in statuses if row["source"] == "period_late_inquiry")
        self.assertEqual(late_status["status"], "unsupported")

    def test_late_period_observation_window_pauses_without_end_confirmation(self):
        config = _config("period_late_inquiry")
        config["rules"]["period_late_inquiry"]["observation_window_days"] = 14
        start = {"event_id": "cycle-start-window", "confirmed": True,
                 "occurred_at": "2026-09-25T07:10:00+08:00"}
        facts, _contexts, statuses = integration.facts_from_capture(
            _capture(start_events=[start], observed_at="2026-10-09T09:45:00+08:00"),
            config, datetime(2026, 10, 9, 10, 0, tzinfo=TZ),
            random_draw=lambda _event, _day: 0.1,
        )
        self.assertNotIn("period_late_inquiry", facts)
        late_status = next(row for row in statuses if row["source"] == "period_late_inquiry")
        self.assertEqual(late_status["evaluation_state"], "paused_window")
        self.assertIn("等待 OPPO 明确结束记录", late_status["reason"])

    def test_confirmed_start_generates_only_its_current_d1_to_d3_stage(self):
        config = _config("confirmed_period_start")
        event = {"event_id": "period-start-1", "confirmed": True,
                 "occurred_at": "2026-10-07T07:10:00+08:00"}
        facts, contexts, statuses = integration.facts_from_capture(
            _capture(start_events=[event]), config, NOW
        )
        stage = facts["confirmed_period_start"][0]
        self.assertEqual(stage["event_id"], "period-start-1:D3")
        self.assertEqual(stage["target_date"], "2026-10-09")
        self.assertEqual(stage["stage"], 3)
        self.assertIn("第 3 天", contexts[integration.event_key("confirmed_period_start", stage["event_id"])])
        self.assertEqual(statuses[1]["status"], "ok")

    def test_confirmed_start_advances_one_stage_per_local_date(self):
        config = _config("confirmed_period_start")
        event = {"event_id": "period-start-days", "confirmed": True,
                 "occurred_at": "2026-10-07T07:10:00+08:00"}
        stages = []
        for day in (7, 8, 9):
            now = datetime(2026, 10, day, 8, 30, tzinfo=TZ)
            capture = _capture(
                start_events=[event], observed_at=f"2026-10-{day:02d}T08:00:00+08:00"
            )
            facts, _contexts, _statuses = integration.facts_from_capture(capture, config, now)
            stages.append(facts["confirmed_period_start"][0]["stage"])
        self.assertEqual(stages, [1, 2, 3])

    def test_period_start_stage_skips_after_window_and_end_requires_explicit_event(self):
        config = _config("confirmed_period_start", "confirmed_period_end")
        start = {"event_id": "period-start-2", "confirmed": True,
                 "occurred_at": "2026-10-07T07:10:00+08:00"}
        end = {"event_id": "period-end-2", "confirmed": True,
               "occurred_at": "2026-10-09T08:10:00+08:00"}
        facts, _contexts, _statuses = integration.facts_from_capture(
            _capture(start_events=[start], end_events=[end]), config,
            datetime(2026, 10, 9, 22, 0, tzinfo=TZ),
        )
        self.assertNotIn("confirmed_period_start", facts)
        self.assertNotIn("confirmed_period_end", facts)

    def test_weight_facts_and_contexts_never_include_numeric_measurement(self):
        config = _config("weight_date_linked")
        facts, contexts, statuses = integration.facts_from_capture(
            _capture(weight_records=[{
                "record_date": "2026-10-08",
                "measured_at_local": "07:45",
                "observed_at": "2026-10-08T08:05:00+08:00",
                "value": 61.7,
                "unit": "kg",
            }]), config, NOW,
            weight_mode_enabled_at="2026-10-08T07:00:00+08:00",
        )
        fact_text = repr(facts["weight_entry"])
        context_text = repr(contexts)
        self.assertIn("2026-10-08", fact_text)
        self.assertIn("2026-10-08", context_text)
        self.assertNotIn("61.7", fact_text)
        self.assertNotIn("61.7", context_text)
        self.assertNotIn("value", fact_text)
        self.assertEqual(statuses[-1]["data_date"], "2026-10-08")
        self.assertEqual(facts["weight_entry"][0]["measured_at"], "2026-10-08T07:45:00+08:00")

    def test_weight_opt_in_rejects_preexisting_measurements_and_accepts_a_new_sample(self):
        config = _config("weight_date_linked")
        records = [
            {"record_date": "2026-10-08", "measured_at_local": "07:45",
             "observed_at": "2026-10-09T08:25:00+08:00"},
            {"record_date": "2026-10-09", "measured_at_local": "08:20",
             "observed_at": "2026-10-09T08:25:00+08:00"},
        ]
        facts, _contexts, statuses = integration.facts_from_capture(
            _capture(weight_records=records), config, NOW,
            weight_mode_enabled_at="2026-10-09T08:00:00+08:00",
        )
        self.assertEqual(len(facts["weight_entry"]), 1)
        self.assertEqual(facts["weight_entry"][0]["record_date"], "2026-10-09")
        self.assertIn("未提供独立记录创建时刻", statuses[-1]["completeness"])

    def test_weight_is_not_observed_without_explicit_mode_timestamp(self):
        config = _config("weight_date_linked")
        facts, _contexts, statuses = integration.facts_from_capture(
            _capture(weight_records=[{
                "record_date": "2026-10-09", "measured_at_local": "08:20",
                "observed_at": "2026-10-09T08:25:00+08:00",
            }]), config, NOW,
        )
        self.assertNotIn("weight_entry", facts)
        self.assertIn("尚未明确开启", statuses[-1]["reason"])

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
