from datetime import datetime
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from booking import TZ,BookingError
from plan_config import bookable_days,save_config,load_config
from selection_plan import SelectedSlot,config_for_slots,GridSelection
from plan_history import PlanHistory,ReplaceRequired
from system_schedule import TaskRegistrationUncertain
from test_booking import META,availability

def selected(site=1,start='16:00',end='17:00'):
    return config_for_slots('2030-11-10','综合馆羽毛球','badminton',[SelectedSlot(start,end,site)])

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.history=PlanHistory(self.temp.name);self.calls=[]
    def tearDown(self):self.temp.cleanup()
    def activate(self,data,mode,record_id):
        self.calls.append(copy.deepcopy(data))
        path=Path(self.temp.name)/('config-'+str(len(self.calls))+'.json')
        digest=save_config(path,data)
        return {'task_name':'TEST_ONLY','config':str(path),'config_sha256':digest,'pid':None}
    def test_different_court_same_time_requires_confirmation_before_side_effect(self):
        original,_=self.history.apply(selected(1),META,'schedule',self.activate)
        with self.assertRaises(ReplaceRequired):
            self.history.apply(selected(9),META,'schedule',self.activate)
        self.assertEqual(len(self.calls),1);self.assertEqual(len(self.history.records()),1)
        changed,_=self.history.apply(selected(9),META,'schedule',self.activate,replace=True)
        self.assertNotEqual(changed['id'],original['id'])
        self.assertEqual(self.history.find(original['id'])['status'],'superseded')
        self.assertEqual(changed['data']['selections'][0]['site_id'],9)
        self.assertEqual(sum(r['status']=='scheduled' for r in self.history.records()),1)
    def test_delete_releases_and_reselection_creates_new_history_entry(self):
        row,_=self.history.apply(selected(),META,'schedule',self.activate)
        canceled=[]
        self.history.delete_local(row['id'],lambda r:canceled.append(r['task_name']))
        self.assertEqual(canceled,['TEST_ONLY']);self.assertEqual(self.history.records(),[])
        newer,_=self.history.apply(selected(7),META,'schedule',self.activate)
        self.assertNotEqual(row['id'],newer['id'])
        self.assertEqual(len(self.history.records()),1)
    def test_different_period_is_merged_not_overwriting_original(self):
        first,_=self.history.apply(selected(3),META,'schedule',self.activate)
        second,merged=self.history.apply(selected(8,'19:00','20:00'),META,'schedule',self.activate)
        self.assertTrue(merged);self.assertNotEqual(first['id'],second['id'])
        self.assertEqual([(s['site_id'],s['start']) for s in second['data']['selections']],[(3,'16:00'),(8,'19:00')])
    def test_third_period_rejected_before_register(self):
        self.history.apply(selected(),META,'schedule',self.activate)
        self.history.apply(selected(2,'19:00','20:00'),META,'schedule',self.activate)
        with self.assertRaises(BookingError):self.history.apply(selected(3,'20:00','21:00'),META,'schedule',self.activate)
        self.assertEqual(len(self.calls),2)
    def test_failed_system_update_restores_original_pending_plan(self):
        original,_=self.history.apply(selected(),META,'schedule',self.activate)
        def fail(*args):raise OSError('MOCK_FAILURE')
        with self.assertRaises(OSError):self.history.apply(selected(4),META,'schedule',fail,replace=True)
        self.assertEqual(self.history.find(original['id'])['data'],original['data'])
        self.assertEqual(self.history.find(original['id'])['status'],'scheduled')
    def test_two_windows_share_same_time_lock(self):
        self.history.apply(selected(),META,'schedule',self.activate)
        another=PlanHistory(self.temp.name)
        with self.assertRaises(ReplaceRequired):another.apply(selected(5),META,'schedule',self.activate)
    def test_delete_stale_same_content_revision_cannot_disable_current_task(self):
        old,_=self.history.apply(selected(1),META,'schedule',self.activate)
        newer,_=self.history.apply(selected(1),META,'schedule',self.activate,replace=True)
        calls=[];self.history.delete_local(old['id'],lambda row:calls.append(row))
        self.assertFalse(calls)
        self.assertEqual(self.history.find(newer['id'])['status'],'scheduled')
    def test_deletion_does_not_allow_old_auto_job_to_revive(self):
        row,_=self.history.apply(selected(),META,'schedule',self.activate)
        self.history.delete_local(row['id'],lambda r:None)
        with self.assertRaises(BookingError):
            self.history.attach_configured(row['data'],row['config'],self.activate)
        self.assertEqual(len(self.calls),1)
    def test_explicit_reregistration_after_delete_can_auto_launch_new_record(self):
        row,_=self.history.apply(selected(),META,'schedule',self.activate)
        self.history.delete_local(row['id'],lambda r:None)
        new,_=self.history.apply(selected(),META,'schedule',self.activate)
        launched=self.history.attach_configured(new['data'],new['config'],self.activate)
        self.assertEqual(launched['id'],new['id'])
    def test_running_or_uncertain_history_cannot_be_deleted(self):
        row,_=self.history.apply(selected(),META,'schedule',self.activate)
        self.history.update_run(row['id'],{'state':'UNCERTAIN','message':'MOCK_UNKNOWN','pid':123})
        with self.assertRaises(BookingError):self.history.delete_local(row['id'],lambda r:None)
    def test_legacy_any_court_remains_any_when_adding_other_period(self):
        legacy={'schema':1,'day':'2030-11-10','venue':'综合馆羽毛球','venue_type':'badminton',
                'start':'16:00','end':'17:00','site_ids':[],'max_price':0,'allow_one_hour':False}
        row,_=self.history.apply(legacy,META,'schedule',self.activate)
        merged,_=self.history.apply(selected(2,'19:00','20:00'),META,'schedule',self.activate)
        _,plan=load_config(merged['config'])
        self.assertEqual(plan.groups()[0].site_ids,())
        self.assertEqual(plan.groups()[1].site_ids,(2,))
    def test_pending_cell_is_replaceable_but_overquota_new_slot_not(self):
        stock=availability();stock['site_situation'].append({**stock['site_situation'][0],'site_id':2})
        self.history.apply(selected(),META,'schedule',self.activate)
        blocked,replaceable,reserved=self.history.blocks('2030-11-10','综合馆羽毛球',META)
        model=GridSelection('2030-11-10','综合馆羽毛球','badminton',META,stock,
                            now=datetime(2030,11,7,tzinfo=TZ),blocked=blocked,replaceable=replaceable,reserved_count=reserved)
        self.assertTrue(model.existing(SelectedSlot('16:00','17:00',2)))
        self.assertEqual(model.state(SelectedSlot('16:00','17:00',2)),'available')
    def test_replaced_dispatched_old_config_cannot_launch_or_submit(self):
        old,_=self.history.apply(selected(1),META,'schedule',self.activate)
        new,_=self.history.apply(selected(2),META,'schedule',self.activate,replace=True)
        with self.assertRaises(BookingError):self.history.attach_configured(old['data'],old['config'],self.activate)
        with self.assertRaises(BookingError):self.history.assert_active(old['id'],old['config_sha256'])
        with self.assertRaises(BookingError):self.history.assert_active(new['id'],old['config_sha256'])
        self.history.assert_active(new['id'],new['config_sha256'])
        self.assertEqual(len(self.calls),2)
    def test_unknown_auto_config_cannot_compete_with_pending_plan(self):
        self.history.apply(selected(1),META,'schedule',self.activate)
        path=Path(self.temp.name)/'old-unknown.json';save_config(path,selected(4))
        with self.assertRaises(BookingError):self.history.attach_configured(selected(4),path,self.activate)
    def test_system_side_effect_unknown_preserves_new_revision_until_reconciled(self):
        old,_=self.history.apply(selected(1),META,'schedule',self.activate)
        path=Path(self.temp.name)/'new.json';digest=save_config(path,selected(2))
        def partial(*args):raise TaskRegistrationUncertain(path,digest,'TEST_ONLY')
        with self.assertRaises(TaskRegistrationUncertain):self.history.apply(selected(2),META,'schedule',partial,replace=True)
        new=next(r for r in self.history.records() if r['status']=='uncertain')
        self.assertEqual(new['config_sha256'],digest)
        self.assertEqual(self.history.find(old['id'])['status'],'superseded')
        with self.assertRaises(BookingError):self.history.attach_configured(old['data'],old['config'],self.activate)
        self.history.import_task({'name':'TEST_ONLY','enabled':True,'config':str(path),'sha256':digest})
        self.assertEqual(self.history.find(new['id'])['status'],'scheduled')
    def test_same_content_uncertain_reconciles_latest_not_superseded(self):
        old,_=self.history.apply(selected(1),META,'schedule',self.activate)
        def partial(*args):raise TaskRegistrationUncertain(old['config'],old['config_sha256'],'TEST_ONLY')
        with self.assertRaises(TaskRegistrationUncertain):self.history.apply(selected(1),META,'schedule',partial,replace=True)
        new=next(r for r in self.history.records() if r['status']=='uncertain')
        self.history.import_task({'name':'TEST_ONLY','enabled':True,'config':old['config'],'sha256':old['config_sha256']})
        self.assertEqual(self.history.find(new['id'])['status'],'scheduled')
        self.assertEqual(self.history.find(old['id'])['status'],'superseded')
    def test_stale_confirmation_cannot_replace_newer_revision(self):
        old,_=self.history.apply(selected(1),META,'schedule',self.activate)
        current,_=self.history.apply(selected(2),META,'schedule',self.activate,replace=True)
        with self.assertRaises(BookingError):self.history.apply(selected(3),META,'schedule',self.activate,replace=True,expected_id=old['id'])
        self.assertEqual(self.history.find(current['id'])['status'],'scheduled')
        self.assertEqual(len(self.calls),2)
    def test_final_history_save_failure_does_not_restore_old_system_task(self):
        old,_=self.history.apply(selected(1),META,'schedule',self.activate)
        actual_save=self.history.save;count=0
        def fail_once(document):
            nonlocal count
            count+=1
            if count==2:raise OSError('MOCK_DISK_FAILURE_AFTER_ACTIVATION')
            actual_save(document)
        with patch.object(self.history,'save',side_effect=fail_once):
            with self.assertRaises(OSError):self.history.apply(selected(2),META,'schedule',self.activate,replace=True)
        self.assertEqual(self.history.find(old['id'])['status'],'superseded')
        new=next(r for r in self.history.records() if r['status']=='uncertain')
        self.assertTrue(new['config_sha256']);self.assertTrue(new['registration_uncertain'])
    def test_school_hide_does_not_delete_booking_or_local_protection(self):
        self.history.hide_school('MOCK_SCHOOL_ORDER')
        self.assertIn('MOCK_SCHOOL_ORDER',self.history.school_hidden())
        self.assertFalse(self.calls)

class DateWindowTests(unittest.TestCase):
    def test_before_20_starts_two_days_later(self):
        self.assertEqual(bookable_days(datetime(2026,11,8,19,59,59,tzinfo=TZ))[0],'2026-11-10')
    def test_at_20_moves_to_next_release_day(self):
        days=bookable_days(datetime(2026,11,8,20,tzinfo=TZ))
        self.assertEqual((days[0],days[-1],len(days)),('2026-11-11','2026-11-17',7))
        self.assertEqual(bookable_days(datetime(2026,11,9,20,tzinfo=TZ))[0],'2026-11-12')
if __name__=='__main__':unittest.main()
