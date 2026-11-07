from copy import deepcopy
import ast
from dataclasses import replace
import math
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from booking import (AuthenticationError, BookingError, CandidatePlanner, NetworkError, ReadServiceError,
                     Option, Plan, Reply, ServerClock, Trial, Uncertain, booking_options,exact_order)
from test_booking import Clock, META, SESSION, availability


class AdvancingStop:
    def __init__(self,clock):self.clock=clock;self.stopped=False
    def is_set(self):return self.stopped
    def wait(self,seconds):self.clock.value+=seconds;return self.stopped


class School:
    def __init__(self,clock):
        self.clock=clock;self.session=dict(SESSION);self.calls=[];self.timings=[]
        self.active=[];self.order_reads=[];self.validation=None;self.order_error=None
        self.snapshot=lambda:availability()

    def orders(self,invited=False,timeout=5):
        self.order_reads.append(invited)
        if self.order_error:raise self.order_error()
        return [] if invited else deepcopy(self.active)

    def call(self,endpoint,payload=None,timeout=5):
        self.calls.append((endpoint,deepcopy(payload),timeout))
        if endpoint=='post_venue_info':return Reply(200,deepcopy(META))
        if endpoint=='post_site_situation':return Reply(200,self.snapshot())
        if endpoint=='creat_book_info':
            if self.validation:return self.validation(payload)
            return Reply(200,{'conflict_times':[],'available_times':payload['orderData']['time_list']})
        if endpoint=='creat_order':
            row=payload['orderData']
            self.active=[{'order_num':'OFFLINE_ONLY','order_date':row['date'],
                         'venue_name':row['venue_name'],'venue_type':row['venue_type'],
                         'site_id':row['site_id'],'start_time':row['start_time'],
                         'end_time':row['end_time'],'total_price':row['total_price']}]
            return Reply(200,{'data':{'order_num':'OFFLINE_ONLY'}})
        raise AssertionError(endpoint)


