"""Real Tk widgets and snapshots; no network, fake workers/scheduler."""
import json
import hashlib
import gc
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
                      patch.object(control_panel.messagebox,'showerror'),
                      patch.object(control_panel.messagebox,'showinfo'),
                      patch.object(control_panel.messagebox,'showwarning')]
        for p in self.patches:p.start()
        self.root=tk.Tk();self.root.withdraw();self.data=default_config()
        self.panel=control_panel.Panel(self.root,self.data,auto_read=False,sync_history=False,enable_tray=False)
        stock=availability();stock['site_situation'].append({**stock['site_situation'][0],'site_id':2})
        self.panel.accept_snapshot({'day':self.data['day'],'venue':self.data['venue'],'venue_type':'badminton',
                                   'meta':META,'stock':stock,'own':[],'invited':[],
                                   'identity_hash':hashlib.sha256(b'TEST_ONLY').hexdigest()})
        self.root.update_idletasks()
    def tearDown(self):
        self.root.destroy()
        self.panel=None;self.root=None
        # Tcl interpreters and callback cycles must be released on their owner thread
        # before the following tests create background logging workers.
        gc.collect()
        for p in self.patches:p.stop()
        self.temp.cleanup()
    def select_two(self):
        self.panel.grid.choose(SelectedSlot('15:00','16:00',1))
        self.panel.grid.choose(SelectedSlot('16:00','17:00',2))
    def scheduled_record(self):
        self.select_two()
        with patch.object(control_panel,'register_task',return_value={'task_name':'TEST_ONLY','start_at':'2030-01-01T19:00:00+08:00'}):
            self.panel.schedule_plan()
        return next(r for r in self.panel.history.records() if r['status']=='scheduled')
    def test_full_selection_can_replace_same_time_court_without_extra_slot(self):
        p=self.panel;self.select_two()
        replacement=SelectedSlot('15:00','16:00',2)
        self.assertEqual(p.current_model().state(replacement),'replaceable')
        p.grid.choose(replacement)
        self.assertEqual(p.current_model().selected,{replacement,SelectedSlot('16:00','17:00',2)})
        self.assertEqual(len(p.current_model().selected),2)

    def test_saved_plan_edit_button_loads_slots_without_writing(self):
        p=self.panel;row=self.scheduled_record()
        self.assertEqual(str(p.edit_plan_button.cget('state')),'normal')
        with patch.object(p.history,'apply') as apply:
            p.edit_current_plan()
        self.assertEqual(len(p.current_model().selected),2)
        apply.assert_not_called()
        self.assertEqual(p.history.find(row['id'])['status'],'scheduled')

    def test_direct_delete_requires_confirmation_and_only_deletes_local_plan(self):
        p=self.panel;row=self.scheduled_record()
        with patch.object(control_panel.messagebox,'askyesno',return_value=False), \
             patch.object(p,'delete_history_plan') as delete:
            p.delete_current_plan()
        delete.assert_not_called()
        with patch.object(control_panel.messagebox,'askyesno',return_value=True), \
             patch.object(p,'delete_history_plan') as delete:
            p.delete_current_plan()
        delete.assert_called_once_with(row['id'])

    def test_saved_plan_stays_blue_while_replacement_draft_is_orange(self):
        p=self.panel;self.scheduled_record()
        m=p.current_model()
        self.assertFalse(m.replace_all)
        colors=[p.grid.canvas.itemcget(item,'fill') for item in p.grid.canvas.find_all()
                if p.grid.canvas.type(item)=='rectangle']
        self.assertEqual(colors.count('#2563eb'),2)
        p.grid.choose(SelectedSlot('14:00','15:00',2))
        self.assertTrue(m.replace_all)
        colors=[p.grid.canvas.itemcget(item,'fill') for item in p.grid.canvas.find_all()
                if p.grid.canvas.type(item)=='rectangle']
        self.assertEqual(colors.count('#2563eb'),2)
        self.assertEqual(colors.count('#f59e0b'),1)

    def test_new_time_replaces_entire_saved_plan_only_after_confirmation(self):
        p=self.panel;row=self.scheduled_record()
        new=SelectedSlot('14:00','15:00',2)
        p.grid.choose(new)
        self.assertEqual(p.current_model().selected,{new})
        self.assertTrue(p.current_model().replace_all)
        with patch.object(control_panel.messagebox,'askyesno',return_value=False):
            p.schedule_plan()
        self.assertEqual(p.history.find(row['id'])['status'],'scheduled')
        colors=[p.grid.canvas.itemcget(i,'fill') for i in p.grid.canvas.find_all() if p.grid.canvas.type(i)=='rectangle']
        self.assertEqual(colors.count('#2563eb'),2)
        self.assertEqual(colors.count('#f59e0b'),1)
        p.grid.choose(SelectedSlot('15:00','16:00',1))
        with patch.object(control_panel.messagebox,'askyesno',return_value=True), \
             patch.object(control_panel,'register_task',return_value={'task_name':'TEST_ONLY','start_at':'2030-01-01T19:00:00+08:00'}):
            p.schedule_plan()
        active=[r for r in p.history.records() if r['status']=='scheduled']
        self.assertEqual(len(active),1)
        slots={(s['start'],s['end']) for s in active[0]['data']['selections']}
        self.assertEqual(slots,{('14:00','15:00'),('15:00','16:00')})
        colors=[p.grid.canvas.itemcget(i,'fill') for i in p.grid.canvas.find_all() if p.grid.canvas.type(i)=='rectangle']
        self.assertEqual(colors.count('#2563eb'),2)
        self.assertEqual(colors.count('#f59e0b'),0)

    def test_first_launch_opens_login_choice_before_background_login(self):
        p=self.panel
        with patch('login_settings.LoginSettings') as store, \
             patch.object(p,'open_startup_settings') as settings, \
             patch.object(control_panel.threading,'Thread') as thread:
            store.return_value.path.exists.return_value=False
            p.prepare_startup_login()
        settings.assert_called_once()
        thread.assert_not_called()

    def test_first_launch_manual_login_can_save_without_account_or_password(self):
        from login_settings import show_settings,LoginSettings
        p=self.panel
        with patch('session.PRIVATE',self.private), \
             patch('login_settings.detect_client',return_value=''), \
             patch('boot_service.autostart_enabled',return_value=False), \
             patch('boot_service.set_autostart') as autostart:
            dialog=show_settings(p)
            def children(w):
                for child in w.winfo_children():
                    yield child
                    yield from children(child)
            save=next(w for w in children(dialog) if w.winfo_class()=='TButton' and w.cget('text')=='保存设置')
            save.invoke()
            saved=LoginSettings(self.private).read()
        self.assertFalse(saved['authorized'])
        self.assertEqual(saved['account'],'')
        self.assertEqual(saved['password'],'')
        autostart.assert_called_once_with(False)

    def test_empty_selection_with_existing_schedule_shows_time_without_duplicate_start(self):
        p=self.panel;row=self.scheduled_record()
        task={'name':'TEST_ONLY','enabled':True,'sha256':row['config_sha256'],'state':'Ready'}
        with patch.object(control_panel,'list_booking_tasks',return_value=[task]), \
             patch.object(p,'data',side_effect=AssertionError('No new selection needed')), \
             patch.object(p.history,'apply') as apply,patch.object(control_panel.subprocess,'Popen') as start:
            p.begin()
        text=control_panel.messagebox.showinfo.call_args.args[1]
        self.assertIn('19:00',text);self.assertIn('无需重复开始',text)
        self.assertEqual(p.heading.cget('text'),'已登记定时启动')
        self.assertEqual(p.preview_heading.cget('text'),'已保存计划')
        apply.assert_not_called();start.assert_not_called()
    def test_no_plan_and_no_selection_asks_for_selection_not_quota(self):
        self.panel.begin()
        text=control_panel.messagebox.showerror.call_args.args[1]
        self.assertIn('点选',text);self.assertNotIn('最多',text)
    def test_disabled_schedule_is_not_reported_as_automatic_start(self):
        p=self.panel;row=self.scheduled_record()
        with patch.object(control_panel,'list_booking_tasks',return_value=[{'name':'TEST_ONLY','enabled':False,'sha256':row['config_sha256']}]), \
             patch.object(p.history,'apply') as apply:
            p.begin()
        self.assertIn('未启用',control_panel.messagebox.showwarning.call_args.args[1])
        apply.assert_not_called()
    def test_same_exact_selected_slots_already_registered_are_not_rewritten(self):
        p=self.panel;row=self.scheduled_record();self.select_two()
        with patch.object(control_panel,'list_booking_tasks',return_value=[{'name':'TEST_ONLY','enabled':True,'sha256':row['config_sha256'],'state':'Ready'}]), \
             patch.object(p.history,'apply') as apply:
            p.begin()
        apply.assert_not_called();self.assertEqual(p.start.cget('text'),'查看已有计划')
    def test_history_states_are_shown_instead_of_not_started(self):
        p=self.panel;row=self.scheduled_record()
        for state,title in [('READY','正在等待放场'),('UNCERTAIN','结果待核实'),('FAILED','尚未选择时段'),('STOPPED','尚未选择时段')]:
            p.history.update_run(row['id'],{'state':state,'message':'TEST_ONLY'})
            p.selection_changed()
            self.assertEqual(p.heading.cget('text'),title)
    def test_inactive_history_does_not_restore_last_config_or_affect_grid(self):
        p=self.panel;row=self.scheduled_record()
        for state in ('STOPPED','FAILED'):
            p.history.update_run(row['id'],{'state':state,'message':'TEST_ONLY'})
            p.boot_data=row['data']
            key=(row['data']['venue'],row['data']['day'])
            snapshot=p.snapshots[key]
            p.models.pop(key,None)
            p.accept_snapshot(snapshot)
            model=p.current_model()
            self.assertEqual(model.selected,set())
            self.assertEqual(model.replaceable,())
            self.assertEqual(model.reserved_count,0)
            self.assertIsNone(p.current_history_plan())
            self.assertEqual(p.heading.cget('text'),'尚未选择时段')
            colors=[p.grid.canvas.itemcget(i,'fill') for i in p.grid.canvas.find_all() if p.grid.canvas.type(i)=='rectangle']
            self.assertEqual(colors.count('#2563eb'),0)
            self.assertEqual(colors.count('#f59e0b'),0)
            self.assertIsNotNone(p.history.find(row['id']))

    def test_other_day_clears_previous_plan_status(self):
        p=self.panel;self.scheduled_record()
        p.change_day(next(d for d in p.days if d!=self.data['day']))
        self.assertEqual(p.heading.cget('text'),'尚未选择时段')
        self.assertNotIn('计划启动',p.detail.get('1.0','end'))
    def test_read_failure_does_not_always_claim_login_problem(self):
        p=self.panel
        for reason,login in [('other',False),('login',True)]:
            p.messages.put(('read_error',p.generation,reason))
            with patch.object(self.root,'after'):p.poll()
            self.assertEqual('登录资料不可用' in p.load_info.cget('text'),login)
    def test_week_read_count_excludes_cached_days_outside_calendar(self):
        p=self.panel
        p.models[(self.data['venue'],'2000-01-01')]=p.current_model()
        p.messages.put(('loaded',p.generation))
        with patch.object(self.root,'after'):p.poll()
        self.assertIn('1/7',p.load_info.cget('text'))
    def test_failed_replacement_does_not_hide_original_active_schedule(self):
        p=self.panel;row=self.scheduled_record()
        from copy import deepcopy
        with p.history.lock():
            doc=p.history.load();failed=deepcopy(row)
            failed.update(id='TEST_FAILED_REPLACEMENT',status='failed')
            doc['records'].append(failed);p.history.save(doc)
        self.assertEqual(p.current_history_plan()['id'],row['id'])
        p.selection_changed();self.assertEqual(p.heading.cget('text'),'已登记定时启动')
    def test_authentication_failure_is_reported_as_login_issue(self):
        p=self.panel
        with patch('control_panel.ScheduleReader') as reader,patch('control_panel.threading.Thread') as thread:
            reader.return_value.read_week.side_effect=control_panel.AuthenticationError('TEST_ONLY')
            p.read_week();thread.call_args.kwargs['target']()
        with patch.object(self.root,'after'):p.poll()
        self.assertIn('检查登录或拒绝',p.load_info.cget('text'))
    def test_schedule_read_failure_is_not_reported_as_registration_failure(self):
        p=self.panel;self.scheduled_record()
        with patch.object(control_panel,'list_booking_tasks',side_effect=OSError('TEST_ONLY')), \
             patch.object(p.history,'apply') as apply:
            p.begin()
        self.assertIn('暂时无法读取',control_panel.messagebox.showwarning.call_args.args[1])
        apply.assert_not_called()
    def test_close_hides_to_tray_without_stopping_worker(self):
        p=self.panel;p.tray=Mock(available=True)
        p.process=Mock();p.process.poll.return_value=None
        with patch.object(p,'request_stop') as stop,patch.object(self.root,'withdraw') as hide:
            p.close()
        hide.assert_called_once();stop.assert_not_called()
        self.assertFalse(p.closing);self.assertFalse(p.read_stop.is_set())
        p.tray=None
    def test_tray_failure_minimizes_instead_of_hiding_unreachable_window(self):
        with patch.object(self.root,'iconify') as minimize:
            self.panel.close()
        minimize.assert_called_once();self.assertFalse(self.panel.read_stop.is_set())
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
    def test_login_settings_default_to_no_password_authorization(self):
        from login_settings import show_settings
        with patch('session.PRIVATE',self.private), patch('login_settings.detect_client',return_value=''), \
             patch('boot_service.autostart_enabled',return_value=False):
            dialog=show_settings(self.panel)
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        entries=[w for w in descendants(dialog) if w.winfo_class()=='TEntry']
        masked=[w for w in entries if w.cget('show')]
        self.assertEqual(len(masked),1)
        self.assertEqual(str(masked[0].cget('state')),'disabled')
        self.assertFalse((self.private/'dingtalk-login.dpapi').exists())
        dialog.destroy()
    def test_manual_entry_prepares_authorized_login_before_reading_week(self):
        with patch('login_settings.LoginSettings') as store, \
             patch('login_settings.test_login',return_value='login verified') as login, \
             patch('control_panel.threading.Thread') as thread:
            store.return_value.read.return_value={'authorized':True}
            self.panel.prepare_startup_login()
            thread.call_args.kwargs['target']()
            login.assert_called_once_with(store.return_value)
        with patch.object(self.panel,'read_week') as read,patch.object(self.root,'after'):
            self.panel.poll()
        read.assert_called_once()
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
