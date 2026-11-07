from dataclasses import replace
import math
import unittest

from booking import BookingError, Plan, PriceError, candidates, payload_for
from test_booking import META, SESSION, availability


class PriceAndSportTests(unittest.TestCase):
    def test_tennis_payload_uses_its_own_sport_without_changing_badminton(self):
        tennis = replace(Plan(), venue='TEST_TENNIS', venue_type='tennis')
        self.assertEqual(payload_for(SESSION, META, tennis, 1, 0)['orderData']['venue_type'], 'tennis')
        self.assertEqual(payload_for(SESSION, META, Plan(), 1, 0)['orderData']['venue_type'], 'badminton')

    def test_nonfinite_or_negative_prices_never_reach_a_submission_payload(self):
        for amount in (math.nan, math.inf, -math.inf, -1, 'NaN', True, None):
            with self.subTest(amount=amount), self.assertRaises(PriceError):
                payload_for(SESSION, META, Plan(), 1, amount)

    def test_invalid_slot_prices_cannot_cancel_each_other_out(self):
        data = availability()
        data['extra_list'] = {'extra_price_time': [11, 12], 'extra_price_list': [-1, 1]}
        with self.assertRaises(PriceError):
            candidates(META, data, Plan())

    def test_incomplete_or_ambiguous_price_table_stops(self):
        for extra in (
            {'extra_price_time': [11, 12], 'extra_price_list': [0]},
            {'extra_price_time': [11, 11], 'extra_price_list': [0, 0]},
            {'extra_price_time': [True], 'extra_price_list': [0]},
            {'extra_price_time': [100], 'extra_price_list': [0]},
        ):
            with self.subTest(extra=extra), self.assertRaises(BookingError):
                candidates(META, {**availability(), 'extra_list': extra}, Plan())

    def test_boolean_site_id_and_invalid_budget_are_rejected(self):
        with self.assertRaises(BookingError):
            payload_for(SESSION, META, Plan(), True, 0)
        for limit in (math.nan, math.inf, -1):
            with self.subTest(limit=limit), self.assertRaises(PriceError):
                payload_for(SESSION, META, replace(Plan(), max_price=limit), 1, 0)


if __name__ == '__main__':
    unittest.main()
