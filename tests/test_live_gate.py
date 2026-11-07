from pathlib import Path
from datetime import datetime
import json
import tempfile
import unittest
from unittest.mock import patch

from booking import BookingError, Reply, TZ
import tennis_test


class LiveGateTests(unittest.TestCase):
    def test_catalog_planning_skips_paid_slots_without_any_write_requests(self):
        class Catalog:
            calls = []
            def call(self, endpoint, payload):
                self.calls.append(endpoint)
                if endpoint == 'choose_venue': return Reply(200, {'venue_list': [{'venue_name': 'TEST_TENNIS'}]})
                if endpoint == 'post_venue_info': return Reply(200, {
                    'time_list': ['10:00', '11:00', '12:00', '13:00'], 'rest_list': [],
                    'base_price': 0, 'days_can_appoint': 3})
                if endpoint == 'post_site_situation': return Reply(200, {
                    'site_situation': [{'site_id': 1, 'book_time_list': [], 'oci_time_list': []}],
                    'extra_list': {'extra_price_time': [0], 'extra_price_list': [10]}})
                raise AssertionError('Unexpected write')
        catalog = Catalog()
        plan, _, option = tennis_test.choose_one_free_slot(catalog, '2026-11-04', '2026-11-03')
        self.assertEqual((plan.start, plan.end, plan.venue_type, option.total), ('11:00', '13:00', 'tennis', 0))
        self.assertEqual(catalog.calls, ['choose_venue', 'post_venue_info', 'post_site_situation'])

    def test_missing_simulation_never_reaches_account_or_real_api(self):
        with patch.object(tennis_test, 'require_current_simulation', side_effect=BookingError('NOT_VERIFIED')), \
             patch.object(tennis_test, 'Api') as api:
            with self.assertRaises(BookingError): tennis_test.live_once()
        api.assert_not_called()

    def test_closed_date_is_never_probed_for_availability_or_writes(self):
        class Catalog:
            calls = []
            def call(self, endpoint, payload):
                self.calls.append(endpoint)
                if endpoint == 'choose_venue': return Reply(200, {'venue_list':[{'venue_name':'TEST_TENNIS'}]})
                if endpoint == 'post_venue_info': return Reply(200, {'days_can_appoint':2})
                raise AssertionError('Closed date must not be queried or booked')
        catalog = Catalog()
        with self.assertRaises(tennis_test.NoFreeTennisSlot):
            tennis_test.choose_one_free_slot(catalog, '2026-11-05', '2026-11-03')
        self.assertEqual(catalog.calls, ['choose_venue', 'post_venue_info'])

    def test_manual_invitation_readiness_is_required_even_with_confirmed_count(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'evidence.json'
            evidence = {'used_count':0,'restriction_at':4,'period':'TEST_WEEK',
                        'source':'TEST_SOURCE','observed_at':datetime.now(TZ).isoformat(),
                        'rule_confirmed':True,'count_confirmed':True,
                        'account_fingerprint':'TEST_ONLY'}
            path.write_text(json.dumps(evidence),encoding='utf-8')
            with patch.object(tennis_test,'EVIDENCE',path):
                with self.assertRaises(BookingError):tennis_test.cancellation_evidence()
                evidence['manual_invitation_ready'] = True
                path.write_text(json.dumps(evidence),encoding='utf-8')
                self.assertEqual(tennis_test.cancellation_evidence()[0].used_count,0)

    def test_missing_cancellation_count_or_pending_cancel_proof_stops_before_api(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(tennis_test, 'require_current_simulation', return_value={}), \
             patch.object(tennis_test, 'JOURNAL', Path(folder) / 'one.json'), \
             patch.object(tennis_test, 'EVIDENCE', Path(folder) / 'missing.json'), \
             patch.object(tennis_test, 'Api') as api:
            with self.assertRaises(BookingError): tennis_test.live_once()
        api.assert_not_called()

    def test_existing_test_cannot_be_rerun_even_if_cancel_was_successful(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'one.json'
            path.write_text('{"phase":"CANCELLED_VERIFIED"}', encoding='utf-8')
            with patch.object(tennis_test, 'require_current_simulation', return_value={}), \
                 patch.object(tennis_test, 'JOURNAL', path), patch.object(tennis_test, 'Api') as api:
                with self.assertRaises(BookingError): tennis_test.live_once()
            api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
