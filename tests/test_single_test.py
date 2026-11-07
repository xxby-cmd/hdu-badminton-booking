from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from booking import AuthenticationError, BookingError, NetworkError, Option, Plan, Reply, TZ, Uncertain, payload_for
from cancel_budget import CancellationBudget
from once_journal import OnceTestJournal, TestAlreadyUsed
from single_test import GuardedTestApi, SingleTestFlow
from test_booking import Clock, SESSION


META = {'time_list': ['10:00', '11:00', '12:00'], 'rest_list': [], 'base_price': 0}


class SimulatedSchool:
    session = SESSION

    def __init__(self, plan, fault=None):
        self.plan, self.fault = plan, fault
        self.calls, self.timings = [], []
        self.existing = {'order_num': 'old-order', 'order_date': plan.day,
                         'venue_name': 'OLD_VENUE', 'site_id': 2,
                         'start_time': '14:00', 'end_time': '15:00'}
        self.active = [dict(self.existing)]
        self.cancel_sent = False
        self.invitation_reads = 0

    def orders(self, invited=False, timeout=5):
        return self.call('post_order_info', {'is_invite': invited}, timeout=timeout).data['order_info']

    def call(self, endpoint, payload=None, timeout=5):
        self.calls.append((endpoint, payload))
        if endpoint == 'post_order_info':
            if self.cancel_sent and self.fault == 'cancel_query_unreadable':
                raise NetworkError('TEST_ONLY')
            if not payload.get('is_invite') and any(value['order_num'] == 'new-order' for value in self.active):
                self.invitation_reads += 1
                if self.fault == 'invitation_auto_cancel' and self.invitation_reads >= 2:
                    self.active = [value for value in self.active if value['order_num'] != 'new-order']
                elif self.invitation_reads >= 3 and self.fault != 'invitation_never':
                    for value in self.active:
                        if value['order_num'] == 'new-order':
                            value['with_friends'] = True
                            if self.fault == 'changed_price_after_invite': value['total_price'] = 10
            values = [] if payload.get('is_invite') else [dict(value) for value in self.active]
            return Reply(200, {'order_info': values})
        if endpoint == 'post_site_situation':
            return Reply(200, {'site_situation': [{'site_id': 1, 'book_time_list': [], 'oci_time_list': []}],
                               'extra_list': {'extra_price_time': [], 'extra_price_list': []}})
        if endpoint == 'creat_book_info':
            if self.fault == '500': return Reply(500, {'message': "local variable 'self_book' referenced before assignment"})
            if self.fault == 'auth': raise AuthenticationError('HTTP 403')
            if self.fault == 'html': raise BookingError('服务器返回了无法识别的页面（HTTP 502）')
            if self.fault == 'timeout': raise NetworkError('TEST_ONLY')
            if self.fault == 'conflict': return Reply(200, {'conflict_times': [0], 'available_times': []})
            return Reply(200, {'conflict_times': [], 'available_times': payload['orderData']['time_list']})
        if endpoint == 'creat_order':
            data = payload['orderData']
            self.active.append({'order_num': 'new-order', 'order_date': data['date'],
                                'venue_name': data['venue_name'], 'venue_type': data['venue_type'],
                                'site_id': data['site_id'], 'start_time': data['start_time'],
                                'end_time': data['end_time'], 'total_price': data['total_price'],
                                'with_friends': False})
            if self.fault == 'created_reply_lost': raise NetworkError('TEST_ONLY')
            number = 'wrong-number' if self.fault == 'wrong_create_number' else 'new-order'
            return Reply(200, {'data': {'order_num': number}})
        if endpoint == 'cancel_order':
            assert next(value for value in self.active if value['order_num'] == payload['order_num'])['with_friends'] is True
            self.cancel_sent = True
            if self.fault != 'cancel_denied':
                self.active = [value for value in self.active if value['order_num'] != payload['order_num']]
            if self.fault == 'cancel_reply_lost': raise NetworkError('TEST_ONLY')
            return Reply(201 if self.fault == 'cancel_denied' else 200, {'message': 'TEST_ONLY'})
        raise AssertionError('Unexpected simulated endpoint: ' + endpoint)


class SingleTestWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'one-test.json'
        self.plan = replace(Plan(), venue='TEST_TENNIS', venue_type='tennis',
                            start='10:00', end='12:00', allow_one_hour=False)

    def flow(self, fault=None):
        api = SimulatedSchool(self.plan, fault)
        journal = OnceTestJournal(self.path)
        budget = CancellationBudget(0, 4, 'TEST_PERIOD', 'TEST_SOURCE', datetime.now(TZ), True, True)
        elapsed, events = [0], []
        def sleep(delay): elapsed[0] += delay
        flow = SingleTestFlow(api, journal, self.plan, META, Option(1, 0, self.plan), budget,
                              clock=Clock(self.plan.release.timestamp()), sleep=sleep,
                              monotonic=lambda:elapsed[0], publish=lambda **event:events.append(event))
        flow.test_events = events
        return flow, api, journal

    def test_complete_create_verify_cancel_verify_affects_only_new_order(self):
        flow, api, journal = self.flow()
        result = flow.run()
        self.assertEqual(result['state'], 'CANCELLED_VERIFIED')
        self.assertEqual(api.active, [api.existing])
        self.assertEqual([endpoint for endpoint, _ in api.calls].count('creat_order'), 1)
        cancels = [payload for endpoint, payload in api.calls if endpoint == 'cancel_order']
        self.assertEqual(cancels, [{'order_num': 'new-order'}])
        self.assertEqual(journal.data['requests'], {'creat_book_info': 1, 'creat_order': 1, 'cancel_order': 1})
        self.assertTrue(journal.data['cancel_verified'])
        self.assertFalse(result['local_test_allowance_replenished'])
        self.assertEqual([event['state'] for event in flow.test_events],
                         ['WAITING_FOR_INVITATION', 'INVITATION_CONFIRMED'])

    def test_no_invitation_or_auto_cancellation_never_sends_cancel(self):
        for fault in ('invitation_never', 'invitation_auto_cancel', 'changed_price_after_invite'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                self.path = Path(folder) / 'one-test.json'
                flow, api, journal = self.flow(fault)
                with self.assertRaises(BookingError): flow.run()
                self.assertEqual(sum(endpoint == 'creat_order' for endpoint, _ in api.calls), 1)
                self.assertFalse(any(endpoint == 'cancel_order' for endpoint, _ in api.calls))
                self.assertFalse(journal.data['cancel_verified'])

    def test_budget_must_cover_invitation_wait_before_any_request(self):
        flow, api, _ = self.flow()
        flow.budget = replace(flow.budget, observed_at=datetime.now(TZ)-timedelta(seconds=100))
        with self.assertRaises(BookingError): flow.run()
        self.assertEqual(api.calls, [])
        self.assertFalse(self.path.exists())

    def test_budget_is_rechecked_after_validation_before_creation(self):
        flow, api, journal = self.flow()
        original_call = api.call
        def aging_call(endpoint, payload=None, timeout=5):
            reply = original_call(endpoint,payload,timeout)
            if endpoint == 'creat_book_info':
                flow.budget = replace(flow.budget,observed_at=datetime.now(TZ)-timedelta(seconds=100))
            return reply
        api.call = aging_call
        with self.assertRaises(BookingError):flow.run()
        self.assertFalse(any(endpoint == 'creat_order' for endpoint,_ in api.calls))
        self.assertEqual(journal.data['requests']['cancel_order'],0)

    def test_invitation_reply_after_wait_deadline_never_sends_cancellation(self):
        flow, api, journal = self.flow()
        elapsed = [0]
        flow.monotonic = lambda:elapsed[0]
        original_call = api.call
        def late_reply(endpoint, payload=None, timeout=5):
            reply = original_call(endpoint,payload,timeout)
            if endpoint == 'post_order_info' and api.invitation_reads >= 2:
                elapsed[0] = 181
                for item in reply.data['order_info']:
                    if item['order_num'] == 'new-order':item['with_friends'] = True
            return reply
        api.call = late_reply
        with self.assertRaises(Uncertain):flow.run()
        self.assertFalse(any(endpoint == 'cancel_order' for endpoint,_ in api.calls))
        self.assertFalse(journal.data['cancel_verified'])

    def test_single_hour_is_not_a_two_period_test(self):
        self.plan = replace(self.plan, end='11:00')
        flow, api, _ = self.flow()
        with self.assertRaises(BookingError): flow.run()
        self.assertEqual(api.calls, [])

    def test_cancellation_cutoff_blocks_test_before_claim_or_any_request(self):
        start = datetime.strptime(self.plan.day + ' ' + self.plan.start,
                                  '%Y-%m-%d %H:%M').replace(tzinfo=TZ).timestamp()
        for seconds_left in (3600, 3599, 0, -1):
            with self.subTest(seconds_left=seconds_left):
                flow, api, _ = self.flow()
                flow.clock = Clock(start - seconds_left)
                with self.assertRaises(BookingError): flow.run()
                self.assertEqual(api.calls, [])
                self.assertFalse(self.path.exists())

    def test_two_periods_use_one_order_and_one_cancellation(self):
        self.plan = replace(self.plan, end='12:00')
        flow, api, journal = self.flow()
        self.assertEqual(flow.run()['state'], 'CANCELLED_VERIFIED')
        creates = [payload for endpoint, payload in api.calls if endpoint == 'creat_order']
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]['orderData']['time_list'], [0, 1])
        self.assertEqual(journal.data['requests']['cancel_order'], 1)

    def test_cancellation_cutoff_is_rechecked_before_sending_cancel(self):
        flow, api, journal = self.flow()
        normal_call = api.call
        def advancing_call(endpoint, payload=None, timeout=5):
            reply = normal_call(endpoint, payload, timeout)
            if endpoint == 'creat_order':
                flow.clock.value = datetime.strptime(self.plan.day + ' ' + self.plan.start,
                    '%Y-%m-%d %H:%M').replace(tzinfo=TZ).timestamp() - 3600
            return reply
        api.call = advancing_call
        with self.assertRaises(BookingError): flow.run()
        self.assertEqual(sum(endpoint == 'creat_order' for endpoint, _ in api.calls), 1)
        self.assertFalse(any(endpoint == 'cancel_order' for endpoint, _ in api.calls))
        self.assertFalse(journal.data['cancel_verified'])

    def test_validation_failures_and_conflicts_never_create_or_cancel(self):
        for fault in ('500', 'auth', 'html', 'timeout', 'conflict'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                self.path = Path(folder) / 'one-test.json'
                flow, api, journal = self.flow(fault)
                with self.assertRaises(BookingError): flow.run()
                endpoints = [endpoint for endpoint, _ in api.calls]
                self.assertEqual(endpoints.count('creat_book_info'), 1)
                self.assertNotIn('creat_order', endpoints)
                self.assertNotIn('cancel_order', endpoints)
                self.assertFalse(journal.data['cancel_verified'])
                if fault == '500':
                    self.assertEqual(journal.data['last_reply']['signature'], 'uninitialized_self_book')

    def test_creation_reply_lost_or_wrong_number_never_creates_twice_or_cancels_guess(self):
        for fault in ('created_reply_lost', 'wrong_create_number'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                self.path = Path(folder) / 'one-test.json'
                flow, api, journal = self.flow(fault)
                with self.assertRaises(BookingError): flow.run()
                endpoints = [endpoint for endpoint, _ in api.calls]
                self.assertEqual(endpoints.count('creat_order'), 1)
                self.assertNotIn('cancel_order', endpoints)
                self.assertFalse(journal.data['cancel_verified'])

    def test_lost_cancel_reply_is_resolved_by_orders_without_resending(self):
        flow, api, _ = self.flow('cancel_reply_lost')
        self.assertEqual(flow.run()['state'], 'CANCELLED_VERIFIED')
        self.assertEqual(sum(endpoint == 'cancel_order' for endpoint, _ in api.calls), 1)

    def test_denied_cancel_and_unreadable_queries_never_claim_cancellation(self):
        for fault in ('cancel_denied', 'cancel_query_unreadable'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                self.path = Path(folder) / 'one-test.json'
                flow, api, journal = self.flow(fault)
                with self.assertRaises(Uncertain): flow.run()
                self.assertFalse(journal.data['cancel_verified'])
                self.assertEqual(sum(endpoint == 'creat_order' for endpoint, _ in api.calls), 1)
                self.assertEqual(sum(endpoint == 'cancel_order' for endpoint, _ in api.calls), 1)

    def test_restart_and_changed_scope_cannot_send_another_order(self):
        flow, api, journal = self.flow()
        flow.run()
        again, second_api, _ = self.flow()
        with self.assertRaises(TestAlreadyUsed): again.run()
        self.assertFalse(any(endpoint in ('creat_book_info', 'creat_order', 'cancel_order')
                             for endpoint, _ in second_api.calls))
        expected = payload_for(api.session, META, self.plan, 1, 0)
        guard = GuardedTestApi(api, journal, expected)
        before = len(api.calls)
        with self.assertRaises(BookingError):
            guard.call('creat_order', {**expected, 'extra': 'CHANGED'})
        self.assertEqual(len(api.calls), before)


if __name__ == '__main__':
    unittest.main()
