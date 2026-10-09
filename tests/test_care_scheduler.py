import importlib
import asyncio
from datetime import datetime, timedelta
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
care_scheduler = importlib.import_module("oppo_health_candidate.care_scheduler")


TZ = ZoneInfo("Asia/Shanghai")


class SyntheticSender:
    def __init__(self, fail=False):
        self.plans = []
        self.fail = fail

    def dispatch(self, plan):
        self.plans.append(plan)
        if self.fail:
            raise RuntimeError("synthetic secret body must not be persisted")


def _at(value):
    return datetime.fromisoformat(value).replace(tzinfo=TZ)


def _enabled_config(rule_id, **overrides):
    config = care_rules.default_config()
    rule = config["rules"][rule_id]
    rule["enabled"] = True
    rule.update(overrides)
    return config


def _configure_wellness(rule):
    rule.update(
        enabled=True, mode="slow_down_category", timezone_confirmed=True,
        confirmation_minutes=15, minimum_independent_samples=2,
        recovery_debounce_minutes=15, maximum_sample_age_minutes=30,
        repeat_cooldown_minutes=240,
    )


def _confirmed_event(event_id, *, observed_at="2026-10-09T08:00:00+08:00", occurred_at="2026-10-09T08:10:00+08:00", confirmed=True):
    return {
        "event_id": event_id,
        "confirmed": confirmed,
        "occurred_at": occurred_at,
        "observed_at": observed_at,
    }


def _period_late_fact(event_id, day, period_day, observed_at):
    return {
        "event_id": event_id,
        "kind": "period_end_inquiry",
        "confirmed_start": True,
        "start_occurred_at": f"2026-10-{day - period_day + 1:02d}T07:10:00+08:00",
        "observed_at": observed_at,
        "data_date": f"2026-10-{day:02d}",
        "target_date": f"2026-10-{day:02d}",
        "period_day": period_day,
        "candidate_at": f"2026-10-{day:02d}T09:00:00+08:00",
        "candidate_window_end": f"2026-10-{day:02d}T21:00:00+08:00",
    }


