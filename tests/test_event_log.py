import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from booking import BookingError, Plan
from event_log import EventLog, EventLogError


class OfflineTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'socket.getaddrinfo', 'urllib.request.urlopen'):
            blocker = patch(target, side_effect=AssertionError('Real network is forbidden'))
            blocker.start()
            self.addCleanup(blocker.stop)


class EventLogTests(OfflineTests):
    def make_log(self, folder):
        return EventLog(Path(folder) / 'status.json', Path(folder) / 'events.jsonl')

    def test_fifo_snapshots_and_latest_atomic_status(self):
        with tempfile.TemporaryDirectory() as folder:
            log = self.make_log(folder)
            replace = Path.replace
            replacements = []

            def atomic_replace(source, target):
                replacements.append((source, target))
                return replace(source, target)

            try:
                with patch.object(Path, 'replace', atomic_replace):
                    for number in range(100):
                        event = {'state': 'READY', 'number': number, 'timings': [{'value': number}]}
                        log.submit(event)
                        event['timings'][0]['value'] = 'changed after submission'
                    log.flush()
                events = [json.loads(line) for line in log.events_file.read_text(encoding='utf-8').splitlines()]
                self.assertEqual([event['number'] for event in events], list(range(100)))
                self.assertEqual([event['timings'][0]['value'] for event in events], list(range(100)))
                self.assertEqual(json.loads(log.state_file.read_text(encoding='utf-8')), events[-1])
                self.assertEqual(replacements, [(log.state_file.with_suffix('.tmp'), log.state_file)] * 100)
                self.assertFalse(log.state_file.with_suffix('.tmp').exists())
            finally:
                log.close()

    def test_slow_disk_does_not_block_submit_and_flush_waits_for_it(self):
        with tempfile.TemporaryDirectory() as folder:
            log = self.make_log(folder)
            entered, release, submitted, flushed = (threading.Event() for _ in range(4))
            original = log._write

            def slow_write(event):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('test writer was not released')
                original(event)

            def publish_more():
                log.submit({'state': 'ATTEMPTING'})
                submitted.set()

            def flush():
                log.flush()
                flushed.set()

            log._write = slow_write
            publisher = threading.Thread(target=publish_more)
            flusher = threading.Thread(target=flush)
            try:
                log.submit({'state': 'READY'})
                self.assertTrue(entered.wait(1))
                publisher.start()
                self.assertTrue(submitted.wait(1), 'submission waited on blocked file I/O')
                flusher.start()
                self.assertFalse(flushed.wait(.05))
                release.set()
                flusher.join(2)
                self.assertTrue(flushed.is_set())
            finally:
                release.set()
                publisher.join(2)
                flusher.join(2)
                log.close()

    def test_close_drains_once_and_rejects_later_submissions(self):
        with tempfile.TemporaryDirectory() as folder:
            log = self.make_log(folder)
            for number in range(20):
                log.submit({'number': number})
            log.close()
            log.close()
            log.flush()
            self.assertFalse(log._thread.is_alive())
            self.assertEqual(len(log.events_file.read_text(encoding='utf-8').splitlines()), 20)
            with self.assertRaises(EventLogError):
                log.submit({'number': 20})
            with self.assertRaises(EventLogError):
                log.check()

    def test_oserror_is_visible_sticky_and_does_not_deadlock_flush_or_close(self):
        with tempfile.TemporaryDirectory() as folder:
            log = self.make_log(folder)
            log._write = Mock(side_effect=OSError('PRIVATE_PATH_AND_DETAILS'))
            log.submit({'state': 'STARTING'})
            with self.assertRaises(EventLogError) as failure:
                log.flush()
            self.assertEqual(str(failure.exception), log.error)
            self.assertNotIn('PRIVATE_PATH_AND_DETAILS', str(failure.exception))
            log.submit({'state': 'FAILED'})
            self.assertEqual(len(log._pending), 2, 'failed/pending events should remain available in memory')
            with self.assertRaises(EventLogError):
                log.check()
            failures = []

            def close():
                try:
                    log.close()
                except EventLogError as exc:
                    failures.append(str(exc))

            closer = threading.Thread(target=close)
            closer.start()
            closer.join(1)
            self.assertFalse(closer.is_alive(), 'close deadlocked after writer failure')
            self.assertEqual(failures, [log.error])
            self.assertFalse(log._thread.is_alive())
            log._write.assert_called_once()

    def test_actual_append_failure_is_reported_without_exposing_path(self):
        with tempfile.TemporaryDirectory() as folder:
            log = self.make_log(folder)
            log.events_file.mkdir()
            log.submit({'state': 'STOPPED'})
            with self.assertRaises(EventLogError) as error:
                log.flush()
            self.assertNotIn(folder, str(error.exception))
            self.assertEqual(json.loads(log.state_file.read_text(encoding='utf-8')), {'state': 'STOPPED'})
            with self.assertRaises(EventLogError):
                log.close()
            self.assertFalse(log._thread.is_alive())


