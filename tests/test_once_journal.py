from dataclasses import replace
from pathlib import Path
from datetime import datetime
import tempfile
import threading
import unittest
from unittest.mock import patch

from booking import BookingError, Plan, TZ
from cancel_budget import CancellationBudget
from once_journal import OnceTestJournal, TestAlreadyUsed


class OnceJournalTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'one-test.json'
        self.plan = replace(Plan(), venue='TEST_TENNIS', venue_type='tennis', allow_one_hour=False)

    def budget(self):
        return CancellationBudget(0, 4, 'TEST_PERIOD', 'TEST_EVIDENCE', datetime.now(TZ), True, True)

    def claimed(self):
        journal = OnceTestJournal(self.path)
        journal.claim(self.plan, 1, ['old-order'], self.budget())
        return journal

    def order(self, number='new-order'):
        return {'order_num': number, 'order_date': self.plan.day,
                'venue_name': self.plan.venue, 'site_id': 1,
                'start_time': self.plan.start, 'end_time': self.plan.end,
                'venue_type': 'tennis', 'total_price': 0, 'with_friends': True}

    def test_concurrent_claims_allow_only_one_test(self):
        results = []
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait()
            try:
                OnceTestJournal(self.path).claim(self.plan, 1, cancellation_budget=self.budget())
                results.append('allowed')
            except TestAlreadyUsed:
                results.append('blocked')

        workers = [threading.Thread(target=claim) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertCountEqual(results, ['allowed', 'blocked'])

    def test_500_or_restart_does_not_restore_the_test_allowance(self):
        journal = self.claimed()
        journal.reserve_request('creat_book_info')
        journal.record_failure('HTTP 500')
        with self.assertRaises(TestAlreadyUsed):
            OnceTestJournal(self.path).claim(self.plan, 1, cancellation_budget=self.budget())
        loaded = OnceTestJournal(self.path)
        loaded.load_readonly()
        with self.assertRaises(TestAlreadyUsed):
            loaded.reserve_request('creat_order')

    def test_each_write_is_reserved_before_send_and_cannot_repeat(self):
        journal = self.claimed()
        for endpoint in ('creat_book_info', 'creat_order'):
            if endpoint == 'creat_order': journal.record_validation_accepted()
            journal.reserve_request(endpoint)
            saved = OnceTestJournal(self.path).load_readonly()
            self.assertEqual(saved['requests'][endpoint], 1)
            with self.assertRaises(TestAlreadyUsed):
                journal.reserve_request(endpoint)

    def test_disk_error_keeps_a_request_latched_and_does_not_allow_resend(self):
        journal = self.claimed()
        journal.reserve_request('creat_book_info')
        journal.record_validation_accepted()
        with patch.object(journal, '_save', side_effect=OSError('TEST_ONLY')):
            with self.assertRaises(OSError):
                journal.reserve_request('creat_order')
        with self.assertRaises(TestAlreadyUsed):
            journal.reserve_request('creat_order')

    def test_cannot_cancel_old_or_mismatched_order(self):
        journal = self.claimed()
        with self.assertRaises(BookingError):
            journal.reserve_request('cancel_order', 'old-order')
        journal.reserve_request('creat_book_info')
        journal.record_validation_accepted()
        journal.reserve_request('creat_order')
        journal.record_create_reply('new-order')
        for order in (self.order('old-order'), {**self.order(), 'site_id': 2}):
            with self.subTest(order=order), self.assertRaises(BookingError):
                journal.record_order(order, self.plan)
        journal.record_order(self.order(), self.plan)
        with self.assertRaises(BookingError):
            journal.reserve_request('cancel_order', 'other-order')

    def test_cancellation_does_not_replenish_create_or_test_allowance(self):
        journal = self.claimed()
        journal.reserve_request('creat_book_info')
        journal.record_validation_accepted()
        journal.reserve_request('creat_order')
        journal.record_create_reply('new-order')
        journal.record_order(self.order(), self.plan)
        journal.reserve_request('cancel_order', 'new-order')
        journal.record_cancel_verified()
        self.assertFalse(journal.data['local_test_allowance_replenished_by_cancellation'])
        with self.assertRaises(TestAlreadyUsed):
            journal.reserve_request('creat_order')
        with self.assertRaises(TestAlreadyUsed):
            OnceTestJournal(self.path).claim(self.plan, 1, cancellation_budget=self.budget())

    def test_failed_validation_cannot_be_followed_by_creation_in_same_process(self):
        journal = self.claimed()
        journal.reserve_request('creat_book_info')
        journal.record_failure('HTTP 500')
        with self.assertRaises(BookingError):
            journal.reserve_request('creat_order')
        self.assertEqual(journal.data['requests']['creat_order'], 0)

    def test_order_identity_cannot_be_rebound_before_or_after_cancellation(self):
        journal = self.claimed()
        journal.reserve_request('creat_book_info')
        journal.record_validation_accepted()
        journal.reserve_request('creat_order')
        journal.record_create_reply('new-order')
        with self.assertRaises(BookingError):
            journal.record_create_reply('other-order')
        journal.record_order(self.order(), self.plan)
        with self.assertRaises(BookingError):
            journal.record_order(self.order('other-order'), self.plan)
        journal.reserve_request('cancel_order', 'new-order')
        journal.record_cancel_verified()
        with self.assertRaises(BookingError):
            journal.record_order(self.order(), self.plan)
        self.assertEqual(journal.data['order_num'], 'new-order')

    def test_pending_invitation_or_unconfirmed_free_tennis_order_cannot_cancel(self):
        journal = self.claimed()
        journal.reserve_request('creat_book_info')
        journal.record_validation_accepted()
        journal.reserve_request('creat_order')
        journal.record_create_reply('new-order')
        for changes in ({'total_price': 10}, {'venue_type': 'badminton'}, {'with_friends': 'true'}):
            with self.subTest(changes=changes), self.assertRaises(BookingError):
                journal.record_order({**self.order(), **changes}, self.plan)
        journal.record_order({**self.order(), 'with_friends': False}, self.plan)
        with self.assertRaises(BookingError): journal.reserve_request('cancel_order', 'new-order')
        journal.record_order(self.order(), self.plan)
        journal.reserve_request('cancel_order', 'new-order')
        self.assertEqual(journal.data['requests']['cancel_order'], 1)


if __name__ == '__main__':
    unittest.main()