class CareSchedulerTests(unittest.TestCase):
    def test_reconcile_prunes_expired_jobs_but_keeps_recent_and_dispatching_jobs(self):
        now = _at("2026-10-09T10:00:00")
        config = care_rules.default_config()
        config["rules"]["confirmed_period_end"]["enabled"] = True

        def job(job_id, window_end, state):
            return {
                "event_key": job_id,
                "rule_id": "confirmed_period_end",
                "expected_at": (window_end - timedelta(hours=1)).isoformat(),
                "window_end": window_end.isoformat(),
                "timezone": "Asia/Shanghai",
                "state": state,
                "reason": "synthetic",
                "updated_at": now.isoformat(),
            }

        old_key = care_scheduler._event_key("confirmed_period_end", "old-event")
        recent_key = care_scheduler._event_key("confirmed_period_end", "recent-event")
        dispatching_key = care_scheduler._event_key("confirmed_period_end", "dispatching-event")
        present_key = care_scheduler._event_key("confirmed_period_end", "present-old-event")
        state = {
            "schema_version": 1,
            "jobs": {
                old_key: job(old_key, now - timedelta(days=181), "handed_off"),
                recent_key: job(recent_key, now - timedelta(days=179), "handed_off"),
                dispatching_key: job(dispatching_key, now - timedelta(days=181), "dispatching"),
                present_key: job(present_key, now - timedelta(days=181), "handed_off"),
            },
            "attempts": [],
        }

        old_event = _confirmed_event(
            "present-old-event",
            occurred_at=(now - timedelta(days=181)).isoformat(),
            observed_at=now.isoformat(),
        )
        updated = care_scheduler.reconcile_jobs(
            config, {"confirmed_period_end": [old_event]}, state, now,
        )

        self.assertNotIn(old_key, updated["jobs"])
        self.assertIn(recent_key, updated["jobs"])
        self.assertIn(dispatching_key, updated["jobs"])
        self.assertIn(present_key, updated["jobs"])

    def test_late_period_selected_day_is_idempotent_across_repeated_polling(self):
        config = _enabled_config("period_late_inquiry")
        fact = _period_late_fact(
            "period-event-day-4", 9, 4, "2026-10-09T09:45:00+08:00",
        )
        sender = SyntheticSender()
        now = _at("2026-10-09T10:00:00")
        state, first = care_scheduler.dispatch_due(
            config, {"period_late_inquiry": [fact]}, {}, now, sender,
        )
        state, second = care_scheduler.dispatch_due(
            config, {"period_late_inquiry": [fact]}, state, _at("2026-10-09T10:15:00"), sender,
        )
        self.assertEqual([row["outcome"] for row in first], ["handed_off"])
        self.assertEqual(second, [])
        self.assertEqual(len(sender.plans), 1)
        self.assertNotIn("period-event-day-4", repr(state))

    def test_late_period_cooldown_skips_a_later_randomly_selected_day(self):
        config = _enabled_config("period_late_inquiry", cooldown_days=2)
        first = _period_late_fact("period-event-day-4", 9, 4, "2026-10-09T09:45:00+08:00")
        second = _period_late_fact("period-event-day-5", 10, 5, "2026-10-10T09:45:00+08:00")
        sender = SyntheticSender()
        state, result = care_scheduler.dispatch_due(
            config, {"period_late_inquiry": [first]}, {}, _at("2026-10-09T10:00:00"), sender,
        )
        self.assertEqual(result[0]["outcome"], "handed_off")
        state, result = care_scheduler.dispatch_due(
            config, {"period_late_inquiry": [second]}, state, _at("2026-10-10T10:00:00"), sender,
        )
        self.assertEqual(result[0]["outcome"], "skipped")
        self.assertIn("2 天冷却期", result[0]["reason"])
        self.assertEqual(len(sender.plans), 1)

    def test_explicit_period_end_cancels_pending_late_inquiry(self):
        config = _enabled_config("period_late_inquiry")
        fact = _period_late_fact(
            "period-event-day-4", 9, 4, "2026-10-09T09:45:00+08:00",
        )
        state = care_scheduler.reconcile_jobs(
            config, {"period_late_inquiry": [fact]}, {}, _at("2026-10-09T09:50:00"),
        )
        cancel = {**fact, "cancelled": True}
        state = care_scheduler.reconcile_jobs(
            config, {"period_late_inquiry": [cancel]}, state, _at("2026-10-09T09:55:00"),
        )
        cancelled = next(iter(state["jobs"].values()))
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertIn("明确记录经期结束", cancelled["reason"])
        sender = SyntheticSender()
        state, result = care_scheduler.dispatch_due(
            config, {"period_late_inquiry": [cancel]}, state, _at("2026-10-09T10:00:00"), sender,
        )
        self.assertEqual(result, [])
        self.assertEqual(sender.plans, [])

    def test_forecast_date_update_reschedules_pending_job_in_place(self):
        config = _enabled_config("predicted_period_lead")
        first = {"predicted_period": {
            "event_id": "forecast-cycle-1", "kind": "prediction",
            "predicted_start_date": "2026-10-12", "observed_at": "2026-10-09T08:00:00+08:00",
        }}
        state = care_scheduler.reconcile_jobs(config, first, {}, _at("2026-10-09T08:30:00"))
        key = next(iter(state["jobs"]))
        self.assertEqual(state["jobs"][key]["target_date"], "2026-10-09")
        self.assertEqual(state["jobs"][key]["state"], "pending")

        updated = {"predicted_period": {
            **first["predicted_period"], "predicted_start_date": "2026-10-14",
        }}
        state2 = care_scheduler.reconcile_jobs(config, updated, state, _at("2026-10-09T08:40:00"))
        self.assertEqual(list(state2["jobs"]), [key])
        self.assertEqual(state2["jobs"][key]["target_date"], "2026-10-11")
        self.assertEqual(state2["jobs"][key]["expected_at"], "2026-10-11T09:00:00+08:00")

    def test_start_and_end_require_explicit_confirmation_and_never_infer_end(self):
        config = care_rules.default_config()
        config["rules"]["confirmed_period_start"]["enabled"] = True
        config["rules"]["confirmed_period_end"]["enabled"] = True
        facts = {
            "confirmed_period_start": _confirmed_event("start-1", confirmed=False),
        }
        state = care_scheduler.reconcile_jobs(config, facts, {}, _at("2026-10-09T08:30:00"))
        self.assertEqual(state["jobs"], {})

    def test_stale_source_pauses_an_existing_pending_job(self):
        config = _enabled_config("confirmed_period_start")
        fresh = {"confirmed_period_start": _confirmed_event("start-2")}
        state = care_scheduler.reconcile_jobs(config, fresh, {}, _at("2026-10-09T08:30:00"))
        stale = {"confirmed_period_start": _confirmed_event(
            "start-2", observed_at="2026-10-09T08:00:00+08:00", occurred_at="2026-10-01T08:00:00+08:00",
        )}
        paused = care_scheduler.reconcile_jobs(config, stale, state, _at("2026-10-09T08:30:00"))
        job = next(iter(paused["jobs"].values()))
        self.assertEqual(job["state"], "paused_stale")

    def test_explicit_cancellation_cancels_pending_job_without_dispatch(self):
        config = _enabled_config("confirmed_period_start")
        facts = {"confirmed_period_start": _confirmed_event("start-cancel")}
        state = care_scheduler.reconcile_jobs(config, facts, {}, _at("2026-10-09T08:30:00"))
        cancelled_facts = {"confirmed_period_start": {
            "event_id": "start-cancel", "cancelled": True,
        }}
        state = care_scheduler.reconcile_jobs(config, cancelled_facts, state, _at("2026-10-09T08:40:00"))
        self.assertEqual(next(iter(state["jobs"].values()))["state"], "cancelled")
        sender = SyntheticSender()
        state, result = care_scheduler.dispatch_due(config, cancelled_facts, state, _at("2026-10-09T10:00:00"), sender)
        self.assertEqual(result, [])
        self.assertEqual(sender.plans, [])

    def test_frequency_cap_counts_failed_attempts_and_skips_additional_job(self):
        config = _enabled_config("confirmed_period_start", max_per_day=1, duplicate_window_minutes=1)
        facts = {"confirmed_period_start": [
            _confirmed_event("start-a"), _confirmed_event("start-b"),
        ]}
        sender = SyntheticSender(fail=True)
        state, results = care_scheduler.dispatch_due(
            config, facts, {}, _at("2026-10-09T09:15:00"), sender,
        )
        outcomes = [row["outcome"] for row in results]
        self.assertEqual(outcomes.count("failed"), 1)
        self.assertEqual(outcomes.count("skipped"), 1)
        self.assertEqual(len(sender.plans), 1)
        failed = next(row for row in state["attempts"] if row["outcome"] == "failed")
        self.assertNotIn("synthetic secret body", failed["reason"])

    def test_duplicate_window_deduplicates_separate_events(self):
        config = _enabled_config("confirmed_period_start", duplicate_window_minutes=30)
        at = _at("2026-10-09T09:15:00")
        state = {"attempts": [{
            "rule_id": "confirmed_period_start", "outcome": "handed_off",
            "at": "2026-10-09T09:00:00+08:00",
        }]}
        job = {"rule_id": "confirmed_period_start"}
        self.assertEqual(
            care_scheduler._is_duplicate(state, job, config["rules"]["confirmed_period_start"], at),
            "处于重复提醒保护时段",
        )

    def test_state_is_reserved_before_adapter_and_uncertain_restart_is_not_retried(self):
        config = _enabled_config("confirmed_period_start")
        facts = {"confirmed_period_start": _confirmed_event("start-restart")}
        snapshots = []
        sender = SyntheticSender()
        state, results = care_scheduler.dispatch_due(
            config, facts, {}, _at("2026-10-09T09:15:00"), sender,
            persist=lambda snapshot: snapshots.append(snapshot),
        )
        self.assertEqual(snapshots[1]["attempts"][-1]["outcome"], "dispatching")
        self.assertEqual(results[0]["outcome"], "handed_off")

        job_id = next(iter(state["jobs"]))
        state["jobs"][job_id]["state"] = "dispatching"
        state["attempts"][-1]["outcome"] = "dispatching"
        recovered = care_scheduler.recover_interrupted_attempts(state, _at("2026-10-09T09:20:00"))
        self.assertEqual(recovered["jobs"][job_id]["state"], "failed_uncertain")
        self.assertEqual(recovered["attempts"][-1]["outcome"], "failed_uncertain")
        sender2 = SyntheticSender()
        recovered, retry = care_scheduler.dispatch_due(
            config, facts, recovered, _at("2026-10-09T10:00:00"), sender2,
        )
        self.assertEqual(retry, [])
        self.assertEqual(sender2.plans, [])

    def test_late_event_is_skipped_without_roll_forward(self):
        facts = {"confirmed_period_start": _confirmed_event("start-late")}
        at = _at("2026-10-09T22:00:00")
        config = _enabled_config("confirmed_period_start")
        state = care_scheduler.reconcile_jobs(config, facts, {}, at)
        job = next(iter(state["jobs"].values()))
        self.assertEqual(job["state"], "skipped")
        self.assertEqual(job["expected_at"], "2026-10-09T09:00:00+08:00")
        self.assertIn("不补发", job["reason"])

    def test_old_period_event_never_rolls_into_a_future_window(self):
        config = _enabled_config("confirmed_period_start", stale_after_hours=200)
        facts = {"confirmed_period_start": _confirmed_event(
            "start-backlog", occurred_at="2026-10-08T10:00:00+08:00",
            observed_at="2026-10-11T21:30:00+08:00",
        )}
        state = care_scheduler.reconcile_jobs(config, facts, {}, _at("2026-10-11T22:00:00"))
        job = next(iter(state["jobs"].values()))
        self.assertEqual(job["state"], "skipped")
        self.assertEqual(job["expected_at"], "2026-10-08T09:00:00+08:00")
        state = care_scheduler.reconcile_jobs(config, facts, state, _at("2026-10-12T22:00:00"))
        self.assertEqual(next(iter(state["jobs"].values()))["state"], "skipped")

    def test_first_activation_skips_windows_already_open_before_startup(self):
        config = _enabled_config("confirmed_period_start")
        facts = {"confirmed_period_start": _confirmed_event(
            "start-first-load", occurred_at="2026-10-09T08:10:00+08:00",
            observed_at="2026-10-09T09:05:00+08:00",
        )}
        activation = _at("2026-10-09T09:10:00")
        sender = SyntheticSender()
        state, results = asyncio.run(care_scheduler.dispatch_due_async(
            config, facts, {}, _at("2026-10-09T09:15:00"), sender,
            not_before=activation,
        ))
        self.assertEqual(sender.plans, [])
        self.assertEqual(results, [])
        self.assertEqual(next(iter(state["jobs"].values()))["state"], "skipped")
        self.assertIn("启用时刻", next(iter(state["jobs"].values()))["reason"])

    def test_dispatch_window_guard_blocks_expired_and_quiet_jobs(self):
        rule = care_rules.default_config()["rules"]["confirmed_period_start"]
        job = {"expected_at": "2026-10-09T20:00:00+08:00", "window_end": "2026-10-09T21:00:00+08:00"}
        self.assertIn(
            "错过",
            care_scheduler.dispatch_window_skip_reason(rule, job, _at("2026-10-09T21:00:00")),
        )
        rule["send_window"] = {"start": "21:00", "end": "23:00"}
        job["expected_at"] = "2026-10-09T21:30:00+08:00"
        job["window_end"] = "2026-10-09T23:00:00+08:00"
        self.assertIn(
            "免打扰",
            care_scheduler.dispatch_window_skip_reason(rule, job, _at("2026-10-09T22:00:00")),
        )

    def test_quiet_hours_overlap_is_rejected_before_scheduling(self):
        config = _enabled_config("weight_date_linked")
        config["rules"]["weight_date_linked"]["send_window"] = {"start": "07:00", "end": "09:00"}
        with self.assertRaisesRegex(ValueError, "overlaps quiet hours"):
            care_rules.validate_config(config)

    def test_async_ingress_receives_only_metadata_and_persists_result(self):
        config = _enabled_config("confirmed_period_start")
        facts = {"confirmed_period_start": _confirmed_event("start-async")}
        now = _at("2026-10-09T09:15:00")
        snapshots = []

        class AsyncSender:
            def __init__(self):
                self.plans = []

            async def dispatch(self, plan):
                self.plans.append(plan)
                await asyncio.sleep(0)

        sender = AsyncSender()
        state, results = asyncio.run(care_scheduler.dispatch_due_async(
            config, facts, {}, now, sender, persist=lambda snapshot: snapshots.append(snapshot)
        ))
        self.assertEqual(results[0]["outcome"], "handed_off")
        self.assertEqual(sender.plans[0]["event_id"], results[0]["job_id"])
        self.assertEqual(sender.plans[0]["source_ref"], "oppo_health_local_page")
        self.assertNotIn("occurred_at", sender.plans[0])
        self.assertNotIn("synthetic", repr(sender.plans[0]))
        self.assertEqual(snapshots[1]["attempts"][-1]["outcome"], "dispatching")
        self.assertEqual(state["jobs"][results[0]["job_id"]]["state"], "handed_off")
        self.assertIn("平台送达状态未确认", state["jobs"][results[0]["job_id"]]["reason"])

    def test_async_refusal_is_redacted_and_never_retried(self):
        config = _enabled_config("confirmed_period_start")
        facts = {"confirmed_period_start": _confirmed_event("start-refused")}
        now = _at("2026-10-09T09:15:00")

        class RefusingSender:
            async def dispatch(self, _plan):
                raise care_scheduler.DispatchRejected("provider_unapproved")

        state, results = asyncio.run(care_scheduler.dispatch_due_async(
            config, facts, {}, now, RefusingSender()
        ))
        self.assertEqual(results[0]["outcome"], "failed")
        self.assertEqual(results[0]["reason"], "当前模型服务未获本机健康数据授权，已暂停提交")
        self.assertEqual(state["attempts"][-1]["outcome"], "failed")
        self.assertNotIn("approved.example", repr(state))

    def test_goodnight_dispatch_refusal_is_recorded_as_skipped_not_failed(self):
        config = _enabled_config("confirmed_period_start")
        facts = {"confirmed_period_start": _confirmed_event("start-goodnight")}
        now = _at("2026-10-09T09:15:00")

        class GoodnightSender:
            async def dispatch(self, _plan):
                raise care_scheduler.DispatchRejected("goodnight_quiet")

        state, results = asyncio.run(care_scheduler.dispatch_due_async(
            config, facts, {}, now, GoodnightSender()
        ))
        self.assertEqual(results[0]["outcome"], "skipped")
        self.assertIn("晚安", results[0]["reason"])
        self.assertEqual(state["attempts"][-1]["outcome"], "skipped")

    def test_wellness_candidate_uses_source_time_window_and_never_transmits_a_score(self):
        config = _enabled_config("wellness_low_state")
        _configure_wellness(config["rules"]["wellness_low_state"])
        event = {
            "event_id": "a" * 64,
            "kind": "sustained_low_state",
            "data_date": "2026-10-09",
            "observed_at": "2026-10-09T09:15:00+08:00",
            "candidate_at": "2026-10-09T09:00:00+08:00",
            "candidate_window_end": "2026-10-09T09:30:00+08:00",
        }
        now = _at("2026-10-09T09:15:00")
        state = care_scheduler.reconcile_jobs(
            config, {"wellness_low_state": [event]}, {}, now
        )
        job = next(item for item in state["jobs"].values() if item["rule_id"] == "wellness_low_state")
        self.assertEqual(job["state"], "pending")
        self.assertEqual(job["expected_at"], "2026-10-09T09:00:00+08:00")
        sender = SyntheticSender()
        state, results = asyncio.run(care_scheduler.dispatch_due_async(
            config, {"wellness_low_state": [event]}, state, now, sender
        ))
        self.assertEqual(results[0]["outcome"], "handed_off")
        self.assertEqual(sender.plans[0]["rule_id"], "wellness_low_state")
        self.assertNotIn("score", repr(sender.plans[0]))

    def test_coincident_health_reasons_are_handed_off_as_one_batch(self):
        config = care_rules.default_config()
        config["rules"]["confirmed_period_start"]["enabled"] = True
        config["rules"]["confirmed_period_end"]["enabled"] = True
        now = _at("2026-10-09T09:15:00")
        facts = {
            "confirmed_period_start": _confirmed_event(
                "period-start", occurred_at="2026-10-09T08:00:00+08:00"
            ),
            "confirmed_period_end": _confirmed_event(
                "period-end", occurred_at="2026-10-09T08:05:00+08:00"
            ),
        }

        class BatchSender:
            def __init__(self):
                self.batches = []

            async def dispatch_batch(self, plans):
                self.batches.append(plans)
                return {"outcome": "handed_off"}

            def dispatch(self, _plan):
                raise AssertionError("batch sender should not dispatch one reason at a time")

        sender = BatchSender()
        state, results = asyncio.run(care_scheduler.dispatch_due_async(
            config, facts, {}, now, sender
        ))
        self.assertEqual(len(sender.batches), 1)
        self.assertEqual(len(sender.batches[0]), 2)
        self.assertEqual({row["rule_id"] for row in sender.batches[0]}, {
            "confirmed_period_start", "confirmed_period_end"
        })
        self.assertEqual(len(results), 2)
        self.assertTrue(all(row["outcome"] == "handed_off" for row in results))
        self.assertTrue(all(job["state"] == "handed_off" for job in state["jobs"].values()))

    def test_seven_categories_keep_independent_daily_caps_without_shared_budget(self):
        config = care_rules.default_config()
        for rule in config["rules"].values():
            rule["enabled"] = True
            rule["send_window"] = {"start": "20:00", "end": "21:00"}
        _configure_wellness(config["rules"]["wellness_low_state"])
        now = _at("2026-10-09T20:20:00")
        facts = {
            "predicted_period": [{
                "event_id": "forecast-cycle-6", "kind": "prediction",
                "predicted_start_date": "2026-10-12", "lead_days": 3,
                "observed_at": "2026-10-09T20:00:00+08:00",
            }],
            "confirmed_period_start": [_confirmed_event(
                "period-start-6", occurred_at="2026-10-09T20:05:00+08:00",
                observed_at="2026-10-09T20:06:00+08:00",
            )],
            "confirmed_period_end": [_confirmed_event(
                "period-end-6", occurred_at="2026-10-09T20:07:00+08:00",
                observed_at="2026-10-09T20:08:00+08:00",
            )],
            "period_late_inquiry": [{
                **_period_late_fact("period-late-6", 9, 4, "2026-10-09T20:00:00+08:00"),
                "candidate_at": "2026-10-09T20:00:00+08:00",
                "candidate_window_end": "2026-10-09T21:00:00+08:00",
            }],
            "weight_entry": [{
                "event_id": "weight-6", "record_date": "2026-10-09",
                "measured_at": "2026-10-09T20:10:00+08:00",
                "observed_at": "2026-10-09T20:11:00+08:00",
            }],
            "wellness_low_state": [{
                "event_id": "a" * 64, "kind": "sustained_low_state",
                "data_date": "2026-10-09", "observed_at": "2026-10-09T20:15:00+08:00",
                "candidate_at": "2026-10-09T20:15:00+08:00",
                "candidate_window_end": "2026-10-09T20:30:00+08:00",
            }],
            "sunlight_evening": [{
                "event_id": "b" * 64, "kind": "sunlight_opportunity",
                "data_date": "2026-10-09", "observed_at": "2026-10-09T20:15:00+08:00",
                "candidate_at": "2026-10-09T20:15:00+08:00",
                "candidate_window_end": "2026-10-09T20:30:00+08:00",
            }],
        }

        class BatchSender:
            def __init__(self):
                self.batches = []

            async def dispatch_batch(self, plans):
                self.batches.append(plans)
                return {"outcome": "handed_off"}

            def dispatch(self, _plan):
                raise AssertionError("batch sender should receive the handoff")

        sender = BatchSender()
        state, results = asyncio.run(care_scheduler.dispatch_due_async(
            config, facts, {}, now, sender
        ))
        self.assertEqual(len(sender.batches), 1)
        self.assertEqual(len(sender.batches[0]), 7)
        self.assertEqual({plan["rule_id"] for plan in sender.batches[0]}, set(config["rules"]))
        self.assertEqual(len(results), 7)
        self.assertTrue(all(result["outcome"] == "handed_off" for result in results))
        self.assertNotIn("health_budget", state)


if __name__ == "__main__":
    unittest.main()
