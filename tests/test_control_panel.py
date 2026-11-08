"""Real Tk widgets and snapshots; no network, fake workers/scheduler."""
import json
import hashlib
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from datetime import datetime,timedelta
from booking import TZ
from unittest.mock import Mock,patch
import control_panel
from plan_config import default_config,load_config
from selection_plan import SelectedSlot,SelectionPlan
from test_booking import META,availability
class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.private=Path(self.temp.name)
        self.patches=[patch.object(control_panel,'PRIVATE',self.private),patch.object(control_panel,'LAST',self.private/'last-plan.json'),
                      patch.object(control_panel.messagebox,'showerror')]
        for p in self.patches:p.start()
        self.root=tk.Tk();self.root.withdraw();self.data=default_config()
        self.panel=control_panel.Panel(self.root,self.data,auto_read=False,sync_history=False)
        stock=availability();stock['site_situation'].append({**stock['site_situation'][0],'site_id':2})
        self.panel.accept_snapshot({'day':self.data['day'],'venue':self.data['venue'],'venue_type':'badminton',
                                   'meta':META,'stock':stock,'own':[],'invited':[],
                                   'identity_hash':hashlib.sha256(b'TEST_ONLY').hexdigest()})
        self.root.update_idletasks()
    def tearDown(self):
        self.root.destroy()
        for p in self.patches:p.stop()
        self.temp.cleanup()
    def select_two(self):
        self.panel.grid.choose(SelectedSlot('15:00','16:00',1))
        self.panel.grid.choose(SelectedSlot('16:00','17:00',2))
    def test_grid_selection_save_and_order_count(self):
        p=self.panel;self.select_two()
        self.assertIn('预计 2 笔订单',p.preview.cget('text'))
        with patch.object(control_panel,'register_task',return_value={'task_name':'TEST_ONLY','start_at':'2030-01-01T19:00:00+08:00'}):
            p.schedule_plan()
        data,plan=load_config(self.private/'last-plan.json')
        self.assertIsInstance(plan,SelectionPlan)
        self.assertEqual(len(plan.selections),2);self.assertEqual(len(plan.groups()),2)
        self.assertEqual(len(p.history.records()),1)
        self.assertFalse(hasattr(p,'save_button'))
        self.assertNotIn('start',p.vars);self.assertNotIn('sport',p.vars)
        self.assertEqual(len(p.day_buttons),7)
    def test_button_keeps_exact_selection_and_stop_never_cancels(self):
        p=self.panel;self.select_two()
        child=Mock(pid=87654321);child.poll.return_value=None
        with patch.object(control_panel.subprocess,'Popen',return_value=child) as launch:
            p.begin();command=launch.call_args.args[0]
            self.assertIn(str(control_panel.ROOT/'app.pyw'),command);self.assertIn('--hidden',command)
            chosen=load_config(command[command.index('--config')+1])[1]
            self.assertEqual(chosen,p.active_plan)
            self.assertEqual(str(p.start.cget('state')),'disabled')
            p.request_stop();self.assertEqual(json.loads(p.paths['stop'].read_text())['pid'],child.pid)
            p.begin();launch.assert_called_once();self.assertFalse(p.paths['submitted'].exists())
    def test_scheduler_uses_independent_same_selection_snapshot(self):
        p=self.panel;self.select_two()
        with patch.object(control_panel,'register_task',return_value={'task_name':'TEST_ONLY','start_at':'2030-01-01T19:00:00+08:00'}) as register:
            expected=p.data()[1];p.schedule_plan();path,digest=register.call_args.args
            self.assertEqual(load_config(path,digest)[1],expected);self.assertIsNone(p.process)
    def test_same_period_overwrite_decline_and_confirm(self):
        p=self.panel
        with patch.object(control_panel,'register_task',return_value={'task_name':'TEST_ONLY','start_at':'2030-01-01T19:00:00+08:00'}) as register:
            p.grid.choose(SelectedSlot('16:00','17:00',1));p.schedule_plan()
            p.grid.choose(SelectedSlot('16:00','17:00',2))
            with patch.object(control_panel.messagebox,'askyesno',return_value=False):p.schedule_plan()
            self.assertEqual(register.call_count,1)
            with patch.object(control_panel.messagebox,'askyesno',return_value=True):p.schedule_plan()
            self.assertEqual(register.call_count,2)
            current=next(r for r in p.history.records() if r['status']=='scheduled')
            self.assertEqual(current['data']['selections'][0]['site_id'],2)
    def test_history_change_site_and_delete_only_matching_pending_task(self):
        p=self.panel
        with patch.object(control_panel,'register_task',return_value={'task_name':'TEST_ONLY','start_at':'2030-01-01T19:00:00+08:00'}):
            p.grid.choose(SelectedSlot('16:00','17:00',1));p.schedule_plan()
            first=next(r for r in p.history.records() if r['status']=='scheduled')
            p.change_history_sites(first['id'],[2])
            current=next(r for r in p.history.records() if r['status']=='scheduled')
            self.assertEqual([(s['start'],s['end'],s['site_id']) for s in current['data']['selections']],[('16:00','17:00',2)])
        with patch.object(control_panel,'disable_booking_task') as disable:
            p.delete_history_plan(first['id']);disable.assert_not_called()
            p.delete_history_plan(current['id']);disable.assert_called_once_with('TEST_ONLY',current['config_sha256'])
        self.assertEqual(p.current_model().state(SelectedSlot('16:00','17:00',1)),'available')
    def test_legacy_scheduled_config_keeps_any_court(self):
        p=self.panel;child=Mock(pid=87654322);child.poll.return_value=None
        with patch.object(control_panel.subprocess,'Popen',return_value=child) as launch:
            p.begin_configured();args=launch.call_args.args[0]
            data,plan=load_config(args[args.index('--config')+1])
            self.assertEqual(data['schema'],1);self.assertEqual(plan.site_ids,())
            self.assertEqual((plan.start,plan.end),('15:00','17:00'))
    def test_closed_cell_does_not_become_selected(self):
        p=self.panel;p.grid.choose(SelectedSlot('08:00','09:00',1))
        self.assertEqual(len(p.current_model().selected),0);self.assertIn('未开放',p.hint.cget('text'))
    def test_date_switch_keeps_days_separate(self):
        p=self.panel;self.select_two()
        p.change_day(next(d for d in p.days if d!=self.data['day']))
        self.assertIsNone(p.current_model())
        p.change_day(self.data['day']);self.assertEqual(len(p.current_model().selected),2)
    def test_malformed_snapshot_does_not_stop_poll(self):
        p=self.panel
        p.messages.put(('snapshot',p.generation,{'day':self.data['day']}))
        with patch.object(self.root,'after') as after:
            p.poll()
            after.assert_called_with(250,p.poll)
        self.assertIn('不完整排期',p.hint.cget('text'))
    def test_calendar_rolls_forward_without_network(self):
        p=self.panel;tomorrow=datetime.now(TZ)+timedelta(days=1)
        self.assertTrue(p.refresh_calendar(tomorrow))
        self.assertEqual(p.days[0],control_panel.bookable_days(tomorrow)[0])
        self.assertEqual(len(p.day_buttons),7)
    def test_account_switch_clears_prior_selection(self):
        p=self.panel;self.select_two()
        original=p.snapshots[(self.data['venue'],self.data['day'])]
        p.accept_snapshot({**original,'identity_hash':hashlib.sha256(b'NEW_ACCOUNT').hexdigest()})
        self.assertFalse(p.current_model().selected)
    def test_running_plan_does_not_reenable_schedule_on_late_snapshot(self):
        p=self.panel;self.select_two()
        child=Mock(pid=87654323);child.poll.return_value=None
        with patch.object(control_panel.subprocess,'Popen',return_value=child):
            p.begin()
            p.accept_snapshot(p.snapshots[(self.data['venue'],self.data['day'])])
            self.assertEqual(str(p.schedule.cget('state')),'disabled')
if __name__=='__main__':unittest.main()
