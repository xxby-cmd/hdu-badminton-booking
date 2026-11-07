import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import unittest
from unittest.mock import patch
from datetime import datetime,timezone,timedelta
import threading
import tempfile
from booking import Plan,Trial,Reply,BookingError,Uncertain,Stopped,candidates,indices,payload_for,mark_submission,booking_options,plan_for,Option,Api,ServerClock

META={'time_list':['08:00','09:00','10:00','11:00','11:40','13:20','14:00','15:00','16:00','17:00','18:00','19:00','20:00','21:00'],
 'rest_list':[0,1,2,3,9,10,11,12],'base_price':0,'days_can_appoint':3}
SESSION={'token':'TEST_ONLY','openid':'TEST_ONLY','nickname':'TEST_ONLY','phone':'TEST_ONLY'}

def availability(booked=None,opened=None,price=0):
 return {'site_situation':[{'site_id':1,'book_time_list':booked or [],'oci_time_list':opened if opened is not None else [11,12]}],
  'extra_list':{'extra_price_time':[11,12] if price else [],'extra_price_list':[price,price] if price else []}}

def order(site=1):
 return {'order_num':'test-order','order_date':'2026-11-05','venue_name':'综合馆羽毛球',
  'venue_type':'badminton','total_price':0,'site_id':site,'start_time':'19:00','end_time':'21:00'}

class Clock:
 def __init__(self,value=None):self.value=Plan().release.timestamp() if value is None else value
 def now(self):return self.value
 def sync(self,api,timeout=5):pass

class FakeApi:
 session=SESSION
 def __init__(self,check=None,create=None,order_reads=None):
  self.calls=[];self.check=check or Reply(200,{'conflict_times':[],'available_times':[11,12]})
  self.create=create or Reply(200,{'data':{'order_num':'test-order'}})
  self.order_reads=list(order_reads or [[],[order()]])
  self.snapshot=availability()
 def orders(self,invited=False,timeout=5):
  self.calls.append(('orders',invited))
  return self.order_reads.pop(0) if self.order_reads else []
 def call(self,endpoint,payload=None,timeout=5):
  self.calls.append((endpoint,payload))
  if endpoint=='post_site_situation':return Reply(200,self.snapshot)
  result=self.check if endpoint=='creat_book_info' else self.create
  if isinstance(result,Exception):raise result
  return result

class RoutedApi(FakeApi):
 def __init__(self,days=3,order_reads=None):
  super().__init__(order_reads=order_reads or [[],[],[order()]])
  self.days=days
 def call(self,endpoint,payload=None,timeout=5):
  if endpoint=='post_venue_info':
   self.calls.append((endpoint,payload));return Reply(200,{**META,'days_can_appoint':self.days})
  if endpoint=='post_site_situation':
   self.calls.append((endpoint,payload));return Reply(200,availability())
  return super().call(endpoint,payload)

class NoWaitEvent:
 def is_set(self):return False
 def wait(self,seconds):return False