class ResilientTrialTests(unittest.TestCase):
    def make(self,offset=0):
        clock=Clock(Plan().release.timestamp()+offset)
        api=School(clock);events=[]
        trial=Trial(api,Plan(),clock=clock,stop=AdvancingStop(clock),
                    publish=lambda **event:events.append(event),shuffle=lambda values:None)
        trial.wait_until=lambda target:setattr(clock,'value',max(clock.value,target))
        return trial,api,clock,events

    def writes(self,api):
        return [endpoint for endpoint,_,_ in api.calls if endpoint.startswith('creat_')]

    def test_slot_conflict_memory_uses_other_hour_without_repeating_or_refreshing(self):
        trial,api,clock,_=self.make()
        started=clock.now()
        def check(payload):
            wanted=payload['orderData']['time_list']
            return Reply(200,{'conflict_times':[11] if 11 in wanted else [],
                              'available_times':[i for i in wanted if i!=11]})
        api.validation=check
        result=trial.attempt(META,booking_options(META,availability(),Plan()))
        self.assertEqual((result['start_time'],result['end_time']),('20:00','21:00'))
        self.assertEqual(self.writes(api),['creat_book_info','creat_book_info','creat_order'])
        self.assertFalse(any(endpoint=='post_site_situation' for endpoint,_,_ in api.calls))
        self.assertAlmostEqual(clock.now()-started,.025,places=5)

    def test_twelve_full_court_conflicts_still_reach_authorized_late_hour(self):
        trial,api,_,_=self.make()
        snapshot=availability()
        snapshot['site_situation']=[{**deepcopy(snapshot['site_situation'][0]),'site_id':i}
                                    for i in range(1,13)]
        def check(payload):
            wanted=payload['orderData']['time_list']
            return Reply(200,{'conflict_times':[11] if 11 in wanted else [],
                              'available_times':[i for i in wanted if i!=11]})
        api.validation=check
        result=trial.attempt(META,booking_options(META,snapshot,Plan()))
        self.assertEqual((result['start_time'],result['end_time']),('20:00','21:00'))
        self.assertEqual(self.writes(api).count('creat_book_info'),13)
        self.assertEqual(self.writes(api).count('creat_order'),1)
        self.assertFalse(any(endpoint=='post_site_situation' for endpoint,_,_ in api.calls))

    def test_preflight_empty_inventory_can_open_at_release_without_starting_over(self):
        trial,api,clock,_=self.make(-120)
        api.snapshot=lambda:availability(opened=[] if clock.value<Plan().release.timestamp() else [11,12])
        armed=[];trial.before_attempt=lambda:armed.append(clock.now())
        self.assertIsNotNone(trial.run())
        self.assertEqual(len(armed),1)
        self.assertLess(armed[0],Plan().release.timestamp())
        self.assertEqual(self.writes(api),['creat_book_info','creat_order'])

    def test_preflight_transient_network_failure_recovers_inside_same_trial(self):
        trial,api,_,events=self.make(-120)
        counter=[0]
        def fail_once():
            counter[0]+=1
            return NetworkError('OFFLINE_ONLY') if counter[0]==1 else None
        original=api.orders
        def orders(invited=False,timeout=5):
            error=fail_once()
            if error:raise error
            return original(invited,timeout)
        api.orders=orders
        self.assertIsNotNone(trial.run())
        self.assertEqual(sum(e['state']=='RECOVERING' for e in events),1)
        self.assertEqual(self.writes(api),['creat_book_info','creat_order'])

    def test_preflight_persistent_failure_is_bounded_and_authentication_not_retried(self):
        for error,retries in ((NetworkError,3),(AuthenticationError,1)):
            with self.subTest(error=error.__name__):
                trial,api,_,_=self.make()
                api.order_error=lambda:error('OFFLINE_ONLY')
                self.assertIsNone(trial.run())
                self.assertEqual(len(api.order_reads),retries)
                self.assertEqual(self.writes(api),[])

    def test_empty_inventory_refresh_can_recover_a_readonly_service_error(self):
        trial,api,_,events=self.make()
        count=[0]
        def snapshot():
            count[0]+=1
            if count[0]==1:raise ReadServiceError(503)
            return availability()
        api.snapshot=snapshot
        self.assertIsNotNone(trial.attempt(META,[],prechecked=True))
        self.assertEqual(count[0],2)
        self.assertEqual(self.writes(api),['creat_book_info','creat_order'])
        self.assertTrue(any(e['state']=='RECOVERING' for e in events))

    def test_self_book_500_only_diagnoses_and_never_retries_validation(self):
        trial,api,_,events=self.make()
        api.validation=lambda payload:Reply(500,{'message':"local variable 'self_book' referenced before assignment"})
        with self.assertRaises(BookingError):trial.attempt(META,[(1,0),(2,0)])
        self.assertEqual(self.writes(api),['creat_book_info'])
        diagnostics=[e for e in events if e['state']=='DIAGNOSING']
        self.assertEqual(diagnostics[-1]['issue'],'uninitialized_self_book')
        self.assertIs(diagnostics[-1]['own_conflict'],False)
        self.assertGreater(diagnostics[-1]['available_candidates'],0)

    def test_ambiguous_validation_transport_does_not_unblock_on_empty_orders(self):
        trial,api,_,_=self.make()
        def timeout(payload):raise NetworkError('OFFLINE_ONLY')
        api.validation=timeout
        with self.assertRaises(Uncertain):trial.attempt(META,[(1,0),(2,0)])
        self.assertEqual(self.writes(api),['creat_book_info'])

    def test_invalid_conflict_shapes_never_poison_candidates_or_create(self):
        for conflict,available in (([True],[12]),([999],[]),(['11'],[12]),([11],[11]),([],['11'])):
            with self.subTest(conflict=conflict,available=available):
                trial,api,_,_=self.make()
                api.validation=lambda payload:Reply(200,{'conflict_times':conflict,'available_times':available})
                with self.assertRaises(BookingError):trial.attempt(META,[(1,0),(2,0)])
                self.assertEqual(self.writes(api),['creat_book_info'])

    def test_business_rejection_does_not_turn_into_retry_storm(self):
        trial,api,_,_=self.make()
        api.validation=lambda payload:Reply(201,{'message':'OFFLINE_REJECTED'})
        with self.assertRaises(BookingError):trial.attempt(META,[(1,0),(2,0)])
        self.assertEqual(self.writes(api),['creat_book_info'])

    def test_prepared_payload_reused_and_bound_to_identity_and_time_table(self):
        trial,api,_,_=self.make()
        meta,options=trial.preflight()
        original=trial.prepared_payload(meta,options[0])
        with patch('booking._payload_for',side_effect=AssertionError('Must be prepared before release')):
            self.assertIsNotNone(trial.attempt(meta,options,prechecked=True))
        self.assertIs(trial.prepared_payload(meta,options[0]),original)
        api.session={**api.session,'openid':'OTHER_OFFLINE_ACCOUNT'}
        with self.assertRaises(AuthenticationError):trial.prepared_payload(meta,options[0])

    def test_mutated_time_table_or_out_of_scope_option_is_rejected_before_writes(self):
        trial,api,_,_=self.make()
        meta,options=trial.preflight();meta['time_list'][11]='19:01'
        with self.assertRaises(BookingError):trial.attempt(meta,options,prechecked=True)
        self.assertEqual(self.writes(api),[])
        trial,api,_,_=self.make()
        option=Option(1,0,replace(Plan(),day='2026-11-06'))
        with self.assertRaises(BookingError):trial.attempt(META,[option],prechecked=True)
        self.assertEqual(self.writes(api),[])

    def test_transport_timeouts_shrink_to_remaining_window(self):
        trial,api,_,_=self.make(19.8)
        self.assertIsNotNone(trial.attempt(META,[(1,0)]))
        for endpoint,_,timeout in api.calls:
            if endpoint.startswith('creat_'):self.assertLessEqual(timeout,.201)

    def test_expiry_after_validation_keeps_create_marker_unwritten(self):
        trial,api,clock,_=self.make()
        mark=Mock();trial.before_create=mark
        def late(payload):
            clock.value=trial.deadline+.001
            return Reply(200,{'conflict_times':[],'available_times':[11,12]})
        api.validation=late
        with self.assertRaises(BookingError):trial.attempt(META,[(1,0)])
        mark.assert_not_called();self.assertEqual(self.writes(api),['creat_book_info'])

    def test_order_verification_requires_sport_and_free_price_not_just_a_matching_time(self):
        plan=Plan()
        row={'order_num':'OFFLINE_ONLY','order_date':plan.day,'venue_name':plan.venue,
             'venue_type':'badminton','total_price':'0.00','site_id':'1',
             'start_time':plan.start,'end_time':plan.end}
        self.assertTrue(exact_order(row,plan,1))
        for fields in ({'total_price':1},{'total_price':float('nan')},{'venue_type':'tennis'},
                       {'total_price':None},{'order_num':True},{'venue_type':None}):
            with self.subTest(fields=fields):self.assertFalse(exact_order({**row,**fields},plan,1))


