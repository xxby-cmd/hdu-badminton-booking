import copy
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from booking import Reply,BookingError
import schedule_data
from schedule_data import validate_snapshot,ScheduleReader
from test_booking import META,availability,SESSION

def snapshot():
    return {'day':'2030-11-10','venue':'综合馆羽毛球','venue_type':'badminton',
            'meta':copy.deepcopy(META),'stock':availability(),'own':[],'invited':[]}
class ScheduleReadTests(unittest.TestCase):
    def test_bad_metadata_and_stock_are_rejected_without_gui(self):
        for corrupt in ('times','rest','site','indices','prices'):
            s=snapshot()
            if corrupt=='times':s['meta']['time_list']=['17:00','16:00']
            if corrupt=='rest':s['meta']['rest_list']=[999]
            if corrupt=='site':del s['stock']['site_situation'][0]['site_id']
            if corrupt=='indices':s['stock']['site_situation'][0]['oci_time_list']=[True]
            if corrupt=='prices':s['stock']['extra_list']['extra_price_list']=[float('nan')]
            with self.subTest(corrupt=corrupt),self.assertRaises(BookingError):validate_snapshot(s)
    def test_seven_days_cache_has_no_identity_or_token_and_only_queries(self):
        calls=[]
        def reply(endpoint,payload=None,timeout=5):
            calls.append(endpoint)
            if endpoint=='post_order_info':return Reply(200,{'order_info':[]})
            if endpoint=='post_venue_info':return Reply(200,META)
            if endpoint=='post_site_situation':return Reply(200,availability())
            raise AssertionError(endpoint)
        with tempfile.TemporaryDirectory() as private, \
             patch.object(schedule_data,'read_live_session',return_value=SESSION), \
             patch.object(schedule_data,'validate_session',return_value=(SESSION,{})), \
             patch.object(schedule_data.Api,'call',side_effect=reply), \
             patch.object(schedule_data.time,'sleep'):
            data=[];ScheduleReader(private).read_week('综合馆羽毛球','badminton',data.append,today=date(2030,11,10))
            self.assertEqual(len(data),7)
            self.assertEqual([s['day'] for s in data],['2030-11-'+str(i) for i in range(10,17)])
            self.assertEqual(len(calls),16)
            self.assertTrue(set(calls)<={'post_order_info','post_venue_info','post_site_situation'})
            for file in Path(private).rglob('*.json'):
                raw=file.read_text(encoding='utf-8')
                self.assertNotIn('TEST_ONLY',raw);self.assertNotIn('"token"',raw);self.assertNotIn('"openid"',raw)
if __name__=='__main__':unittest.main()
