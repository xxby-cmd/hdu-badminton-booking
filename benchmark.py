"""Local mocks plus optional read-only network measurements; never books a court."""
from datetime import datetime
from pathlib import Path
import argparse
import json
import statistics
import time
import uuid
from booking import Api,Plan,Trial,Reply,mark_submission,TZ

META={'time_list':['08:00','09:00','10:00','11:00','11:40','13:20','14:00','15:00','16:00','17:00','18:00','19:00','20:00','21:00'],
 'rest_list':[0,1,2,3,9,10,11,12],'base_price':0,'days_can_appoint':3}
ORDER={'order_num':'SIMULATION_ONLY','order_date':'2026-11-05','venue_name':'综合馆羽毛球',
 'venue_type':'badminton','total_price':0,'site_id':1,'start_time':'19:00','end_time':'21:00'}

class MockApi:
    session={'token':'SIMULATION_ONLY','openid':'SIMULATION_ONLY','nickname':'SIMULATION_ONLY','phone':'SIMULATION_ONLY'}
    def orders(self,invited=False,timeout=5):return [dict(ORDER)]
    def call(self,endpoint,payload=None,timeout=5):
        if endpoint=='creat_book_info':return Reply(200,{'conflict_times':[],'available_times':[11,12]})
        if endpoint=='creat_order':return Reply(200,{'data':{'order_num':'SIMULATION_ONLY'}})
        raise AssertionError('Unexpected mock endpoint')

class Clock:
    def now(self):return Plan().release.timestamp()

def summary(samples):
    ordered=sorted(samples)
    return {'samples':len(samples),'median_ms':round(statistics.median(samples),4),
        'p95_ms':round(ordered[min(len(ordered)-1,int(.95*len(ordered)))],4),
        'min_ms':round(min(samples),4),'max_ms':round(max(samples),4)}

def local_samples(count,journal=None):
    samples=[]
    for i in range(count):
        hook=(lambda active,site:mark_submission(journal/f'{i}.json',active,site)) if journal else None
        trial=Trial(MockApi(),Plan(),clock=Clock(),shuffle=lambda x:None,before_create=hook)
        start=time.perf_counter_ns()
        result=trial.attempt(META,[(1,0)],prechecked=True)
        assert result['order_num']=='SIMULATION_ONLY'
        samples.append((time.perf_counter_ns()-start)/1e6)
    return summary(samples)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--read-only-network',action='store_true')
    parser.add_argument('--output',default='benchmark-results.json')
    args=parser.parse_args()
    scratch=Path(__file__).resolve().parent/'.private/benchmarks'/uuid.uuid4().hex
    scratch.mkdir(parents=True)
    report={'measured_at':datetime.now(TZ).isoformat(),
        'local_mock_no_network':local_samples(5000),
        'local_mock_with_durable_marker_no_network':local_samples(200,scratch),
        'critical_booking_requests_before_order_creation':2,
        'live_booking_measured':False,
        'limitations':['Mock replies are immediate; they do not measure school processing.',
            'Read-only latency is measured off peak and is not booking latency.',
            'No manual-booking baseline has been measured.']}
    if args.read_only_network:
        from session import load_session
        session,_=load_session();api=Api(session)
        # The only live endpoints used here are time, venue information and own order queries.
        api.call('post_server_time')
        report['network_cold_time_query_ms']=api.timings[-1]['elapsed_ms']
        by_endpoint={name:[] for name in ('post_server_time','post_venue_info','post_order_info')}
        for _ in range(5):
            for endpoint in by_endpoint:
                if endpoint=='post_server_time':reply=api.call(endpoint)
                elif endpoint=='post_venue_info':reply=api.call(endpoint,{'date':Plan().day,'venue_name':Plan().venue})
                else:reply=api.call(endpoint,{'openid':session['openid'],'is_invite':False})
                assert reply.status==200
                by_endpoint[endpoint].append(api.timings[-1]['elapsed_ms'])
        report['network_read_only_warm_ms']={name:summary(samples) for name,samples in by_endpoint.items()}
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
