import copy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from booking import BookingError, CandidatePlanner, Trial, Reply, TZ, exact_order, payload_for, mark_submission
from plan_config import default_config, parse_config, save_config, load_config, ensure_future, paths_for
from system_schedule import task_xml, NS, register_task
from test_booking import Clock, RoutedApi, SESSION, META, availability

def request():
    return {**default_config(datetime(2026,11,7,tzinfo=TZ)), 'day':'2026-11-10'}

def target(site=1):
    return {'order_num':'test-order','order_date':'2026-11-10','venue_name':'综合馆羽毛球',
            'venue_type':'badminton','site_id':site,'start_time':'15:00','end_time':'17:00','total_price':0}

class ConfigTests(unittest.TestCase):
    def test_nov10_exact_full_interval_and_release(self):
        plan=parse_config(request())
        self.assertEqual(plan.release,datetime(2026,11,8,20,tzinfo=TZ))
        self.assertEqual((plan.start,plan.end,plan.site_ids,plan.max_price,plan.allow_one_hour),
                         ('15:00','17:00',(),0,False))
        planner=CandidatePlanner(META,plan)
        self.assertEqual(planner.wanted,{('15:00','17:00'):(7,8)})
        self.assertEqual(len(planner.options(availability())),1)
        self.assertEqual(payload_for(SESSION,META,plan,1,0)['orderData']['time_list'],[7,8])

    def test_disallows_invalid_time_price_and_scope(self):
        for override in ({'day':'2026-11-31'},{'day':'20261110'},{'start':'17:00'},
                         {'end':'18:00'},{'start':'9:00'},{'start':'-1:00'},
                         {'max_price':1},{'max_price':True},{'allow_one_hour':True},
                         {'site_ids':[True]},{'site_ids':[1,1]},{'site_ids':[0]},
                         {'venue':''},{'venue_type':'basketball'},{'schema':True}):
            with self.subTest(override=override),self.assertRaises(BookingError):
                parse_config({**request(),**override})

    def test_specific_sites_cannot_leak_to_another_court(self):
        plan=parse_config({**request(),'site_ids':[2]})
        av=availability()
        av['site_situation'] += [{**av['site_situation'][0],'site_id':2}]
        self.assertEqual([o.site for o in CandidatePlanner(META,plan).options(av)],[2])
        with self.assertRaises(BookingError):payload_for(SESSION,META,plan,1,0)
        self.assertFalse(exact_order(target(1),plan))
        self.assertTrue(exact_order(target(2),plan))

    def test_no_one_hour_or_another_day_is_a_success(self):
        plan=parse_config(request())
        self.assertFalse(exact_order({**target(),'end_time':'16:00'},plan))
        self.assertFalse(exact_order({**target(),'order_date':'2026-11-09'},plan))
        av=availability(booked=[8])
        self.assertEqual(CandidatePlanner(META,plan).options(av),[])

    def test_snapshot_roundtrip_and_changed_plan_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'计划 with space.json'
            digest=save_config(path,request())
            self.assertEqual(load_config(path,digest)[1],parse_config(request()))
            save_config(path,{**request(),'site_ids':[2]})
            with self.assertRaises(BookingError):load_config(path,digest)

    def test_future_and_same_day_durable_protection(self):
        plan=parse_config(request())
        ensure_future(plan,datetime(2026,11,7,tzinfo=TZ))
        with self.assertRaises(BookingError):ensure_future(plan,plan.release)
        left=paths_for(plan,Path('example'))
        right=paths_for(replace(plan,start='13:20',end='14:00'),Path('example'))
        self.assertEqual(left['validation'],right['validation'])
        self.assertEqual(left['submitted'].name,'submitted-20261108.json')

