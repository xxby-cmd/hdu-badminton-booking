"""Real Tk widgets, fake child process/scheduler, temporary state only."""
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch
import control_panel
from plan_config import default_config, load_config

class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.private=Path(self.temp.name)
        self.patches=[patch.object(control_panel,'PRIVATE',self.private),
                      patch.object(control_panel,'LAST',self.private/'last-plan.json')]
        for p in self.patches:p.start()
        self.root=tk.Tk();self.root.withdraw()
        self.panel=control_panel.Panel(self.root,default_config())
        self.root.update_idletasks()
    def tearDown(self):
        self.root.destroy()
        for p in self.patches:p.stop()
        self.temp.cleanup()

    def test_controls_save_same_plan_and_keep_fee_and_duration(self):
        p=self.panel
        p.vars['sites'].set('2, 3')
        p.save()
        data,plan=load_config(self.private/'last-plan.json')
        self.assertEqual(plan.site_ids,(2,3))
        self.assertEqual((plan.start,plan.end,plan.max_price,plan.allow_one_hour),('15:00','17:00',0,False))
        p.vars['sport'].set('网球')
        self.assertEqual(p.vars['venue'].get(),'穹顶网球场')
        self.assertEqual(p.data()[1].venue_type,'tennis')

    def test_button_launches_original_engine_and_stop_never_cancels(self):
        p=self.panel
        child=Mock(pid=87654321);child.poll.return_value=None
        with patch.object(control_panel.subprocess,'Popen',return_value=child) as launch:
            p.begin()
            command=launch.call_args.args[0]
            self.assertIn(str(control_panel.ROOT/'app.pyw'),command)
            self.assertIn('--config',command);self.assertIn('--hidden',command)
            chosen=load_config(command[command.index('--config')+1])[1]
            self.assertEqual(chosen,p.active_plan)
            self.assertEqual(str(p.start.cget('state')),'disabled')
            p.request_stop()
            self.assertEqual(json.loads(p.paths['stop'].read_text())['pid'],child.pid)
            p.begin();launch.assert_called_once()
            self.assertFalse(p.paths['submitted'].exists())
            self.assertFalse(p.paths['validation'].exists())

    def test_panel_uses_same_snapshot_for_scheduler(self):
        p=self.panel
        result={'start_at':'2030-01-01T19:00:00+08:00','verified':True}
        with patch.object(control_panel,'register_task',return_value=result) as register:
            p.schedule_plan()
            path,digest=register.call_args.args
            self.assertEqual(load_config(path,digest)[1],p.data()[1])
            self.assertIsNone(p.process)

if __name__=='__main__':unittest.main()
