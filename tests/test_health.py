import datetime as dt
import unittest
from zoneinfo import ZoneInfo
from unittest.mock import patch
from snapshot import summarize, collect
from cloud_client import day_range, fetch
from care_logic import wake_due, due_kind, new_activity_due

TZ=ZoneInfo('Asia/Shanghai')
DAY=dt.date(2026,10,7)
def at(hour,minute=0): return dt.datetime(2026,10,7,hour,minute,tzinfo=TZ)
def ms(hour): return int(at(hour).timestamp()*1000)

class HealthTests(unittest.TestCase):
 def test_steps_deduplicate_and_hide(self):
  a=dict(clientDataId='a',deviceUniqueId='watch',display=1,startTimestamp=ms(8),endTimestamp=ms(9),steps=100,modifiedTime=1)
  rows=[a,dict(a,steps=150,modifiedTime=2),dict(a,display=0,steps=9999),dict(a,startTimestamp=ms(8)-86400000,steps=300)]
  self.assertEqual(summarize('steps',rows,DAY)['total'],150)
 def test_heart_latest_regular_not_special_or_hidden(self):
  rows=[dict(dataCreatedTimestamp=ms(8),heartRateValue=70,heartRateType=0),dict(dataCreatedTimestamp=ms(9),heartRateValue=190,heartRateType=5),dict(dataCreatedTimestamp=ms(10),heartRateValue=200,heartRateType=0,display=0)]
  self.assertEqual(summarize('heart_rate',rows,DAY)['latest'],70)
 def test_sleep_main_and_day(self):
  r=dict(date=20261007,totalSleepTime=450,modifiedTimestamp=2,sleepMainData=dict(totalSleepTime=405,sleepInTime=ms(0),sleepOutTime=ms(8)))
  s=summarize('sleep',[r,dict(r,date=20261006,modifiedTimestamp=3)],DAY)
  self.assertEqual(s['minutes'],405)
  snap=dict(date=DAY.isoformat(),metrics={'sleep':s})
  self.assertEqual(wake_due(snap,at(10)),at(9))
 def test_missing_not_zero(self):
  self.assertIsNone(summarize('steps',[],DAY))
  with patch('snapshot.fetch',return_value={'status':'api_error','error_code':10101}):
   s=collect(DAY)
  self.assertFalse(s['metrics']); self.assertEqual(len(s['errors']),4)
 def test_sleep_due_once_and_restart(self):
  s=dict(date=DAY.isoformat(),metrics={'sleep':{'wake_time':at(8).isoformat()}})
  state=dict(date=DAY.isoformat(),activity_due=at(15).isoformat())
  self.assertIsNone(due_kind(state,s,at(8,59),at(7)))
  self.assertEqual(due_kind(state,s,at(9),at(7)),'sleep')
  self.assertIsNone(due_kind(dict(state,sleep_attempted=True),s,at(9),at(7)))
  self.assertIsNone(due_kind(state,s,at(10),at(9,30)))
 def test_random_bounds_and_quiet_period(self):
  self.assertLess(dt.datetime.fromisoformat(new_activity_due(at(13),chooser=lambda a,b:b)),at(21))
  state=dict(date=DAY.isoformat(),activity_due=at(15).isoformat(),sleep_attempted=True)
  self.assertIsNone(due_kind(state,None,at(15),at(7),at(14,45)))
  self.assertEqual(due_kind(state,None,at(15,20),at(7),at(14,45)),'activity')
  self.assertIsNone(due_kind(dict(state,activity_attempted=True),None,at(16),at(7)))
 def test_cloud_day_bounds_and_sleep_limit(self):
  requested=dt.datetime.now(TZ).date()
  b=day_range(requested); self.assertEqual(b['endTimestamp']-b['startTimestamp'],86400000-1)
  stamps=[b['startTimestamp']+i for i in range(30)]
  with patch('cloud_client.post',side_effect=lambda metric,body:{'status':'ok','body':{'modifiedTimestampList':stamps}} if metric=='sleep_version' else {'status':'ok','body':[]}) as p:
   fetch('sleep',requested)
  self.assertEqual(p.call_count,11)

if __name__=='__main__': unittest.main()
