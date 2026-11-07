import http.client
from datetime import datetime, timedelta
import unittest
from unittest.mock import Mock, call, patch

from test_booking import Clock, RoutedApi, SESSION, order
from booking import Api, AuthenticationError, BookingError, NetworkError, Plan, ServerClock, Trial, TZ


class ReleaseTimingTests(unittest.TestCase):
    def make_trial(self, final_error=None, initial_offset=-120, order_reads=None):
        plan = Plan()
        api = RoutedApi(order_reads=order_reads or [[], [], [], [], [order()]])
        clock = Clock(plan.release.timestamp() + initial_offset)
        clock.sync = Mock(side_effect=[None, None, final_error])
        events = []
        marker = Mock()
        trial = Trial(api, plan, clock=clock, before_create=marker,
                      publish=lambda **event: events.append(event), shuffle=lambda values: None)
        waits = []

        def advance(target):
            waits.append(target)
            clock.value = max(clock.value, target)

        trial.wait_until = advance
        return trial, api, clock, waits, events, marker

    def test_full_recheck_is_t_minus_60_and_final_sync_is_t_minus_3(self):
        trial, api, clock, waits, _, marker = self.make_trial()
        self.assertIsNotNone(trial.run())
        release = trial.plan.release.timestamp()
        self.assertEqual(waits, [release - 60, release - 3, release])
        self.assertEqual(clock.sync.call_args_list,
                         [call(api, timeout=1), call(api, timeout=1), call(api, timeout=.4)])
        endpoints = [endpoint for endpoint, _ in api.calls]
        first = endpoints.index('creat_book_info')
        self.assertEqual(endpoints[first:], ['creat_book_info', 'creat_order', 'orders'])
        marker.assert_called_once()

    def test_late_start_does_not_repeat_the_just_completed_full_preflight(self):
        trial, api, clock, waits, events, marker = self.make_trial(
            initial_offset=-20, order_reads=[[], [], [order()]])
        refresh_session = Mock(return_value=SESSION)
        self.assertIsNotNone(trial.run(refresh_session=refresh_session))
        release = trial.plan.release.timestamp()
        self.assertEqual(waits, [release - 3, release])
        self.assertEqual(clock.sync.call_args_list, [call(api, timeout=1), call(api, timeout=.4)])
        endpoints = [endpoint for endpoint, _ in api.calls]
        first = endpoints.index('creat_book_info')
        self.assertEqual(endpoints[:first],
                         ['orders', 'orders', 'post_venue_info', 'post_site_situation'])
        self.assertEqual(endpoints[first:], ['creat_book_info', 'creat_order', 'orders'])
        self.assertEqual(sum(event['state'] == 'READY' for event in events), 1)
        refresh_session.assert_not_called()
        marker.assert_called_once()

    def test_final_transport_failure_uses_the_clock_synced_this_run(self):
        trial, api, clock, _, events, marker = self.make_trial(NetworkError('网络请求未得到完整回复'))
        self.assertIsNotNone(trial.run())
        self.assertEqual(clock.sync.call_count, 3)
        self.assertEqual(clock.sync.call_args.kwargs, {'timeout': .4})
        self.assertEqual(trial.first_request_time, trial.plan.release.timestamp())
        self.assertEqual(events[-1]['state'], 'CREATED_PENDING_INVITE')
        self.assertEqual(sum(endpoint == 'creat_order' for endpoint, _ in api.calls), 1)
        marker.assert_called_once()

    def test_final_authentication_or_business_failure_still_stops(self):
        for error in (AuthenticationError('登录失效'), BookingError('无法校准学校服务器时间')):
            with self.subTest(error=type(error).__name__):
                trial, api, _, _, events, marker = self.make_trial(error)
                self.assertIsNone(trial.run())
                self.assertEqual(events[-1]['state'], 'FAILED')
                self.assertFalse(any(endpoint.startswith('creat') for endpoint, _ in api.calls))
                marker.assert_not_called()

    def test_short_sync_sets_existing_socket_and_reconnect_then_restores_default(self):
        stale, fresh = Mock(), Mock()
        stale.request.side_effect = http.client.RemoteDisconnected('PRIVATE_NETWORK_DETAIL')
        fresh.getresponse.return_value = Mock(status=200)
        fresh.getresponse.return_value.read.return_value = b'{"data":1793690000000}'
        with patch('booking.urllib.request.getproxies', return_value={}), \
             patch('booking.http.client.HTTPSConnection', return_value=fresh) as connect:
            api = Api(SESSION)
            api.connection = stale
            ServerClock().sync(api, timeout=.4)
            self.assertEqual(stale.timeout, .4)
            stale.sock.settimeout.assert_called_once_with(.4)
            connect.assert_called_once_with('sportmeta.hdu.edu.cn', timeout=.4)
            self.assertEqual(fresh.timeout, .4)
            self.assertEqual(fresh.sock.settimeout.call_args, call(.4))
            api.call('post_server_time')
        self.assertEqual(fresh.timeout, 5)
        self.assertEqual(fresh.sock.settimeout.call_args_list, [call(.4), call(5)])
        failure, retried, normal = api.timings
        self.assertEqual(failure['endpoint'], 'post_server_time')
        self.assertEqual(failure['error_type'], 'RemoteDisconnected')
        self.assertEqual(failure['attempt'], 1)
        self.assertGreaterEqual(failure['elapsed_ms'], 0)
        self.assertEqual((retried['attempt'], retried['status']), (2, 200))
        self.assertEqual((normal['attempt'], normal['status']), (1, 200))
        self.assertNotIn('PRIVATE_NETWORK_DETAIL', str(api.timings))

    def test_failed_initial_preflight_publishes_attempt_diagnostics_without_private_text(self):
        connections = [Mock() for _ in range(6)]
        for connection in connections:
            connection.request.side_effect = http.client.RemoteDisconnected('PRIVATE_NETWORK_DETAIL')
        events = []
        plan = Plan(release=datetime.now(TZ) + timedelta(minutes=1))
        with patch('booking.urllib.request.getproxies', return_value={}), \
             patch('booking.http.client.HTTPSConnection', side_effect=connections):
            api = Api(SESSION)
            trial = Trial(api, plan, publish=lambda **event: events.append(event))
            self.assertIsNone(trial.run())
        self.assertEqual(events[-1]['state'], 'FAILED')
        timings = events[-1]['request_timings']
        self.assertEqual(len(timings), 6)
        self.assertEqual([item['attempt'] for item in timings], [1, 2, 1, 2, 1, 2])
        self.assertTrue(all(item['endpoint'] == 'post_server_time' for item in timings))
        self.assertTrue(all(item['error_type'] == 'RemoteDisconnected' for item in timings))
        self.assertTrue(all(item['elapsed_ms'] >= 0 for item in timings))
        self.assertNotIn('PRIVATE_NETWORK_DETAIL', str(events))
        for connection in connections:self.assertEqual(connection.request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
