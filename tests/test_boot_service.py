from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from booking import TZ,BookingError
from boot_service import recover_due_tasks
from plan_config import save_config, parse_config, paths_for
from plan_history import PlanHistory


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.private = Path(self.temp.name)
        self.history = PlanHistory(self.private)
        data = {'schema': 1, 'day': '2030-11-10', 'start': '16:00', 'end': '17:00',
                'venue': '综合馆羽毛球', 'venue_type': 'badminton', 'site_ids': [10],
                'max_price': 0, 'allow_one_hour': False}
        self.path = self.private / 'original.json'
        digest = save_config(self.path, data)
        self.task = {'name': 'HDU-Booking-20301108', 'enabled': True, 'state': 'Ready',
                     'config': str(self.path), 'sha256': digest}
        self.row = {'id': 'scheduled-revision', 'data': data, 'status': 'scheduled',
                    'task_name': self.task['name'], 'config_sha256': digest, 'pid': None}
        self.document = {'schema': 1, 'records': [self.row], 'hidden_school': []}
        self.history.save(self.document)
        self.plan = parse_config(data)
        self.now = datetime(2030, 11, 8, 19, 10, tzinfo=TZ)
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def run_recovery(self, **kwargs):
        return recover_due_tasks(self.private, self.now, [self.task], self.calls.append, **kwargs)

    def test_late_logon_starts_only_existing_same_revision_task(self):
        self.assertEqual(self.run_recovery(), [self.task['name']])
        self.run_recovery()
        self.assertEqual(self.calls, [self.task['name']])

    def test_before_19_and_at_release_never_start(self):
        for hour in (18, 20):
            self.now = self.now.replace(hour=hour, minute=0)
            self.assertEqual(self.run_recovery(), [])
        self.assertFalse(self.calls)

    def test_deleted_stopped_unknown_and_disabled_never_revive(self):
        for state in ('deleted', 'stopped', 'superseded', 'uncertain', 'confirmed', 'failed'):
            self.row['status'] = state
            self.history.save(self.document)
            self.assertFalse(self.run_recovery())
        self.row['status'] = 'scheduled'
        self.history.save(self.document)
        self.task['enabled'] = False
        self.assertFalse(self.run_recovery())
        self.assertFalse(self.calls)

    def test_global_intent_or_submission_prevents_restart(self):
        paths = paths_for(self.plan, self.private)
        for marker in ('validation', 'submitted'):
            paths[marker].write_text('{}')
            self.assertFalse(self.run_recovery())
            paths[marker].unlink()
        self.assertFalse(self.calls)

    def test_different_or_running_system_revision_is_not_started(self):
        self.task['state'] = 'Running'
        self.assertFalse(self.run_recovery())
        self.task['state'] = 'Ready'
        self.task['sha256'] = '0' * 64
        self.assertFalse(self.run_recovery())

    def test_only_dead_waiting_worker_with_safe_last_event_can_resume(self):
        self.row.update(status='waiting', pid=777, started_at=self.now.isoformat())
        self.history.save(self.document)
        paths_for(self.plan, self.private)['state'].write_text(json.dumps({'pid': 777, 'state': 'READY'}))
        self.assertFalse(self.run_recovery(alive=lambda row: True))
        self.assertTrue(self.run_recovery(alive=lambda row: False))

    def test_stop_or_uncertain_last_event_prevents_crash_recovery(self):
        self.row.update(status='waiting', pid=777, started_at=self.now.isoformat())
        self.history.save(self.document)
        for state in ('STOPPED', 'UNCERTAIN', 'ATTEMPTING', 'CREATED_PENDING_INVITE'):
            paths_for(self.plan, self.private)['state'].write_text(json.dumps({'pid': 777, 'state': state}))
            self.assertFalse(self.run_recovery(alive=lambda row: False))

    def test_persisted_stop_blocks_recovery_before_stopped_event_is_written(self):
        self.row.update(status='waiting', pid=777, started_at=self.now.isoformat())
        self.history.save(self.document)
        paths = paths_for(self.plan, self.private)
        paths['state'].write_text(json.dumps({'pid': 777, 'state': 'READY'}))
        paths['stop'].write_text(json.dumps({'pid': 777}))
        self.assertFalse(self.run_recovery(alive=lambda row: False))

    def test_windows_dispatch_can_recover_dead_waiter_before_guardian(self):
        self.row.update(status='waiting', pid=777, started_at=self.now.isoformat())
        self.history.save(self.document)
        paths_for(self.plan, self.private)['state'].write_text(json.dumps({'pid': 777, 'state': 'READY'}))
        with patch('boot_service.process_matches', return_value=False), patch('plan_history.datetime') as clock:
            clock.now.return_value = self.now
            row = self.history.attach_configured(self.row['data'], self.path,
                lambda data, mode, rid: {'pid': 888, 'config_sha256': self.task['sha256']})
        self.assertEqual(row['pid'], 888)
        self.assertEqual(row['status'], 'waiting')
    def test_windows_auto_dispatch_never_revives_stopped_or_failed_revision(self):
        for status in ('stopped','failed'):
            self.row['status']=status
            self.history.save(self.document)
            with self.assertRaises(BookingError):
                self.history.attach_configured(self.row['data'],self.path,
                    lambda *args:self.fail('Stopped or failed plan must not launch'))


if __name__ == '__main__':
    unittest.main()
