import unittest
from datetime import timedelta
from unittest.mock import Mock

from test_booking import Clock, FakeApi, META
from booking import BookingError, Plan, Trial


class RecoveryWindowTests(unittest.TestCase):
    def test_explicit_recovery_preserves_original_plan_and_creates_only_once(self):
        plan = Plan()
        api = FakeApi()
        trial = Trial(api, plan, clock=Clock(plan.release.timestamp() + 60),
                      recovery_until=plan.release + timedelta(minutes=10))
        result = trial.attempt(META, [(1, 0)])
        self.assertIs(trial.plan, plan)
        self.assertEqual(result['order_num'], 'test-order')
        creates = [payload['orderData'] for endpoint, payload in api.calls
                   if endpoint == 'creat_order']
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]['date'], plan.day)
        self.assertEqual((creates[0]['start_time'], creates[0]['end_time']),
                         (plan.start, plan.end))
        self.assertEqual(creates[0]['total_price'], 0)

    def test_default_window_still_rejects_late_attempt_without_requests(self):
        plan = Plan()
        api = FakeApi()
        trial = Trial(api, plan, clock=Clock(plan.release.timestamp() + 21))
        with self.assertRaises(BookingError):
            trial.attempt(META, [(1, 0)])
        self.assertEqual(api.calls, [])

    def test_recovery_rejects_unbounded_or_naive_deadline(self):
        plan = Plan()
        for deadline in (plan.release + timedelta(minutes=31),
                         plan.release - timedelta(seconds=1),
                         plan.release.replace(tzinfo=None)):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                Trial(FakeApi(), plan, recovery_until=deadline)

    def test_check_finishing_after_deadline_cannot_mark_or_create(self):
        plan = Plan()
        deadline = plan.release + timedelta(minutes=10)
        clock = Clock(deadline.timestamp() - 1)
        api = FakeApi()
        original_call = api.call

        def delayed_check(endpoint, payload=None, timeout=5):
            reply = original_call(endpoint, payload, timeout=timeout)
            if endpoint == 'creat_book_info':
                clock.value = deadline.timestamp() + 0.1
            return reply

        api.call = delayed_check
        mark = Mock()
        trial = Trial(api, plan, clock=clock, recovery_until=deadline,
                      before_create=mark)
        with self.assertRaises(BookingError):
            trial.attempt(META, [(1, 0)])
        mark.assert_not_called()
        self.assertNotIn('creat_order', [endpoint for endpoint, _ in api.calls])


if __name__ == '__main__':
    unittest.main()