class ClockQualityTests(unittest.TestCase):
    def test_invalid_server_time_cannot_arm_a_request(self):
        for value in (float('nan'),float('inf'),True,-1):
            with self.subTest(value=value):
                api=Mock();api.call.return_value=Reply(200,{'data':value})
                with self.assertRaises(BookingError):ServerClock().sync(api)

    def test_slow_final_sample_cannot_replace_a_recent_low_rtt_anchor(self):
        api=Mock();clock=ServerClock()
        api.call.side_effect=[Reply(200,{'data':1793707200000}),Reply(200,{'data':1793707201000})]
        with patch('booking.time.monotonic',side_effect=[0,.01,1,1.2,1.21]):
            clock.sync(api);anchor=clock.anchor_mono
            clock.sync(api)
            self.assertFalse(clock.last_sample_accepted)
            self.assertEqual(clock.anchor_mono,anchor)
            self.assertAlmostEqual(clock.now(),1793707201.2,places=5)

    def test_ui_polling_accelerates_only_near_release_or_active_requests(self):
        # Load the pure timing function, never the GUI entry point.
        source=Path(__file__).resolve().parents[1]/'app.pyw'
        node=next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
                  if isinstance(n,ast.FunctionDef) and n.name=='refresh_interval')
        scope={};exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),scope)
        interval=scope['refresh_interval']
        self.assertEqual(interval('READY',60),500)
        self.assertEqual(interval('READY',2),50)
        self.assertEqual(interval('ATTEMPTING',-5),50)
        self.assertEqual(interval('CREATED_PENDING_INVITE',-20),500)


if __name__=='__main__':unittest.main()