class FakeWidget:
    def __init__(self, root, **options):
        self.root = root.root if isinstance(root, FakeWidget) else root
        self.options = dict(options)
        self.root.widgets.append(self)

    def pack(self, **options):
        pass

    def configure(self, **options):
        self.options.update(options)


class FakeRoot:
    def __init__(self, drive):
        self.widgets = []
        self.callbacks = []
        self.drive = drive
        self.destroyed = False

    title = geometry = minsize = configure = deiconify = lift = lambda *args, **kwargs: None

    def after(self, delay, callback):
        self.callbacks.append(callback)

    def protocol(self, name, callback):
        self.close_callback = callback

    def destroy(self):
        self.destroyed = True

    def mainloop(self):
        self.drive(self)

    def refresh(self):
        next(callback for callback in self.callbacks if callback.__name__ == 'refresh')()

    @property
    def detail(self):
        return next(widget.options['text'] for widget in self.widgets if 'wraplength' in widget.options)

    @property
    def heading(self):
        return next(widget.options['text'] for widget in self.widgets
                    if widget.options.get('font') == ('Microsoft YaHei UI', 12, 'bold'))


class AppEventLogTests(OfflineTests):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / 'app.pyw'
        loader = importlib.machinery.SourceFileLoader('event_log_app_under_test', str(source))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.app = importlib.util.module_from_spec(spec)
        with patch.object(sys, 'argv', [str(source)]):
            loader.exec_module(cls.app)

    def run_app(self, scenario, inspect, write=None, existing_validation=False, close_early=None,
                fail_widget_setup=False):
        app = self.app
        logs = []
        stages = []
        scenario_errors = []

        class CheckedLog(EventLog):
            def __init__(inner, *args):
                super().__init__(*args)
                if write is not None:
                    inner._write = lambda event: write(inner, event)
                logs.append(inner)

            def close(inner):
                self.assertFalse(any(thread.name == 'HDU-booking-trial' and thread.is_alive()
                                     for thread in threading.enumerate()))
                stages.append('writer close')
                return super().close()

        def trial(api, plan, **kwargs):
            def run(**run_kwargs):
                try:
                    return scenario(kwargs, logs[0])
                except BaseException as exc:
                    scenario_errors.append(exc)
                    raise
            return Mock(run=run)

        def frame(*args, **kwargs):
            if fail_widget_setup:
                raise RuntimeError('test window setup failure')
            return FakeWidget(*args, **kwargs)

        def drive(root):
            if close_early is not None:
                root.close_callback()
                self.assertFalse(root.destroyed)
                self.assertNotIn('writer close', stages)
                close_early.set()
            worker = next((thread for thread in threading.enumerate()
                           if thread.name == 'HDU-booking-trial'), None)
            if worker is not None:
                worker.join(3)
                self.assertFalse(worker.is_alive(), 'mock booking worker did not finish')
            self.assertEqual(scenario_errors, [])
            stages.append('worker stopped')
            root.refresh()
            inspect(root, logs[0])
            root.close_callback()

        with tempfile.TemporaryDirectory() as folder:
            location = Path(folder)
            if existing_validation:
                (location / 'validation.json').write_text('{"stage":"validation_intent"}', encoding='utf-8')
            root = FakeRoot(drive)
            kernel = Mock()
            kernel.CreateMutexW.return_value = 123
            with patch.object(app.ctypes, 'WinDLL', return_value=kernel), \
                 patch.object(app.ctypes, 'get_last_error', return_value=0), \
                 patch.object(app.tk, 'Tk', return_value=root), \
                 patch.object(app.ttk, 'Frame', side_effect=frame), \
                 patch.object(app.ttk, 'Label', FakeWidget), \
                 patch.object(app.ttk, 'Button', FakeWidget), \
                 patch.object(app, 'PRIVATE', location), \
                 patch.object(app, 'STATE_FILE', location / 'status.json'), \
                 patch.object(app, 'EVENTS_FILE', location / 'events.jsonl'), \
                 patch.object(app, 'SUBMITTED', location / 'submitted.json'), \
                 patch.object(app, 'VALIDATION_INTENT', location / 'validation.json'), \
                 patch.object(app, 'load_session', return_value=({'token': 'TEST_ONLY'}, {})), \
                 patch.object(app, 'Api', return_value=Mock()), \
                 patch.object(app, 'EventLog', CheckedLog), \
                 patch.object(app, 'Trial', side_effect=trial):
                if fail_widget_setup:
                    with self.assertRaisesRegex(RuntimeError, 'test window setup failure'):
                        app.main()
                    inspect(root, logs[0])
                else:
                    app.main()
            if not fail_widget_setup:
                self.assertTrue(root.destroyed)
            self.assertEqual(scenario_errors, [])
            self.assertEqual(stages[0], 'writer close' if fail_widget_setup else 'worker stopped')
            self.assertFalse(logs[0]._thread.is_alive())
            kernel.CloseHandle.assert_called_once_with(123)

    def test_publish_updates_ui_and_reaches_first_http_while_writer_is_blocked(self):
        entered, release = threading.Event(), threading.Event()

        def write(log, event):
            if event['state'] == 'ATTEMPTING':
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('test did not release writer')
            EventLog._write(log, event)

        def scenario(callbacks, log):
            callbacks['publish'](state='ATTEMPTING', message='test check')
            self.assertTrue(entered.wait(1))
            first_http = Mock()
            try:
                callbacks['publish'](state='VERIFYING', message='test submit')
                first_http()
                first_http.assert_called_once()
                self.assertFalse(release.is_set())
            finally:
                release.set()
            callbacks['publish'](state='STOPPED', message='test complete')

        def inspect(root, log):
            events = [json.loads(line) for line in log.events_file.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([event['state'] for event in events],
                             ['STARTING', 'ATTEMPTING', 'VERIFYING', 'STOPPED'])
            self.assertTrue(all(event['updated_at'] and event['pid'] and event['plan'] for event in events))
            self.assertEqual(root.detail, 'test complete')

        self.run_app(scenario, inspect, write)

    def test_known_log_failure_blocks_original_marker_and_create(self):
        marker = Mock()
        create = Mock()

        def scenario(callbacks, log):
            with self.assertRaises(EventLogError):
                log.flush()
            for callback, args in ((callbacks['before_attempt'], ()),
                                   (callbacks['before_create'], (Plan(), 1))):
                with self.assertRaises(BookingError):
                    callback(*args)
                    create()
            callbacks['publish'](state='FAILED', message='known log failure stopped creation')

        def inspect(root, log):
            self.assertEqual(root.heading, '本次已停止')
            self.assertIn(log.error, root.detail)
            self.assertNotIn('PRIVATE_DISK_DETAIL', root.detail)
            marker.assert_not_called()
            create.assert_not_called()

        with patch.object(self.app, 'mark_submission', marker):
            self.run_app(scenario, inspect, lambda log, event: (_ for _ in ()).throw(OSError('PRIVATE_DISK_DETAIL')))

    def test_before_create_preserves_durable_marker_and_checks_error_first(self):
        calls = []
        original_fsync = os.fsync

        def fsync(descriptor):
            calls.append('fsync')
            return original_fsync(descriptor)

        def scenario(callbacks, log):
            original_check = log.check

            def check():
                calls.append('logging check')
                original_check()

            log.check = check
            callbacks['before_create'](Plan(), 1)
            calls.append('create')
            self.assertEqual(json.loads(self.app.SUBMITTED.read_text(encoding='utf-8')),
                             {'day': Plan().day, 'start': '19:00', 'end': '21:00', 'site_id': 1,
                              'stage': 'create_reserved'})
            callbacks['publish'](state='STOPPED', message='test complete')

        def inspect(root, log):
            self.assertEqual(calls, ['logging check', 'fsync', 'create'])
            self.assertEqual(root.heading, '已停止')

        with patch('booking.os.fsync', side_effect=fsync):
            self.run_app(scenario, inspect)

    def test_before_attempt_checks_log_then_writes_validation_intent_stage(self):
        marker = Mock()
        calls = []

        def scenario(callbacks, log):
            log.check = lambda: calls.append('logging check')
            callbacks['before_attempt']()
            marker.assert_called_once_with(self.app.VALIDATION_INTENT, self.app.PLAN, None,
                                           stage='validation_intent')
            calls.append('ARMED')
            callbacks['publish'](state='STOPPED', message='test armed')

        def inspect(root, log):
            self.assertEqual(calls, ['logging check', 'marker', 'ARMED'])
            self.assertEqual(root.heading, '已停止')

        marker.side_effect = lambda *args, **kwargs: calls.append('marker')
        with patch.object(self.app, 'mark_submission', marker):
            self.run_app(scenario, inspect)

    def test_existing_validation_intent_blocks_startup_before_session_or_requests(self):
        scenario = Mock()

        def inspect(root, log):
            scenario.assert_not_called()
            self.app.load_session.assert_not_called()
            self.app.Api.assert_not_called()
            self.assertEqual(root.heading, '本次已停止')
            self.assertEqual(root.detail, '已有提交或可能的校验记录，请先核对，勿重启重试')
            self.assertEqual(json.loads(self.app.VALIDATION_INTENT.read_text(encoding='utf-8')),
                             {'stage': 'validation_intent'})
            self.assertFalse(self.app.SUBMITTED.exists())

        self.run_app(scenario, inspect, existing_validation=True)

    def test_success_remains_success_when_terminal_log_write_fails(self):
        marker = Mock()

        def write(log, event):
            if event['state'] == 'CREATED_PENDING_INVITE':
                raise OSError('PRIVATE_DISK_DETAIL')
            EventLog._write(log, event)

        def scenario(callbacks, log):
            callbacks['before_create'](Plan(), 1)
            callbacks['publish'](state='CREATED_PENDING_INVITE', message='verified', site_id=1,
                                 booked_start='19:00', booked_end='21:00')

        def inspect(root, log):
            self.assertEqual(root.heading, '订单已创建，等待你邀请同伴')
            self.assertIn('订单已核实', root.detail)
            self.assertIn(log.error, root.detail)
            self.assertNotIn('PRIVATE_DISK_DETAIL', root.detail)
            marker.assert_called_once_with(self.app.SUBMITTED, Plan(), 1)

        with patch.object(self.app, 'mark_submission', marker):
            self.run_app(scenario, inspect, write)

    def test_close_signals_stop_then_waits_for_booking_worker_before_writer(self):
        finish_worker = threading.Event()

        def scenario(callbacks, log):
            self.assertTrue(callbacks['stop'].wait(2))
            self.assertTrue(finish_worker.wait(2))
            callbacks['publish'](state='STOPPED', message='stop observed before close')

        def inspect(root, log):
            self.assertEqual(root.heading, '已停止')
            self.assertEqual(root.detail, 'stop observed before close')
            self.assertFalse(log._closing)

        self.run_app(scenario, inspect, close_early=finish_worker)

    def test_window_initialization_exception_closes_the_non_daemon_writer(self):
        scenario = Mock()

        def inspect(root, log):
            scenario.assert_not_called()
            self.assertFalse(log._thread.is_alive())

        self.run_app(scenario, inspect, fail_widget_setup=True)


if __name__ == '__main__':
    unittest.main()
