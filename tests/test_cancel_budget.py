from dataclasses import replace
from datetime import datetime, timedelta
import unittest

from booking import BookingError, TZ
from cancel_budget import CancellationBudget


class CancellationBudgetTests(unittest.TestCase):
    def test_only_verified_fresh_count_with_headroom_allows_one_cancellation(self):
        now = datetime.now(TZ)
        budget = CancellationBudget(2, 4, 'TEST_WEEK', 'TEST_SOURCE', now, True, True)
        self.assertEqual(budget.ensure_one_safe_cancellation(now)['used_count'], 2)
        for unsafe in (replace(budget, used_count=3), replace(budget, used_count=4),
                       replace(budget, restriction_at=3), replace(budget, count_confirmed=False),
                       replace(budget, rule_confirmed=False), replace(budget, period=''),
                       replace(budget, observed_at=now-timedelta(minutes=6)),
                       replace(budget, observed_at=now+timedelta(seconds=1))):
            with self.subTest(unsafe=unsafe), self.assertRaises(BookingError):
                unsafe.ensure_one_safe_cancellation(now)

    def test_unconfirmed_count_never_creates_a_real_test_journal(self):
        from pathlib import Path
        import tempfile
        from booking import Plan
        from once_journal import OnceTestJournal
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'one-test.json'
            with self.assertRaises(BookingError):
                OnceTestJournal(path).claim(replace(Plan(), venue_type='tennis'), 1)
            self.assertFalse(path.exists())

    def test_remaining_validity_must_cover_the_manual_invitation_wait(self):
        now = datetime.now(TZ)
        budget = CancellationBudget(0, 4, 'TEST_WEEK', 'TEST_SOURCE', now-timedelta(seconds=100), True, True)
        budget.ensure_one_safe_cancellation(now)
        with self.assertRaises(BookingError):
            budget.ensure_one_safe_cancellation(now, required_remaining=210)


if __name__ == '__main__':
    unittest.main()
