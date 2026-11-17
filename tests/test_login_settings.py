import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys
import time
from unittest.mock import Mock, patch

from booking import BookingError
from login_settings import LoginSettings,test_login
from test_venue_startup import SimulatedTime
from venue_startup import wait_for_session
from session import SessionError


class AuthorizedLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoginSettings(self.temp.name)
        self.store.login_process_identity = Mock(return_value=(101,'TEST_START'))
        self.client = Path(self.temp.name) / 'DingTalk.exe'
        self.client.write_bytes(b'TEST_ONLY')

    def tearDown(self):
        self.temp.cleanup()

    def save(self, authorized=True):
        self.store.save(client=str(self.client), launch_client=False, authorized=authorized,
                        account='TEST_ONLY_ACCOUNT', password='TEST_ONLY_SECRET')

    def test_updater_version_switch_only_follows_same_install(self):
        base=self.store.private/'install/main'
        old=base/'current/DingTalk.exe'
        new=base/'current_new/DingTalk.exe'
        new.parent.mkdir(parents=True);new.write_bytes(b'TEST_ONLY')
        with patch('login_settings.detect_client',return_value=str(new)):
            self.assertEqual(self.store.active_client(str(old)),str(new))
            self.assertEqual(self.store.active_client(str(self.client)),str(self.client))

    def test_readiness_retries_ten_times_and_stores_no_credentials(self):
        self.save()
        with patch.object(self.store,'client_logged_in',return_value=False), \
             patch.object(self.store,'assist',return_value='NO_FORM') as assist:
            self.assertEqual(self.store.assist_with_retries(2030000000,pause=lambda _:None),'NO_FORM')
        self.assertEqual(assist.call_count,10)
        state=json.loads((self.store.private/'dingtalk-login-status.json').read_text())
        self.assertEqual(state['attempt'],10)
        self.assertNotIn('TEST_ONLY_SECRET',str(state))

    def test_retry_stops_as_soon_as_native_login_succeeds(self):
        self.save()
        with patch.object(self.store,'client_logged_in',side_effect=[False,False,True]), \
             patch.object(self.store,'assist',return_value='NO_FORM') as assist:
            self.assertEqual(self.store.assist_with_retries(2030000000,pause=lambda _:None),'ALREADY_LOGGED_IN')
        self.assertEqual(assist.call_count,2)

    def test_submitted_password_is_only_monitored_not_replayed(self):
        self.save()
        with patch.object(self.store,'client_logged_in',return_value=False), \
             patch.object(self.store,'assist',return_value='SUBMITTED') as assist:
            self.assertEqual(self.store.assist_with_retries(2030000000,pause=lambda _:None),'SUBMITTED')
        assist.assert_called_once()

    def test_manual_or_ambiguous_result_stops_retry(self):
        self.save()
        with patch.object(self.store,'client_logged_in',return_value=False), \
             patch.object(self.store,'assist',return_value='MANUAL') as assist:
            self.assertEqual(self.store.assist_with_retries(2030000000,pause=lambda _:None),'MANUAL')
        assist.assert_called_once()

    def test_password_encrypted_and_revocation_clears_saved_credentials(self):
        self.save()
        encrypted = self.store.path.read_bytes()
        self.assertNotIn(b'TEST_ONLY_ACCOUNT', encrypted)
        self.assertNotIn(b'TEST_ONLY_SECRET', encrypted)
        self.assertEqual(self.store.read()['password'], 'TEST_ONLY_SECRET')
        self.save(False)
        self.assertFalse(self.store.read()['authorized'])
        self.assertEqual(self.store.read()['password'], '')

    def test_no_authorization_never_launches_credential_automation(self):
        with patch('login_settings.subprocess.run') as run:
            self.assertEqual(self.store.assist(2030000000), 'MANUAL')
            run.assert_not_called()

    def test_one_possible_login_submission_per_revision_and_release(self):
        self.save()
        response = Mock(returncode=0, stdout=b'SUBMITTED')
        with patch('login_settings.subprocess.run', return_value=response) as run:
            self.assertEqual(self.store.assist(2030000000), 'SUBMITTED')
            self.assertEqual(self.store.assist(2030000000), 'MANUAL')
            run.assert_called_once()
            args, kwargs = run.call_args
            self.assertNotIn('TEST_ONLY_SECRET', str(args))
            self.assertIn('-Command',args[0])
            self.assertNotIn('-File',args[0])
            self.assertEqual(json.loads(kwargs['input'])['password'], 'TEST_ONLY_SECRET')

    def test_timeout_keeps_attempt_reserved_and_does_not_resubmit(self):
        self.save()
        with patch('login_settings.subprocess.run', side_effect=subprocess.TimeoutExpired('TEST_ONLY', 1)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.store.assist(2030000000)
            self.assertEqual(self.store.assist(2030000000), 'MANUAL')
            run.assert_called_once()
    def test_absent_form_does_not_consume_later_actual_login(self):
        self.save()
        with patch('login_settings.subprocess.run',side_effect=[Mock(returncode=0,stdout=b'NO_FORM'),
                                                               Mock(returncode=0,stdout=b'SUBMITTED')]) as run:
            self.assertEqual(self.store.assist(2030000000),'NO_FORM')
            self.assertEqual(self.store.assist(2030000000),'SUBMITTED')
            self.assertEqual(self.store.assist(2030000000),'MANUAL')
            self.assertEqual(run.call_count,2)
    def test_fresh_session_from_first_window_does_not_open_second_window(self):
        with patch.object(self.store,'launch_client'), \
             patch.object(self.store,'client_logged_in',return_value=True), \
             patch('session.read_live_session',side_effect=[{'token':'old'},{'token':'fresh'}]), \
             patch('session.validate_session'),patch('login_settings.time.sleep'), \
             patch('login_settings.os.startfile') as entrance, \
             patch('booking.Api.orders') as orders:
            self.assertIn('通过只读查询',test_login(self.store))
        entrance.assert_called_once()
        orders.assert_called_once_with(False)

    def test_second_window_is_only_fallback_after_eight_unsuccessful_checks(self):
        calls=[]
        def read():
            calls.append('read')
            return {'token':'old'} if calls.count('read')<=9 else {'token':'fresh'}
        with patch.object(self.store,'launch_client'), \
             patch.object(self.store,'client_logged_in',return_value=True), \
             patch('session.read_live_session',side_effect=read), \
             patch('session.validate_session'),patch('login_settings.time.sleep'), \
             patch('login_settings.os.startfile',side_effect=lambda _:calls.append('open')) as entrance, \
             patch('booking.Api.orders') as orders:
            self.assertIn('通过只读查询',test_login(self.store))
        self.assertEqual(entrance.call_count,2)
        self.assertEqual(calls.index('open',calls.index('open')+1),10)

    def test_old_session_cannot_prove_new_password_login_succeeded(self):
        with patch.object(self.store,'assist_with_retries',return_value='SUBMITTED'), \
             patch.object(self.store,'client_logged_in',return_value=False), \
             patch('session.read_live_session',return_value={'token':'old-token'}), \
             patch('login_settings.time.sleep'),patch('login_settings.os.startfile'), \
             patch('booking.Api.orders') as orders:
            message=test_login(self.store)
        self.assertIn('尚未核实',message)
        orders.assert_not_called()
    def test_proven_host_rejection_before_execution_allows_corrected_dispatch(self):
        self.save()
        saved=self.store.read()
        from datetime import datetime
        from booking import TZ
        key=saved['revision']+':'+datetime.fromtimestamp(2030000000,TZ).date().isoformat()+':101:TEST_START'
        self.store.record_attempts(self.store.private/'dingtalk-login-attempts.json',
                                   {key:'HOST_BLOCKED_BEFORE_EXECUTION'})
        with patch('login_settings.subprocess.run',return_value=Mock(returncode=0,stdout=b'SUBMITTED')) as run:
            self.assertEqual(self.store.assist(2030000000),'SUBMITTED')
            self.assertEqual(self.store.assist(2030000000),'MANUAL')
            run.assert_called_once()

    def test_invalid_target_executable_cannot_be_authorized(self):
        with self.assertRaises(BookingError):
            self.store.save(client='not-DingTalk.exe', launch_client=True, authorized=True,
                            account='TEST_ONLY', password='TEST_ONLY')
    def test_official_desktop_launcher_is_used_for_cold_start(self):
        install=self.store.private/'DingDing'
        client=install/'main/current/DingTalk.exe'
        client.parent.mkdir(parents=True);client.write_bytes(b'TEST_ONLY')
        launcher=install/'DingtalkLauncher.exe';launcher.write_bytes(b'TEST_ONLY')
        self.store.save(client=str(client),launch_client=True,authorized=False)
        with patch('login_settings.subprocess.Popen') as start:
            self.assertTrue(self.store.launch_client())
        self.assertEqual(start.call_args.args[0],[str(launcher),'/run:desktop'])
        self.assertEqual(start.call_args.kwargs['cwd'],install)
    def test_new_client_process_can_login_without_repeating_in_same_process(self):
        self.save()
        self.store.login_process_identity.side_effect=[(101,'FIRST'),(101,'FIRST'),(202,'SECOND')]
        with patch('login_settings.subprocess.run',return_value=Mock(returncode=0,stdout=b'SUBMITTED')) as run:
            self.assertEqual(self.store.assist(2030000000),'SUBMITTED')
            self.assertEqual(self.store.assist(2030000000),'MANUAL')
            self.assertEqual(self.store.assist(2030000000),'SUBMITTED')
        self.assertEqual(run.call_count,2)
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['expected_pid'],202)
    def test_official_entrance_opens_before_login_form_probe(self):
        order=[]
        with patch.object(self.store,'launch_client',side_effect=lambda:order.append('client')), \
             patch.object(self.store,'client_logged_in',side_effect=lambda:order.append('probe') or True), \
             patch('session.read_live_session',return_value={'token':'old-token'}), \
             patch('login_settings.os.startfile',side_effect=lambda uri:order.append('entrance')), \
             patch('login_settings.time.sleep'):
            test_login(self.store)
        self.assertEqual(order[:3],['client','entrance','probe'])
    def test_probe_uses_selected_client_without_sending_credentials(self):
        self.save()
        with patch('login_settings.subprocess.run',return_value=Mock(returncode=0,stdout=b'ALREADY_LOGGED_IN')) as run:
            self.assertTrue(self.store.client_logged_in())
        payload=json.loads(run.call_args.kwargs['input'])
        self.assertEqual(payload,{'client':str(self.client)})

    def test_login_helper_failure_still_waits_for_user_login(self):
        clock = SimulatedTime()
        loader = Mock(side_effect=[SessionError('TEST_ONLY'), 'fresh_session'])
        helper = Mock(side_effect=subprocess.TimeoutExpired('TEST_ONLY', 1))
        result = wait_for_session(600, clock, Mock(), loader, now=clock.now,
                                  open_entrance=Mock(), assist_login=helper)
        self.assertEqual(result, 'fresh_session')
        helper.assert_called_once_with(600)

    def test_valid_session_does_not_fill_credentials(self):
        helper = Mock()
        result = wait_for_session(600, SimulatedTime(), Mock(), Mock(return_value='valid'),
                                  now=lambda: 0, open_entrance=Mock(), assist_login=helper)
        self.assertEqual(result, 'valid')
        helper.assert_not_called()

    def test_completed_revocation_cannot_race_with_old_submission(self):
        self.save()
        code = '''
import sys,time
from pathlib import Path
from unittest.mock import patch,Mock
from login_settings import LoginSettings
store=LoginSettings(sys.argv[1])
store.login_process_identity=lambda: (101,'TEST_START')
def helper(*args,**kwargs):
    (store.private/'entered').write_text('yes')
    time.sleep(.5)
    assert store.read()['authorized']
    return Mock(returncode=0,stdout=b'SUBMITTED')
with patch('login_settings.subprocess.run',side_effect=helper):
    assert store.assist(2030000000)=='SUBMITTED'
'''
        worker = subprocess.Popen([sys.executable,'-B','-c',code,self.temp.name],
                                  stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            for _ in range(40):
                if (self.store.private/'entered').exists():break
                time.sleep(.025)
            self.assertTrue((self.store.private/'entered').exists())
            self.save(False)
            output,errors=worker.communicate(timeout=3)
            self.assertEqual(worker.returncode,0,errors.decode(errors='replace'))
            self.assertFalse(self.store.read()['authorized'])
            self.assertEqual(self.store.assist(2030000000),'MANUAL')
        finally:
            if worker.poll() is None:
                worker.terminate();worker.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
