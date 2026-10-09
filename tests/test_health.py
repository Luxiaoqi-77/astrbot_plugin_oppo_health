import datetime as dt
import random
import unittest
from zoneinfo import ZoneInfo
from unittest.mock import patch
from snapshot import summarize, collect
from cloud_client import day_range, fetch
from care_logic import (
    choose_sleep_care,
    due_kind,
    explicit_goodnight_message,
    legacy_dispatch_skip_reason,
    new_activity_due,
    observe_sleep_candidate,
    sleep_record,
    wake_due,
)

TZ=ZoneInfo('Asia/Shanghai')
DAY=dt.date(2026,10,7)
def at(hour,minute=0): return dt.datetime(2026,10,7,hour,minute,tzinfo=TZ)
def ms(hour, minute=0): return int(at(hour, minute).timestamp()*1000)

def sleep_snapshot(fetched_at):
 r=dict(date=20261007,totalSleepTime=450,modifiedTimestamp=ms(8,30),sleepMainData=dict(totalSleepTime=405,sleepInTime=ms(0),sleepOutTime=ms(8)))
 sleep=summarize('sleep',[r],DAY)
 return dict(date=DAY.isoformat(),fetched_at=fetched_at.isoformat(),metrics={'sleep':sleep})

class HealthTests(unittest.TestCase):
 def test_sleep_selection_is_reproducibly_random_with_about_half_selected_and_delay_bounds(self):
  rng=random.Random(7642)
  wake=at(8)
  plans=[choose_sleep_care(f'record-{i}',wake,rng) for i in range(1000)]
  selected=[plan['selected'] for plan in plans]
  selected_count=sum(selected)
  self.assertGreaterEqual(selected_count,450)
  self.assertLessEqual(selected_count,550)
  self.assertTrue(any(selected[i] and selected[i+1] for i in range(len(selected)-1)))
  self.assertTrue(any(not selected[i] and not selected[i+1] for i in range(len(selected)-1)))
  self.assertTrue(all(60 <= plan['delay_minutes'] <= 120 for plan in plans))
  self.assertTrue(all(plan['due_at'] is None for plan in plans if not plan['selected']))

 def test_explicit_goodnight_quiets_remaining_local_day_only(self):
  self.assertTrue(explicit_goodnight_message('晚安',own_private_plain_message=True))
  self.assertTrue(explicit_goodnight_message('我先睡了。',own_private_plain_message=True))
  self.assertFalse(explicit_goodnight_message('引用“晚安”',own_private_plain_message=True))
  self.assertFalse(explicit_goodnight_message('晚安',own_private_plain_message=False))
  state=dict(date=DAY.isoformat(),activity_due=at(15).isoformat(),goodnight_date=DAY.isoformat())
  self.assertIsNone(due_kind(state,None,at(15),at(7)))
  tomorrow=DAY+dt.timedelta(days=1)
  next_time=dt.datetime.combine(tomorrow,dt.time(15),tzinfo=TZ)
  next_state=dict(date=tomorrow.isoformat(),activity_due=next_time.isoformat())
  self.assertEqual(due_kind(next_state,None,next_time,next_time-dt.timedelta(hours=8)),'activity')

 def test_steps_deduplicate_and_hide(self):
  a=dict(clientDataId='a',deviceUniqueId='watch',display=1,startTimestamp=ms(8),endTimestamp=ms(9),steps=100,modifiedTime=1)
  rows=[a,dict(a,steps=150,modifiedTime=2),dict(a,display=0,steps=9999),dict(a,startTimestamp=ms(8)-86400000,steps=300)]
  self.assertEqual(summarize('steps',rows,DAY)['total'],150)
 def test_heart_latest_regular_not_special_or_hidden(self):
  rows=[dict(dataCreatedTimestamp=ms(8),heartRateValue=70,heartRateType=0),dict(dataCreatedTimestamp=ms(9),heartRateValue=190,heartRateType=5),dict(dataCreatedTimestamp=ms(10),heartRateValue=200,heartRateType=0,display=0)]
  self.assertEqual(summarize('heart_rate',rows,DAY)['latest'],70)
 def test_sleep_main_and_day(self):
  r=dict(date=20261007,totalSleepTime=450,modifiedTimestamp=ms(8,30),sleepMainData=dict(totalSleepTime=405,sleepInTime=ms(0),sleepOutTime=ms(8)))
  s=summarize('sleep',[r,dict(r,date=20261006,modifiedTimestamp=3)],DAY)
  self.assertEqual(s['minutes'],405)
  self.assertEqual(s['record_date'],DAY.isoformat())
  self.assertEqual(s['record_modified_at'],at(8,30).isoformat())
  snap=dict(date=DAY.isoformat(),fetched_at=at(9,45).isoformat(),metrics={'sleep':s})
  self.assertEqual(wake_due(snap,at(10)),at(9))
 def test_missing_not_zero(self):
  self.assertIsNone(summarize('steps',[],DAY))
  with patch('snapshot.fetch',return_value={'status':'api_error','error_code':10101}):
   s=collect(DAY)
  self.assertFalse(s['metrics']); self.assertEqual(len(s['errors']),4)
 def test_sleep_due_once_and_restart(self):
  first=sleep_snapshot(at(8,40))
  s=sleep_snapshot(at(8,56))
  state=dict(date=DAY.isoformat(),activity_due=at(15).isoformat())
  observe_sleep_candidate(state,first,at(8,40))
  observe_sleep_candidate(state,s,at(8,56))
  self.assertIsNone(due_kind(state,s,at(8,59),at(7)))
  self.assertEqual(due_kind(state,s,at(9),at(7)),'sleep')
  self.assertIsNone(due_kind(dict(state,sleep_attempted=True),s,at(9),at(7)))
  self.assertIsNone(due_kind(state,s,at(10),at(9,30)))
 def test_random_sleep_choice_is_per_record_and_quiet_hours_are_respected(self):
  class Selected:
   @staticmethod
   def random(): return 0.2
   @staticmethod
   def randint(low, high): return high
  class NotSelected(Selected):
   @staticmethod
   def random(): return 0.8
  s=sleep_snapshot(at(8,56))
  record=sleep_record(s,at(9))
  plan=choose_sleep_care(record['record_id'],record['wake_at'],Selected())
  self.assertTrue(plan['selected'])
  self.assertEqual(plan['delay_minutes'],120)
  self.assertEqual(dt.datetime.fromisoformat(plan['due_at']),at(10))
  skipped=choose_sleep_care(record['record_id'],record['wake_at'],NotSelected())
  self.assertFalse(skipped['selected'])
  self.assertIsNone(skipped['due_at'])
  fresh=sleep_snapshot(at(22))
  state=dict(date=DAY.isoformat(),activity_due=at(21).isoformat(),sleep_plan_record_id=record['record_id'],
             sleep_care_selected=True,sleep_care_due_at=at(10).isoformat())
  observe_sleep_candidate(state,sleep_snapshot(at(8,40)),at(8,40))
  observe_sleep_candidate(state,fresh,at(22))
  self.assertIsNone(due_kind(state,fresh,at(22),at(7)))
 def test_random_bounds_and_quiet_period(self):
  self.assertLess(dt.datetime.fromisoformat(new_activity_due(at(13),chooser=lambda a,b:b)),at(21))
  state=dict(date=DAY.isoformat(),activity_due=at(15).isoformat(),sleep_attempted=True)
  self.assertIsNone(due_kind(state,None,at(15),at(7),at(14,45)))
  self.assertEqual(due_kind(state,None,at(15,20),at(7),at(14,45)),'activity')
  self.assertIsNone(due_kind(dict(state,activity_attempted=True),None,at(16),at(7)))

 def test_legacy_activity_cannot_replay_before_plugin_activation_or_after_quiet(self):
  state=dict(date=DAY.isoformat(),activity_due=at(15).isoformat(),sleep_attempted=True)
  self.assertIsNone(due_kind(state,None,at(15,20),at(15,10)))
  self.assertEqual(legacy_dispatch_skip_reason('activity',at(15).isoformat(),at(15,20),at(15,10)),'before_activation')
  self.assertEqual(legacy_dispatch_skip_reason('activity',at(15).isoformat(),at(21),at(14)),'quiet_or_expired')
  self.assertIsNone(legacy_dispatch_skip_reason('activity',at(15).isoformat(),at(15,20),at(14)))

 def test_legacy_sleep_final_gate_observes_late_grace_and_night_quiet(self):
  self.assertIsNone(legacy_dispatch_skip_reason('sleep',at(10).isoformat(),at(10,30),at(9)))
  self.assertEqual(legacy_dispatch_skip_reason('sleep',at(10).isoformat(),at(10,31),at(9)),'quiet_or_expired')
  self.assertEqual(legacy_dispatch_skip_reason('sleep',at(21,59).isoformat(),at(22),at(9)),'quiet_or_expired')
 def test_cloud_day_bounds_and_sleep_limit(self):
  requested=dt.datetime.now(TZ).date()
  b=day_range(requested); self.assertEqual(b['endTimestamp']-b['startTimestamp'],86400000-1)
  stamps=[b['startTimestamp']+i for i in range(30)]
  with patch('cloud_client.post',side_effect=lambda metric,body:{'status':'ok','body':{'modifiedTimestampList':stamps}} if metric=='sleep_version' else {'status':'ok','body':[]}) as p:
   fetch('sleep',requested)
  self.assertEqual(p.call_count,11)

if __name__=='__main__': unittest.main()
