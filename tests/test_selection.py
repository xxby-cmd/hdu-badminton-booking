from datetime import datetime,timedelta
from pathlib import Path
import tempfile
import unittest
import hashlib
from booking import BookingError,Reply,NetworkError,TZ
from plan_config import save_config,load_config
from selection_plan import SelectedSlot,GridSelection,config_for_slots,validate_cells,parse_selection,ensure_executable,validate_quota
from selection_runner import SelectionRunner
from test_booking import META,SESSION,Clock,availability
DAY='2030-11-10'
def stock():
    source=availability();source['site_situation'].append({**source['site_situation'][0],'site_id':2});return source
def one(start='15:00',end='16:00',site=1):return SelectedSlot(start,end,site)
def plan(slots,now=None,immediate=False):
    return parse_selection(config_for_slots(DAY,'综合馆羽毛球','badminton',slots,now,immediate))
def owned(start='14:00',end='15:00',venue='综合馆羽毛球'):
    return {'order_date':DAY,'venue_name':venue,'venue_type':'badminton','start_time':start,'end_time':end,
            'site_id':1,'order_num':'MOCK_OWNED','total_price':0}
class SelectionRulesTests(unittest.TestCase):
    def test_adjacent_merge_and_different_sites_or_gaps_split(self):
        combined=plan([one(),one('16:00','17:00')])
        self.assertEqual(len(combined.groups()),1)
        self.assertEqual((combined.groups()[0].start,combined.groups()[0].end,combined.groups()[0].site_ids),('15:00','17:00',(1,)))
        self.assertEqual(len(plan([one(),one('16:00','17:00',2)]).groups()),2)
        self.assertEqual(len(plan([one('13:20','14:00'),one('16:00','17:00')]).groups()),2)
    def test_two_slots_can_be_irregular_duration(self):
        selected=plan([one('11:00','11:40'),one('11:40','13:20')])
        self.assertEqual(len(selected.groups()),1);self.assertEqual(selected.groups()[0].end,'13:20')
    def test_overlap_duplicate_and_third_cell_rejected(self):
        for slots in ([one(),one(site=2)],[one(),one()],[one('14:00','15:00'),one(),one('16:00','17:00')]):
            with self.assertRaises(BookingError):plan(slots)
    def test_own_quota_and_invited_overlap(self):
        with self.assertRaises(BookingError):validate_quota(plan([one(),one('16:00','17:00',2)]),[owned()],[],META)
        validate_quota(plan([one()]),[owned()],[],META)
        with self.assertRaises(BookingError):validate_quota(plan([one()]),[],[owned('15:00','16:00','穹顶网球场')],META)
    def test_unknown_existing_period_stops(self):
        with self.assertRaises(BookingError):validate_quota(plan([one()]),[owned('06:17','07:21')],[],META)
    def test_closed_booked_paid_past_blocked(self):
        p=plan([one()]);now=datetime(2030,11,7,tzinfo=TZ)
        for modify in ('closed','booked','paid','past'):
            av=stock();meta=dict(META);checked=now
            if modify=='closed':av['site_situation'][0]['oci_time_list']=[7,11,12]
            if modify=='booked':av['site_situation'][0]['book_time_list']=[7]
            if modify=='paid':av['extra_list']={'extra_price_time':[7],'extra_price_list':[5]}
            if modify=='past':checked=datetime(2030,11,10,15,1,tzinfo=TZ)
            with self.subTest(modify=modify),self.assertRaises(BookingError):validate_cells(p,meta,av,[],[],checked)
    def test_blue_can_be_unselected_at_limit(self):
        m=GridSelection(DAY,'综合馆羽毛球','badminton',META,stock(),now=datetime(2030,11,7,tzinfo=TZ))
        m.toggle(one());m.toggle(one('16:00','17:00',2))
        with self.assertRaises(BookingError):m.toggle(one('14:00','15:00',2))
        m.toggle(one());self.assertEqual(len(m.selected),1);self.assertEqual(m.state(one()),'available')
    def test_exact_selection_roundtrip(self):
        p=plan([one(),one('16:00','17:00',2)])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'selected.json';digest=save_config(path,config_for_slots(p.day,p.venue,p.venue_type,p.selections))
            self.assertEqual(load_config(path,digest)[1],p)
        self.assertEqual(p.groups()[1].site_ids,(2,))
    def test_immediate_expiry_and_no_scheduling_immediate(self):
        now=datetime(2030,11,9,20,tzinfo=TZ);p=plan([one()],now,True);ensure_executable(p,now)
        with self.assertRaises(BookingError):ensure_executable(p,now+timedelta(seconds=26))
        with self.assertRaises(BookingError):ensure_executable(p,now,scheduled=True)
        with self.assertRaises(BookingError):ensure_executable(plan([one()]),now)
    def test_grid_ages_slots_without_new_query(self):
        m=GridSelection(DAY,'综合馆羽毛球','badminton',META,stock(),now=datetime(2030,11,10,14,59,tzinfo=TZ))
        current=[datetime(2030,11,10,14,59,tzinfo=TZ)]
        m.now_provider=lambda:current[0]
        m.toggle(one());current[0]=datetime(2030,11,10,15,tzinfo=TZ)
        self.assertEqual(m.state(one()),'时段已开始')
        self.assertTrue(m.expire());self.assertFalse(m.selected)
