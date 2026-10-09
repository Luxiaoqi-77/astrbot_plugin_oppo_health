import importlib
from datetime import datetime, timedelta
from pathlib import Path
import random
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
care_wellness = importlib.import_module("oppo_health_candidate.care_wellness")

TZ = ZoneInfo("Asia/Shanghai")
START = datetime(2026, 10, 9, 9, 0, tzinfo=TZ)


def configured_rule(mode="numeric"):
    rule = care_rules.default_config()["rules"]["wellness_low_state"]
    rule.update(
        enabled=True,
        mode=mode,
        timezone_confirmed=True,
        confirmation_minutes=15,
        minimum_independent_samples=2,
        recovery_debounce_minutes=15,
        maximum_sample_age_minutes=30,
        repeat_cooldown_minutes=240,
    )
    if mode == "numeric":
        rule.update(low_score_threshold=30, recovery_score_threshold=60)
    return rule


def capture(at, *, score=20, category="Slow down", point_time=None, data_date=None):
    point_time = point_time or at.strftime("%H:%M")
    data_date = data_date or at.date().isoformat()
    return {"pages": {"wellness_home": {"status": "ok", "data": {
        "source": "oppo_health_ui_ocr", "page": "wellness_home", "date": data_date,
        "observed_at": at.isoformat(),
        "metrics": {"kind": "home_point", "score": score, "category": category,
                    "point_time_local": point_time, "quality_label": "来源标记可用"},
    }}}}