class TrialTests(unittest.TestCase):
 def make(self,api,clock=None,stop=None,plan=None):
  events=[]
  trial=Trial(api,plan or Plan(),publish=lambda **e:events.append(e),clock=clock or Clock(),stop=stop,shuffle=lambda x:None)
  return trial,events

 def test_requested_interval_maps_exactly_to_two_slots(self):
  self.assertEqual(indices(META,Plan()),[11,12])
  payload=payload_for(SESSION,META,Plan(),7,0)['orderData']
  self.assertEqual((payload['site_id'],payload['start_time'],payload['end_time'],payload['time_list']),(7,'19:00','21:00',[11,12]))

 def test_school_opening_toggles_and_booking_status_are_respected(self):
  self.assertEqual(candidates(META,availability(),Plan()),[(1,0)])
  self.assertEqual(candidates(META,availability(booked=[12]),Plan()),[])
  self.assertEqual(candidates(META,availability(opened=[11]),Plan()),[])
  self.assertEqual(candidates(META,availability(opened=[]),Plan()),[])

 def test_no_paid_order_is_allowed(self):
  with self.assertRaises(BookingError):candidates(META,availability(price=1),Plan())

 def test_missing_or_changed_time_labels_stop_safely(self):
  with self.assertRaises(BookingError):indices({**META,'time_list':['19:00','20:00']},Plan())

 def test_full_success_requires_authoritative_order_list(self):
  api=FakeApi();trial,events=self.make(api)
  result=trial.attempt(META,[(1,0)])
  self.assertEqual(result['order_num'],'test-order')
  self.assertEqual(events[-1]['state'],'CREATED_PENDING_INVITE')
  self.assertEqual(sum(e[0]=='creat_order' for e in api.calls),1)

 def test_partial_conflict_never_books_only_one_hour(self):
  api=FakeApi(check=Reply(200,{'conflict_times':[12],'available_times':[11]}))
  trial,_=self.make(api,plan=Plan(allow_one_hour=False))
  with self.assertRaises(BookingError):trial.attempt(META,[(1,0)])
  self.assertNotIn('creat_order',[e[0] for e in api.calls])

 def test_conflict_moves_to_another_court_without_partial_order(self):
  api=FakeApi(order_reads=[[],[order(2)]])
  api.snapshot={'site_situation':[
   {'site_id':1,'book_time_list':[12],'oci_time_list':[11,12]},
   {'site_id':2,'book_time_list':[],'oci_time_list':[11,12]}],
   'extra_list':{'extra_price_time':[],'extra_price_list':[]}}
  original=api.call
  def call(endpoint,payload=None,timeout=5):
   if endpoint=='creat_book_info' and payload['orderData']['site_id']==1:
    api.calls.append((endpoint,payload));return Reply(200,{'conflict_times':[11],'available_times':[12]})
   return original(endpoint,payload,timeout=timeout)
  api.call=call;trial,_=self.make(api)
  self.assertEqual(trial.attempt(META,[(1,0),(2,0)])['site_id'],2)
  creates=[p for e,p in api.calls if e=='creat_order']
  self.assertEqual(len(creates),1);self.assertEqual(creates[0]['orderData']['site_id'],2)

 def test_create_timeout_with_successful_order_is_recognized_without_retry(self):
  api=FakeApi(create=BookingError('timeout'),order_reads=[[],[],[order()]])
  trial,events=self.make(api)
  with patch('booking.time.sleep'):result=trial.attempt(META,[(1,0),(2,0)])
  self.assertEqual(result['order_num'],'test-order')
  self.assertEqual(sum(e[0]=='creat_order' for e in api.calls),1)

 def test_ambiguous_timeout_never_retries_or_reports_success(self):
  api=FakeApi(create=BookingError('timeout'),order_reads=[[],[],[],[]])
  trial,events=self.make(api)
  with patch('booking.time.sleep'),self.assertRaises(Uncertain):trial.attempt(META,[(1,0),(2,0)])
  self.assertEqual(sum(e[0]=='creat_order' for e in api.calls),1)
  self.assertFalse(any(e['state']=='CREATED_PENDING_INVITE' for e in events))

 def test_http_success_with_no_order_is_not_success(self):
  api=FakeApi(create=Reply(200,{'message':'failed'}),order_reads=[[],[],[],[]])
  trial,events=self.make(api)
  with patch('booking.time.sleep'),self.assertRaises(Uncertain):trial.attempt(META,[(1,0)])
  self.assertFalse(any(e['state']=='CREATED_PENDING_INVITE' for e in events))

 def test_wrong_order_number_or_time_is_not_success(self):
  api=FakeApi(order_reads=[[],[{**order(),'end_time':'20:00'}],[{**order(),'order_num':'other'}],[]])
  trial,_=self.make(api)
  with patch('booking.time.sleep'),self.assertRaises(Uncertain):trial.attempt(META,[(1,0)])

 def test_existing_order_blocks_duplicate_submission(self):
  api=FakeApi(order_reads=[[order()]])
  trial,_=self.make(api)
  with self.assertRaises(BookingError):trial.attempt(META,[(1,0)])
  self.assertFalse(any(e[0].startswith('creat') for e in api.calls))

 def test_before_release_and_stale_plan_never_write(self):
  for offset in [-.1,21]:
   api=FakeApi();trial,_=self.make(api,Clock(Plan().release.timestamp()+offset))
   with self.assertRaises(BookingError):trial.attempt(META,[(1,0)])
   self.assertEqual(api.calls,[])

 def test_stop_button_prevents_submission(self):
  stop=threading.Event();stop.set();api=FakeApi();trial,_=self.make(api,stop=stop)
  with self.assertRaises(Stopped):trial.attempt(META,[(1,0)])
  self.assertEqual(api.calls,[])

 def test_failed_check_does_not_create_or_try_a_second_court(self):
  api=FakeApi(check=BookingError('timeout'));trial,_=self.make(api)
  with self.assertRaises(Uncertain):trial.attempt(META,[(1,0),(2,0)])
  self.assertEqual(sum(e[0]=='creat_book_info' for e in api.calls),1)
  self.assertNotIn('creat_order',[e[0] for e in api.calls])

 def test_preflight_is_read_only(self):
  api=RoutedApi();trial,_=self.make(api)
  trial.preflight()
  self.assertEqual([e[0] for e in api.calls],['orders','orders','post_venue_info','post_site_situation'])

 def test_full_scheduled_flow_creates_one_exact_order(self):
  api=RoutedApi();trial,events=self.make(api)
  self.assertEqual(trial.run()['order_num'],'test-order')
  self.assertEqual(sum(e[0]=='creat_order' for e in api.calls),1)
  self.assertEqual(events[-1]['state'],'CREATED_PENDING_INVITE')

 def test_date_still_closed_at_release_never_submits(self):
  api=RoutedApi(days=2);trial,events=self.make(api,stop=NoWaitEvent())
  api.check=Reply(201,{'message':'目标日期尚未开放'})
  self.assertIsNone(trial.run())
  self.assertEqual(events[-1]['state'],'FAILED')
  self.assertNotIn('creat_order',[e[0] for e in api.calls])

 def test_preflight_failure_stops_without_booking(self):
  api=RoutedApi();trial,events=self.make(api)
  api.orders=lambda invited,timeout=5:(_ for _ in ()).throw(BookingError('登录失效'))
  self.assertIsNone(trial.run())
  self.assertEqual(events[-1]['state'],'FAILED')
  self.assertFalse(any(e[0].startswith('creat') for e in api.calls))

 def test_restart_cannot_overwrite_submission_marker(self):
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'submitted.json'
   mark_submission(path,Plan(),1);first=path.read_bytes()
   with self.assertRaises(BookingError):mark_submission(path,Plan(),2)
   self.assertEqual(path.read_bytes(),first)

 def test_persisted_submission_blocks_a_second_create(self):
  api=FakeApi();trial,_=self.make(api)
  trial.before_create=lambda active,site:(_ for _ in ()).throw(BookingError('already submitted'))
  with self.assertRaises(BookingError):trial.attempt(META,[(1,0)])
  self.assertNotIn('creat_order',[e[0] for e in api.calls])

 def test_one_hour_19_to_20_is_booked_after_full_interval_conflict(self):
  self.check_hour_fallback(booked=12,start='19:00',end='20:00')

 def test_one_hour_20_to_21_is_booked_after_full_interval_conflict(self):
  self.check_hour_fallback(booked=11,start='20:00',end='21:00')

 def check_hour_fallback(self,booked,start,end):
  active_order={**order(),'start_time':start,'end_time':end}
  api=FakeApi(order_reads=[[],[active_order]])
  api.snapshot=availability(booked=[booked])
  original=api.call
  def call(endpoint,payload=None,timeout=5):
   if endpoint=='creat_book_info':
    api.calls.append((endpoint,payload))
    ids=payload['orderData']['time_list']
    return Reply(200,{'conflict_times':[booked] if booked in ids else [],'available_times':[i for i in ids if i!=booked]})
   return original(endpoint,payload,timeout=timeout)
  api.call=call;trial,events=self.make(api)
  result=trial.attempt(META,[(1,0)])
  self.assertEqual((result['start_time'],result['end_time']),(start,end))
  creates=[payload['orderData'] for endpoint,payload in api.calls if endpoint=='creat_order']
  self.assertEqual(len(creates),1)
  self.assertEqual(creates[0]['time_list'],[11] if start=='19:00' else [12])
  self.assertEqual((events[-1]['booked_start'],events[-1]['booked_end']),(start,end))

 def test_fallback_candidates_stay_within_authorized_window(self):
  choices=booking_options(META,availability(),Plan())
  self.assertEqual([(o.plan.start,o.plan.end) for o in choices],[('19:00','21:00'),('19:00','20:00'),('20:00','21:00')])

 def test_formal_plan_releases_wednesday_for_friday(self):
  plan=plan_for('formal')
  self.assertEqual(plan.release.isoformat(),'2026-11-04T20:00:00+08:00')
  self.assertEqual(plan.day,'2026-11-06')

 def test_fast_path_has_two_booking_requests_then_order_verification(self):
  api=RoutedApi();trial,_=self.make(api)
  self.assertIsNotNone(trial.run())
  calls=[endpoint for endpoint,_ in api.calls]
  first=calls.index('creat_book_info')
  self.assertEqual(calls[first:],['creat_book_info','creat_order','orders'])

 def test_network_failure_does_not_resend_creation(self):
  connection=unittest.mock.Mock()
  connection.request.side_effect=OSError('network error')
  with patch('booking.urllib.request.getproxies',return_value={}),patch('booking.http.client.HTTPSConnection',return_value=connection):
   api=Api(SESSION)
   with self.assertRaises(BookingError):api.call('creat_order',{'orderData':{}})
  self.assertEqual(connection.request.call_count,1)

 def test_clock_does_not_add_server_processing_delay_to_server_time(self):
  clock=ServerClock();api=unittest.mock.Mock()
  api.call.return_value=Reply(200,{'data':1793690000000})
  with patch('booking.time.monotonic',side_effect=[100,100.2,100.5]):
   clock.sync(api)
   self.assertAlmostEqual(clock.now(),1793690000.3,places=4)

if __name__=='__main__':unittest.main(verbosity=2)
