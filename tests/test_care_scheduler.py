import importlib
import asyncio
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


def _confirmed_event(event_id, *, observed_at="2026-10-09T08:00:00+08:00", occurred_at="2026-10-09T08:10:00+08:00", confirmed=True):
    return {
        "event_id": event_id,
        "confirmed": confirmed,
        "occurred_at": occurred_at,
        "observed_at": observed_at,
    }


class CareSchedulerTests(unittest.TestCase):
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
        config = _enabled_config("confirmed_period_start", max_per_day=3, duplicate_window_minutes=30)
        at = _at("2026-10-09T09:15:00")
        one = {"confirmed_period_start": [_confirmed_event("start-window-a")]}
        sender = SyntheticSender()
        state, first = care_scheduler.dispatch_due(config, one, {}, at, sender)
        self.assertEqual(first[0]["outcome"], "handed_off")
        two = {"confirmed_period_start": [_confirmed_event("start-window-b")]}
        state, second = care_scheduler.dispatch_due(config, two, state, at, sender)
        self.assertEqual(second[0]["outcome"], "skipped")
        self.assertEqual(len(sender.plans), 1)

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

    def test_late_event_reschedules_to_next_window_or_skips_by_policy(self):
        facts = {"confirmed_period_start": _confirmed_event("start-late")}
        at = _at("2026-10-09T22:00:00")
        config = _enabled_config("confirmed_period_start")
        state = care_scheduler.reconcile_jobs(config, facts, {}, at)
        self.assertEqual(next(iter(state["jobs"].values()))["expected_at"], "2026-10-10T09:00:00+08:00")
        config = _enabled_config("confirmed_period_start", reschedule="skip_if_late")
        state = care_scheduler.reconcile_jobs(config, facts, {}, at)
        self.assertEqual(next(iter(state["jobs"].values()))["state"], "skipped")

    def test_old_pending_event_moves_to_a_future_window_without_drift(self):
        config = _enabled_config("confirmed_period_start", stale_after_hours=200)
        facts = {"confirmed_period_start": _confirmed_event(
            "start-backlog", observed_at="2026-10-11T21:30:00+08:00",
        )}
        state = care_scheduler.reconcile_jobs(config, facts, {}, _at("2026-10-11T22:00:00"))
        self.assertEqual(next(iter(state["jobs"].values()))["expected_at"], "2026-10-12T09:00:00+08:00")
        state = care_scheduler.reconcile_jobs(config, facts, state, _at("2026-10-12T22:00:00"))
        self.assertEqual(next(iter(state["jobs"].values()))["expected_at"], "2026-10-13T09:00:00+08:00")

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


if __name__ == "__main__":
    unittest.main()