class TestApi:
    def __init__(self,fail_check=0,fail_create=0):
        self.session=SESSION.copy();self.rows=[];self.calls=[];self.checks=0;self.creates=0
        self.fail_check,self.fail_create=fail_check,fail_create
    def orders(self,invited=False,timeout=5):
        self.calls.append(('orders',invited));return [] if invited else list(self.rows)
    def call(self,endpoint,payload=None,timeout=5):
        self.calls.append((endpoint,payload))
        if endpoint=='post_venue_info':return Reply(200,META)
        if endpoint=='post_site_situation':return Reply(200,stock())
        if endpoint=='creat_book_info':
            self.checks+=1
            if self.checks==self.fail_check:return Reply(500,{'message':'MOCK_500'})
            return Reply(200,{'conflict_times':[],'available_times':payload['orderData']['time_list']})
        if endpoint=='creat_order':
            self.creates+=1;row=payload['orderData'];num='MOCK_ONLY_'+str(self.creates)
            self.rows.append({**row,'order_num':num,'order_date':row['date']})
            if self.creates==self.fail_create:raise NetworkError('MOCK_REPLY_LOST')
            return Reply(200,{'data':{'order_num':num}})
        raise AssertionError(endpoint)
class VirtualStop:
    def __init__(self,clock):self.clock=clock;self.stopped=False
    def is_set(self):return self.stopped
    def wait(self,seconds):self.clock.value+=seconds;return self.stopped