class WellnessCareTests(unittest.TestCase):
    def test_default_closed_and_missing_confirmation_parameters_pause(self):
        rule = care_rules.default_config()["rules"]["wellness_low_state"]
        now = START
        state, status, sample = care_wellness.evaluate_capture(
            capture(now), rule, care_wellness.empty_runtime_state(), now
        )
        self.assertEqual(status["status"], "disabled")
        self.assertIsNone(sample)
        rule["enabled"] = True
        state, status, sample = care_wellness.evaluate_capture(
            capture(now), rule, state, now
        )
        self.assertEqual(status["status"], "parameters_unconfirmed")
        self.assertIsNone(status["low_confirmed"] if status["low_confirmed"] else None)

    def test_repeat_poll_is_not_an_independent_sample_and_sustained_low_confirms(self):
        rule = configured_rule()
        state = care_wellness.empty_runtime_state()
        state, first, _ = care_wellness.evaluate_capture(capture(START), rule, state, START)
        self.assertEqual(first["status"], "confirming_low_state")
        second_at = START + timedelta(minutes=15)
        state, second, _ = care_wellness.evaluate_capture(capture(second_at), rule, state, second_at)
        self.assertEqual(second["status"], "low_confirmed")
        episode_id = state["current_episode_id"]
        poll_at = second_at + timedelta(minutes=1)
        repeated = capture(poll_at, point_time="09:15")
        state, duplicate, _ = care_wellness.evaluate_capture(repeated, rule, state, poll_at)
        self.assertEqual(duplicate["status"], "duplicate_sample")
        self.assertEqual(state["low_sample_count"], 2)
        self.assertEqual(state["current_episode_id"], episode_id)

    def test_recovery_rearms_then_a_new_low_streak_gets_a_new_episode(self):
        rule = configured_rule()
        state = care_wellness.empty_runtime_state()
        for minute in (0, 15):
            at = START + timedelta(minutes=minute)
            state, _status, _ = care_wellness.evaluate_capture(capture(at), rule, state, at)
        old_episode = state["current_episode_id"]
        for minute in (60, 75):
            at = START + timedelta(minutes=minute)
            state, status, _ = care_wellness.evaluate_capture(
                capture(at, score=70, category="Good"), rule, state, at
            )
        self.assertEqual(status["status"], "recovered")
        self.assertTrue(state["armed"])
        for minute in (90, 105):
            at = START + timedelta(minutes=minute)
            state, status, _ = care_wellness.evaluate_capture(capture(at), rule, state, at)
        self.assertEqual(status["status"], "low_confirmed")
        self.assertNotEqual(state["current_episode_id"], old_episode)

    def test_stale_cross_day_and_missing_source_time_pause_without_fabrication(self):
        rule = configured_rule()
        state = care_wellness.empty_runtime_state()
        bad = capture(START, data_date="2026-10-08")
        state, status, sample = care_wellness.evaluate_capture(bad, rule, state, START)
        self.assertEqual(status["reason_code"], "cross_day_data")
        self.assertIsNone(sample)
        missing = capture(START)
        del missing["pages"]["wellness_home"]["data"]["metrics"]["point_time_local"]
        state, status, sample = care_wellness.evaluate_capture(missing, rule, state, START)
        self.assertEqual(status["reason_code"], "missing_source_time")
        self.assertIsNone(sample)
        old_at = START - timedelta(minutes=45)
        stale_capture = capture(old_at, data_date=START.date().isoformat())
        state, status, sample = care_wellness.evaluate_capture(stale_capture, rule, state, START)
        self.assertEqual(status["reason_code"], "stale_source_sample")
        self.assertIsNone(sample)
        missing_quality = capture(START)
        del missing_quality["pages"]["wellness_home"]["data"]["metrics"]["quality_label"]
        state, status, sample = care_wellness.evaluate_capture(missing_quality, rule, state, START)
        self.assertEqual(status["status"], "confirming_low_state")
        self.assertIsNotNone(sample)
        self.assertEqual(sample.quality, "来源未提供质量标签")
        self.assertIn("质量标签", sample.completeness)

    def test_slow_down_category_mode_needs_no_unknown_numeric_scale(self):
        rule = configured_rule("slow_down_category")
        state = care_wellness.empty_runtime_state()
        now = START
        state, status, _ = care_wellness.evaluate_capture(
            capture(now, score=None), rule, state, now
        )
        self.assertEqual(status["status"], "confirming_low_state")
        self.assertIsNone(rule["low_score_threshold"])

    def test_weekly_random_slots_allow_same_day_candidates_without_filling_missed_slots(self):
        rule = configured_rule()
        state = care_wellness.empty_runtime_state()
        state = care_wellness.ensure_weekly_slots(state, START, rule, chooser=random.Random(17))
        self.assertEqual(len(state["weekly_slots"]), 5)
        repeated_seed = care_wellness.ensure_weekly_slots(
            care_wellness.empty_runtime_state(), START, rule, chooser=random.Random(17)
        )
        self.assertEqual(repeated_seed["weekly_slots"], state["weekly_slots"])
        day_counts = {}
        for value in state["weekly_slots"]:
            local = datetime.fromisoformat(value).astimezone(TZ)
            self.assertTrue(9 <= local.hour < 21)
            day_counts[local.date()] = day_counts.get(local.date(), 0) + 1
        self.assertEqual(sum(day_counts.values()), 5)

        fixed_state = care_wellness.empty_runtime_state()
        fixed_state.update(
            schedule_week="2026-W41",
            weekly_slots=[
                "2026-10-09T09:15:00+08:00", "2026-10-09T10:00:00+08:00",
                "2026-10-10T10:00:00+08:00", "2026-10-11T10:00:00+08:00",
                "2026-10-12T10:00:00+08:00",
            ],
        )
        low_state = fixed_state
        low_state.update(armed=False, current_episode_id="episode-test")
        sample_time = START + timedelta(minutes=15)
        low_state["last_sample_id"] = "2026-10-09T09:15@Asia/Shanghai"
        sample = care_wellness.WellnessSample(
            sample_id=low_state["last_sample_id"], score=20, category="Slow down",
            measured_at=sample_time, updated_at=sample_time, data_date=sample_time.date(),
            quality="来源未提供质量标签", completeness="完整",
        )
        status = {"low_confirmed": True}
        low_state, fact, reason = care_wellness.weekly_candidate(
            low_state, sample, status, rule, sample_time
        )
        self.assertIsNotNone(fact)
        self.assertEqual(fact["kind"], "sustained_low_state")
        self.assertEqual(fact["candidate_window_end"], "2026-10-09T09:30:00+08:00")
        self.assertEqual(low_state["last_reminder_sample_id"], sample.sample_id)
        self.assertEqual(low_state["last_reminder_episode_id"], "episode-test")
        self.assertNotIn("score", fact)
        self.assertIn("占用", reason)
        low_state["used_slots"] = ["2026-10-09T09:15:00+08:00"]
        low_state["weekly_slots"][1] = "2026-10-09T10:00:00+08:00"
        later = sample_time + timedelta(minutes=45)
        newer_sample = care_wellness.WellnessSample(
            sample_id="2026-10-09T10:00@Asia/Shanghai", score=20, category="Slow down",
            measured_at=later, updated_at=later, data_date=later.date(),
            quality="来源标记可用", completeness="完整",
        )
        low_state, later_fact, later_reason = care_wellness.weekly_candidate(
            low_state, newer_sample, status, rule, later
        )
        self.assertIsNone(later_fact)
        self.assertEqual(later_reason, "同一持续状态仍在提醒间隔内")
        next_day = datetime(2026, 10, 10, 10, 0, tzinfo=TZ)
        next_day_sample = care_wellness.WellnessSample(
            sample_id="2026-10-10T10:00@Asia/Shanghai", score=20, category="Slow down",
            measured_at=next_day, updated_at=next_day, data_date=next_day.date(),
            quality="来源标记可用", completeness="完整",
        )
        low_state, repeated_episode_fact, _reason = care_wellness.weekly_candidate(
            low_state, next_day_sample, status, rule, next_day
        )
        self.assertIsNotNone(repeated_episode_fact)
        self.assertNotEqual(repeated_episode_fact["event_id"], fact["event_id"])
        self.assertEqual(low_state["last_reminder_episode_id"], "episode-test")

    def test_randomized_slots_remain_persisted_after_restart_and_weight_commands_are_exact(self):
        rule = configured_rule()
        state = care_wellness.ensure_weekly_slots(
            care_wellness.empty_runtime_state(), START, rule, chooser=random.Random(4)
        )
        persisted = care_wellness.validate_runtime_state(state)
        reloaded = care_wellness.ensure_weekly_slots(
            persisted, START + timedelta(minutes=5), rule, chooser=random.Random(999)
        )
        self.assertEqual(reloaded["weekly_slots"], persisted["weekly_slots"])
        self.assertEqual(care_wellness.explicit_weight_mode_command("我想减肥", own_private_plain_message=True), "activate")
        self.assertEqual(care_wellness.explicit_weight_mode_command("我不减肥", own_private_plain_message=True), "deactivate")
        self.assertIsNone(care_wellness.explicit_weight_mode_command("我想减肥", own_private_plain_message=False))
        self.assertIsNone(care_wellness.explicit_weight_mode_command("引用“我想减肥”", own_private_plain_message=True))

    def test_sunlight_random_check_requires_fresh_explicit_same_day_minutes(self):
        rule = care_rules.default_config()["rules"]["sunlight_evening"]
        now = datetime(2026, 10, 9, 20, 15, tzinfo=TZ)

        class FirstMinute:
            @staticmethod
            def randrange(_stop):
                return 0

        def sun_capture(duration, *, day="2026-10-09", updated=now):
            metrics = {"label": "Sun exposure", "unit": "min"}
            if duration is not None:
                metrics["duration_min"] = duration
            return {"pages": {"sun_exposure": {"status": "ok", "data": {
                "source": "oppo_health_ui_ocr", "page": "sun_exposure", "date": day,
                "observed_at": updated.isoformat(), "metrics": metrics,
            }}}}

        state, fact, status = care_wellness.sunlight_candidate(
            sun_capture(5), rule, care_wellness.empty_runtime_state(), now, chooser=FirstMinute()
        )
        self.assertIsNotNone(fact)
        self.assertEqual(fact["kind"], "sunlight_opportunity")
        self.assertEqual(status["evaluation_state"], "candidate")
        self.assertEqual(state["sunlight_used"], True)

        zero_state, zero_fact, zero_status = care_wellness.sunlight_candidate(
            sun_capture(0), rule, care_wellness.empty_runtime_state(), now, chooser=FirstMinute()
        )
        self.assertIsNotNone(zero_fact)
        self.assertEqual(zero_status["evaluation_state"], "candidate")
        self.assertTrue(zero_state["sunlight_used"])

        state, fact, status = care_wellness.sunlight_candidate(
            sun_capture(6), rule, care_wellness.empty_runtime_state(), now, chooser=FirstMinute()
        )
        self.assertIsNone(fact)
        self.assertIn("超过 5 分钟", status["reason"])

        state, fact, status = care_wellness.sunlight_candidate(
            sun_capture(None), rule, care_wellness.empty_runtime_state(), now, chooser=FirstMinute()
        )
        self.assertIsNone(fact)
        self.assertIn("不完整", status["reason"])

        state, fact, status = care_wellness.sunlight_candidate(
            sun_capture(0, day="2026-10-08"), rule,
            care_wellness.empty_runtime_state(), now, chooser=FirstMinute()
        )
        self.assertIsNone(fact)
        self.assertEqual(status["evaluation_state"], "paused_source")


if __name__ == "__main__":
    unittest.main()