class November10Replay(unittest.TestCase):
    def run_scenario(self,check=None,booked=None):
        plan=parse_config(request())
        api=RoutedApi(order_reads=[[],[],[],[],[target()]])
        api.check=check or Reply(200,{'conflict_times':[],'available_times':[7,8]})
        if booked is not None:
            orig=api.call
            def call(endpoint,payload=None,timeout=5):
                if endpoint=='post_site_situation':
                    api.calls.append((endpoint,payload));return Reply(200,availability(booked=booked))
                return orig(endpoint,payload,timeout)
            api.call=call
        clock=Clock(plan.release.timestamp()-3600)
        events=[]
        with tempfile.TemporaryDirectory() as directory:
            mark=Path(directory)/'submitted.json'
            trial=Trial(api,plan,publish=lambda **e:events.append(e),clock=clock,
                        shuffle=lambda values:None,before_create=lambda p,s:mark_submission(mark,p,s))
            trial.wait_until=lambda when:setattr(clock,'value',when)
            result=trial.run()
            has_marker=mark.exists()
        return result,api,events,has_marker

    def test_exact_nov10_order_confirmed_only_once(self):
        result,api,events,marker=self.run_scenario()
        self.assertEqual(result,target())
        self.assertTrue(marker)
        checks=[p for e,p in api.calls if e=='creat_book_info']
        creates=[p for e,p in api.calls if e=='creat_order']
        self.assertEqual(len(checks),1);self.assertEqual(creates,checks)
        self.assertEqual(checks[0]['orderData']['time_list'],[7,8])
        self.assertEqual(events[-1]['state'],'CREATED_PENDING_INVITE')
        self.assertFalse(any('cancel' in e for e,p in api.calls))

    def test_500_stops_without_creation(self):
        result,api,events,marker=self.run_scenario(Reply(500,{'message':'test-only server error'}))
        self.assertIsNone(result);self.assertFalse(marker)
        self.assertEqual(sum(e=='creat_book_info' for e,p in api.calls),1)
        self.assertFalse(any(e=='creat_order' for e,p in api.calls))

    def test_no_whole_interval_never_submits_one_hour(self):
        result,api,events,marker=self.run_scenario(booked=[8])
        self.assertIsNone(result);self.assertFalse(marker)
        self.assertFalse(any(e.startswith('creat') for e,p in api.calls))

class SchedulingTests(unittest.TestCase):
    def test_once_only_time_and_escaped_paths_and_original_fingerprint(self):
        plan=parse_config(request())
        xml=task_xml(plan,Path('D:/路径 & 空格/计划.json'),'a'*64,'S-1-5-21-123',
                     datetime(2026,11,7,tzinfo=TZ))
        root=ET.fromstring(xml);ns={'t':NS}
        self.assertEqual(root.findtext('.//t:StartBoundary',namespaces=ns),'2026-11-08T19:00:00+08:00')
        self.assertEqual(len(root.findall('.//t:TimeTrigger',ns)),1)
        self.assertEqual(root.findall('.//t:CalendarTrigger',ns),[])
        self.assertIn('--start',root.findtext('.//t:Arguments',namespaces=ns))
        self.assertIn('startup.py',root.findtext('.//t:Arguments',namespaces=ns))
        self.assertIn('a'*64,root.findtext('.//t:Arguments',namespaces=ns))
        self.assertEqual(root.findtext('.//t:LogonType',namespaces=ns),'InteractiveToken')
        self.assertEqual(root.findtext('.//t:MultipleInstancesPolicy',namespaces=ns),'IgnoreNew')

    def test_past_schedule_is_not_registered(self):
        with self.assertRaises(BookingError):
            task_xml(parse_config(request()),Path('x'),'a'*64,'SID',datetime(2026,11,8,19,1,tzinfo=TZ))

class DingTalkDirectoryTests(unittest.TestCase):
    def test_unique_new_dingtalk_directory_is_used_without_guessing_multiple(self):
        import session
        with tempfile.TemporaryDirectory() as directory:
            local=Path(directory)
            preferred=local/'DingTalk_133/Default/Local Storage/leveldb'
            first=local/'DingTalk_140/Default/Local Storage/leveldb'
            first.mkdir(parents=True)
            with patch.object(session,'LOCAL_DATA',local),patch.object(session,'DB',preferred), \
                 patch.object(session,'read_origin',return_value={}) as read:
                session.origin_records()
                read.assert_called_once_with(first,session.ORIGIN)
                (local/'DingTalk_141/Default/Local Storage/leveldb').mkdir(parents=True)
                with self.assertRaises(session.SessionError):session.origin_records()
                self.assertEqual(read.call_count,1)

if __name__=='__main__':unittest.main()