class SelectionExecutionTests(unittest.TestCase):
    def run_plan(self,slots,api=None,private=None,immediate=False):
        now=datetime(2030,11,9,20,tzinfo=TZ) if immediate else datetime(2030,11,8,19,59,tzinfo=TZ)
        p=plan(slots,now,immediate);api=api or TestApi()
        clock=Clock(now.timestamp());stop=VirtualStop(clock);events=[]
        runner=SelectionRunner(api,p,private,publish=lambda **e:events.append(e),clock=clock,stop=stop)
        return runner.run(),runner,api,events
    def test_adjacent_same_court_one_order(self):
        with tempfile.TemporaryDirectory() as private:
            result,runner,api,events=self.run_plan([one(),one('16:00','17:00')],private=private)
            self.assertEqual(len(result),1);self.assertEqual(api.creates,1)
            self.assertEqual((result[0]['start_time'],result[0]['end_time']),('15:00','17:00'))
            self.assertTrue((Path(private)/'submitted-20301108.json').exists())
    def test_different_courts_two_exact_orders_serially(self):
        with tempfile.TemporaryDirectory() as private:
            result,runner,api,events=self.run_plan([one(),one('16:00','17:00',2)],private=private)
            self.assertEqual(len(result),2);self.assertEqual(api.creates,2)
            self.assertEqual([(r['site_id'],r['start_time'],r['end_time']) for r in result],[(1,'15:00','16:00'),(2,'16:00','17:00')])
            self.assertEqual([e for e,p in api.calls if e.startswith('creat')],['creat_book_info','creat_order','creat_book_info','creat_order'])
            self.assertEqual(events[-1]['state'],'CREATED_PENDING_INVITE');self.assertFalse(any('cancel' in e for e,p in api.calls))
    def test_second_check_error_keeps_first_no_cancellation(self):
        with tempfile.TemporaryDirectory() as private:
            result,runner,api,events=self.run_plan([one(),one('16:00','17:00',2)],TestApi(fail_check=2),private)
            self.assertEqual(len(result),1);self.assertEqual(api.creates,1);self.assertEqual(events[-1]['state'],'PARTIAL_SUCCESS')
    def test_lost_reply_verified_without_resend(self):
        with tempfile.TemporaryDirectory() as private:
            result,runner,api,events=self.run_plan([one()],TestApi(fail_create=1),private)
            self.assertEqual(len(result),1);self.assertEqual(api.creates,1)
    def test_restart_cannot_create_more(self):
        with tempfile.TemporaryDirectory() as private:
            self.run_plan([one()],private=private)
            result,runner,api,events=self.run_plan([one('16:00','17:00',2)],TestApi(),private)
            self.assertEqual(api.creates,0);self.assertEqual(api.checks,0)
    def test_immediate_keeps_true_release(self):
        with tempfile.TemporaryDirectory() as private:
            result,runner,api,events=self.run_plan([one()],private=private,immediate=True)
            self.assertEqual(len(result),1);self.assertEqual(runner.plan.release,datetime(2030,11,8,20,tzinfo=TZ))
    def test_changed_account_before_validation_never_writes(self):
        with tempfile.TemporaryDirectory() as private:
            now=datetime(2030,11,8,19,59,tzinfo=TZ)
            data=config_for_slots(DAY,'综合馆羽毛球','badminton',[one()],now,
                                  account_hash=hashlib.sha256(b'ANOTHER_ACCOUNT').hexdigest())
            api=TestApi();clock=Clock(now.timestamp());events=[]
            runner=SelectionRunner(api,parse_selection(data),private,publish=lambda **e:events.append(e),
                                   clock=clock,stop=VirtualStop(clock))
            self.assertFalse(runner.run());self.assertEqual(api.creates,0);self.assertEqual(api.checks,0)
            self.assertFalse((Path(private)/'validation-20301108.json').exists())
    def test_unlisted_uncertain_create_never_moves_to_second_order(self):
        class Unlisted(TestApi):
            def orders(self,invited=False,timeout=5):return []
        with tempfile.TemporaryDirectory() as private:
            result,runner,api,events=self.run_plan([one(),one('16:00','17:00',2)],Unlisted(fail_create=1),private)
            self.assertFalse(result);self.assertEqual(api.creates,1);self.assertEqual(events[-1]['state'],'UNCERTAIN')
    def test_unknown_second_creation_preserves_first_and_stops(self):
        class SecondUnlisted(TestApi):
            def orders(self,invited=False,timeout=5):return [] if invited else self.rows[:1]
        with tempfile.TemporaryDirectory() as private:
            result,runner,api,events=self.run_plan([one(),one('16:00','17:00',2)],SecondUnlisted(fail_create=2),private)
            self.assertEqual(len(result),1);self.assertEqual(api.creates,2)
            self.assertEqual(events[-1]['state'],'PARTIAL_UNCERTAIN')
if __name__=='__main__':unittest.main()
