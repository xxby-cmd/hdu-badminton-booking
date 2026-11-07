import http.client
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from booking import Api, AuthenticationError, BookingError, Plan, ReadServiceError, Reply, Trial, Uncertain


SESSION = {'token': 'TEST_ONLY', 'openid': 'TEST_ONLY',
           'nickname': 'TEST_ONLY', 'phone': 'TEST_ONLY'}
META = {'time_list': ['19:00', '20:00', '21:00'], 'rest_list': [], 'base_price':0}


class ApiTests(unittest.TestCase):
    def test_read_only_queries_reconnect_once_including_post(self):
        for endpoint in ('post_server_time', 'post_order_info',
                         'post_venue_info', 'post_site_situation', 'choose_venue'):
            with self.subTest(endpoint=endpoint):
                stale, fresh = Mock(), Mock()
                stale.request.side_effect = http.client.RemoteDisconnected('stale connection')
                fresh.getresponse.return_value = Mock(status=200)
                fresh.getresponse.return_value.read.return_value = b'{"data":123}'
                with patch('booking.urllib.request.getproxies', return_value={}), \
                     patch('booking.http.client.HTTPSConnection', side_effect=[stale, fresh]) as connect:
                    result = Api(SESSION).call(endpoint, {})
                self.assertEqual(result, Reply(200, {'data': 123}))
                self.assertEqual(connect.call_count, 2)
                self.assertEqual(stale.request.call_count, 1)
                self.assertEqual(fresh.request.call_count, 1)
                self.assertEqual(stale.request.call_args, fresh.request.call_args)
                stale.close.assert_called_once()

    def test_second_read_only_connection_failure_stops(self):
        first, second = Mock(), Mock()
        for connection in (first, second):
            connection.request.side_effect = http.client.RemoteDisconnected('no reply')
        with patch('booking.urllib.request.getproxies', return_value={}), \
             patch('booking.http.client.HTTPSConnection', side_effect=[first, second]) as connect:
            with self.assertRaisesRegex(BookingError, '网络请求未得到完整回复'):
                Api(SESSION).call('post_order_info', {})
        self.assertEqual(connect.call_count, 2)
        for connection in (first, second):
            self.assertEqual(connection.request.call_count, 1)
            connection.close.assert_called_once()

    def test_non_read_only_operations_are_never_resent(self):
        for endpoint in ('creat_book_info', 'creat_order', 'cancel_book', 'unknown'):
            with self.subTest(endpoint=endpoint):
                connection = Mock()
                connection.request.side_effect = http.client.RemoteDisconnected('no reply')
                with patch('booking.urllib.request.getproxies', return_value={}), \
                     patch('booking.http.client.HTTPSConnection', return_value=connection) as connect:
                    with self.assertRaises(BookingError):
                        Api(SESSION).call(endpoint, {})
                self.assertEqual(connect.call_count, 1)
                self.assertEqual(connection.request.call_count, 1)

    def test_http_failure_is_not_retried(self):
        connection = Mock()
        connection.getresponse.return_value = Mock(status=500)
        connection.getresponse.return_value.read.return_value = b'{"status":500}'
        with patch('booking.urllib.request.getproxies', return_value={}), \
             patch('booking.http.client.HTTPSConnection', return_value=connection):
            result = Api(SESSION).call('post_venue_info', {})
        self.assertEqual(result.status, 500)
        self.assertEqual(connection.request.call_count, 1)

    def test_gateway_errors_are_recoverable_only_for_read_queries(self):
        for endpoint in ('post_site_situation','creat_book_info','creat_order'):
            with self.subTest(endpoint=endpoint):
                connection=Mock()
                connection.getresponse.return_value=Mock(status=503)
                connection.getresponse.return_value.read.return_value=b'{"message":"PRIVATE_RESPONSE"}'
                with patch('booking.urllib.request.getproxies',return_value={}), \
                     patch('booking.http.client.HTTPSConnection',return_value=connection):
                    api=Api(SESSION)
                    if endpoint=='post_site_situation':
                        with self.assertRaises(ReadServiceError):api.call(endpoint,{})
                    else:self.assertEqual(api.call(endpoint,{}).status,503)
                self.assertEqual(connection.request.call_count,1)
                self.assertNotIn('PRIVATE_RESPONSE',str(api.timings))

    def test_denial_keeps_status_and_never_becomes_an_automatic_retry(self):
        for status in (401,403):
            with self.subTest(status=status):
                connection=Mock()
                connection.getresponse.return_value=Mock(status=status)
                connection.getresponse.return_value.read.return_value=b'PRIVATE_RESPONSE'
                with patch('booking.urllib.request.getproxies',return_value={}), \
                     patch('booking.http.client.HTTPSConnection',return_value=connection):
                    with self.assertRaises(AuthenticationError) as caught:Api(SESSION).call('creat_book_info',{})
                self.assertEqual(caught.exception.status,status)
                self.assertEqual(connection.request.call_count,1)

    def test_unparseable_response_reports_status_without_response_body(self):
        connection = Mock()
        connection.getresponse.return_value = Mock(status=502)
        connection.getresponse.return_value.read.return_value = b'<html>PRIVATE_RESPONSE</html>'
        with patch('booking.urllib.request.getproxies', return_value={}), \
             patch('booking.http.client.HTTPSConnection', return_value=connection):
            with self.assertRaises(BookingError) as caught:
                Api(SESSION).call('creat_book_info', {})
        self.assertIn('HTTP 502', str(caught.exception))
        self.assertNotIn('PRIVATE_RESPONSE', str(caught.exception))
        self.assertEqual(connection.request.call_count, 1)

    def test_booking_check_retains_controlled_cause_and_http_status(self):
        for reply in (BookingError('服务器返回了无法识别的页面（HTTP 502）'),
                      Reply(500, {'message': 'PRIVATE_RESPONSE'}),
                      Reply(201, {'message': 'PRIVATE_RESPONSE'})):
            with self.subTest(reply=reply):
                api = Mock(session=SESSION, timings=[])
                if isinstance(reply, Exception):
                    api.call.side_effect = reply
                    expected = Uncertain
                    status = 'HTTP 502'
                else:
                    api.call.return_value = reply
                    expected = BookingError
                    status = 'HTTP ' + str(reply.status)
                clock = Mock()
                clock.now.return_value = Plan().release.timestamp()
                trial = Trial(api, Plan(), clock=clock)
                with self.assertRaises(expected) as caught:
                    trial.attempt(META, [(1, 0)], prechecked=True)
                self.assertIn(status, str(caught.exception))
                self.assertNotIn('PRIVATE_RESPONSE', str(caught.exception))
                booking_calls = [item for item in api.call.call_args_list if item.args[0] == 'creat_book_info']
                self.assertEqual(len(booking_calls), 1)
                self.assertFalse(any(item.args[0] == 'creat_order' for item in api.call.call_args_list))


if __name__ == '__main__':
    unittest.main(verbosity=2)
