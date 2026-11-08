"""Read-only seven-day inventory and per-user cache. No booking functions here."""
from datetime import datetime,timedelta
import hashlib
import json
from pathlib import Path
import time
from booking import Api,TZ,BookingError,checked_price,order_is_cancelled
from session import PRIVATE,read_live_session,validate_session
from plan_config import bookable_days

def order_fields(row):
    return {k:row[k] for k in ('order_date','date','venue_name','venue_type','start_time','end_time',
                               'is_cancel','is_cancelled','cancelled','status') if k in row}

def validate_snapshot(snapshot):
    try:
        day=datetime.fromisoformat(snapshot['day']).date()
        meta=snapshot['meta'];stock=snapshot['stock']
        times=meta['time_list'];rest=meta['rest_list']
        if not isinstance(times,list) or len(times)<2 or len(times)>100:raise ValueError()
        parsed=[datetime.strptime(t,'%H:%M') for t in times]
        if any(t.strftime('%H:%M')!=s for t,s in zip(parsed,times)) or parsed!=sorted(set(parsed)):raise ValueError()
        count=len(times)-1
        def valid(values):
            return isinstance(values,list) and all(type(i) is int and 0<=i<count for i in values) and len(values)==len(set(values))
        if not valid(rest):raise ValueError()
        checked_price(meta['base_price'],10**9)
        rows=stock['site_situation']
        if not isinstance(rows,list) or not rows or len(rows)>100:raise ValueError()
        sites=[]
        for row in rows:
            site=row['site_id']
            if type(site) is not int or site<1 or not valid(row['oci_time_list']) or not valid(row['book_time_list']):raise ValueError()
            sites.append(site)
        if len(sites)!=len(set(sites)):raise ValueError()
        extra=stock['extra_list'];extra_times=extra['extra_price_time'];prices=extra['extra_price_list']
        if not valid(extra_times) or not isinstance(prices,list) or len(extra_times)!=len(prices):raise ValueError()
        for price in prices:checked_price(price,10**9)
        for key in ('own','invited'):
            if not isinstance(snapshot[key],list):raise ValueError()
            for row in snapshot[key]:
                if not isinstance(row,dict):raise ValueError()
                order_is_cancelled(row)
        return snapshot
    except (KeyError,TypeError,ValueError,BookingError):
        raise BookingError('学校排期数据不完整或格式变化，请稍后刷新') from None

class ScheduleReader:
    def __init__(self,private=PRIVATE):
        self.private=Path(private)
    def read_week(self,venue,kind,emit,stop=None,today=None):
        credentials,_=validate_session(read_live_session())
        identity=hashlib.sha256(str(credentials['openid']).encode()).hexdigest()
        class ReadOnly(Api):
            def call(self,endpoint,*args,**kwargs):
                if endpoint not in ('post_order_info','post_venue_info','post_site_situation','choose_venue'):
                    raise AssertionError('Schedule reader cannot write')
                return super().call(endpoint,*args,**kwargs)
        api=ReadOnly(credentials)
        try:
            own=[order_fields(r) for r in api.orders(False)]
            invited=[order_fields(r) for r in api.orders(True)]
            start=today or datetime.fromisoformat(bookable_days()[0]).date()
            folder=self.private/'schedule-cache'/hashlib.sha256((venue+kind).encode()).hexdigest()[:16]
            folder.mkdir(parents=True,exist_ok=True)
            for offset in range(7):
                if stop and stop.is_set():return
                day=(start+timedelta(days=offset)).isoformat()
                meta=api.call('post_venue_info',{'date':day,'venue_name':venue})
                stock=api.call('post_site_situation',{'date':day,'venue_name':venue,'is_permanent':False})
                if meta.status!=200 or stock.status!=200:
                    raise BookingError('学校暂时无法返回完整七日排期')
                snapshot={'day':day,'venue':venue,'venue_type':kind,'meta':meta.data,'stock':stock.data,
                          'own':own,'invited':invited,'identity_hash':identity,'fetched_at':datetime.now(TZ).isoformat()}
                validate_snapshot(snapshot)
                temporary=folder/(day+'.tmp')
                temporary.write_text(json.dumps(snapshot,ensure_ascii=False),encoding='utf-8')
                temporary.replace(folder/(day+'.json'))
                emit(snapshot)
                if stop:stop.wait(.2)
                else:time.sleep(.2)
        finally:
            if api.connection:api.connection.close()
