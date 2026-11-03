"""Single-account, single-order trial. Readiness checks never create reservations."""
from dataclasses import dataclass,replace
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
import json
import http.client
import os
import random
import threading
import time
import urllib.request
import urllib.error
import sys

TZ = timezone(timedelta(hours=8))
BASE = 'https://sportmeta.hdu.edu.cn/book/client/'

@dataclass(frozen=True)
class Plan:
    release: datetime = datetime(2026,11,3,20,0,tzinfo=TZ)
    day: str = '2026-11-05'
    venue: str = '综合馆羽毛球'
    start: str = '19:00'
    end: str = '21:00'
    max_price: float = 0
    allow_one_hour: bool = True

def plan_for(name):
    if name=='trial': return Plan()
    if name=='formal': return replace(Plan(),release=Plan().release+timedelta(days=1),day='2026-11-06')
    raise ValueError('Unknown booking plan')

class BookingError(Exception): pass
class AuthenticationError(BookingError): pass
class Uncertain(BookingError): pass
class Stopped(BookingError): pass
class PriceError(BookingError): pass

@dataclass
class Reply:
    status: int
    data: dict

class Api:
    def __init__(self,session):
        self.session = session
        self.opener = urllib.request.build_opener()
        self.connection=None
        self.timings=[]

    def call(self,endpoint,payload=None):
        begin=time.perf_counter()
        request = urllib.request.Request(BASE+endpoint,
            data=json.dumps(payload,ensure_ascii=False).encode('utf-8') if payload is not None else None,
            headers={'Authorization':'Bearer '+self.session['token'],'Content-Type':'application/json'},
            method='POST' if payload is not None else 'GET')
        try:
            if urllib.request.getproxies().get('https'):
                with self.opener.open(request,timeout=5) as response:
                    status=response.status;raw=response.read(2*1024*1024)
            else:
                if self.connection is None:
                    self.connection=http.client.HTTPSConnection('sportmeta.hdu.edu.cn',timeout=5)
                self.connection.request(request.method,'/book/client/'+endpoint,body=request.data,
                    headers={'Authorization':'Bearer '+self.session['token'],'Content-Type':'application/json',
                        'User-Agent':'Python-urllib/'+str(sys.version_info.major)+'.'+str(sys.version_info.minor)})
                response=self.connection.getresponse()
                status=response.status;raw=response.read(2*1024*1024)
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read(128*1024)
        except (OSError,TimeoutError,urllib.error.URLError,http.client.HTTPException) as exc:
            if self.connection:self.connection.close();self.connection=None
            raise BookingError('网络请求未得到完整回复') from exc
        self.timings.append({'endpoint':endpoint,'status':status,'elapsed_ms':round((time.perf_counter()-begin)*1000,3)})
        if status in (401,403): raise AuthenticationError('登录失效或请求被拒绝，请在钉钉检查登录')
        try: data = json.loads(raw)
        except (ValueError,UnicodeError) as exc: raise BookingError('服务器返回了无法识别的页面') from exc
        if not isinstance(data,dict): raise BookingError('服务器返回格式发生变化')
        return Reply(status,data)

    def orders(self,invited=False):
        reply = self.call('post_order_info',{'openid':self.session['openid'],'is_invite':invited})
        result = reply.data.get('order_info')
        if reply.status != 200 or not isinstance(result,list): raise BookingError('无法读取我的预约')
        return result

class ServerClock:
    def __init__(self):
        self.anchor_time = time.time()
        self.anchor_mono = time.monotonic()
    def sync(self,api):
        start = time.monotonic()
        response = api.call('post_server_time')
        end = time.monotonic()
        value = response.data.get('data')
        if response.status != 200 or not isinstance(value,(int,float)) or value < 1e12:
            raise BookingError('无法校准学校服务器时间')
        # Use the received server timestamp as a lower bound. Adding half the
        # round trip can move the clock ahead when server processing is slow.
        self.anchor_time = value/1000
        self.last_round_trip_ms=(end-start)*1000
        self.anchor_mono = end
    def now(self): return self.anchor_time + time.monotonic()-self.anchor_mono

def indices(meta,plan):
    times = meta.get('time_list')
    if not isinstance(times,list) or len(times)!=len(set(times)) or times!=sorted(times):
        raise BookingError('时段表格式发生变化')
    try: start,end = times.index(plan.start),times.index(plan.end)
    except ValueError as exc: raise BookingError('场馆没有目标时段') from exc
    if start>=end: raise BookingError('预约起止时间错误')
    return list(range(start,end))

def candidates(meta,availability,plan):
    wanted = indices(meta,plan)
    rest = set(meta['rest_list'])
    extra = availability['extra_list']
    price = dict(zip(extra['extra_price_time'],extra['extra_price_list']))
    total = sum(float(price.get(i,meta['base_price'])) for i in wanted)
    if total<0 or total>plan.max_price: raise PriceError('当前费用超过本次免费试约范围')
    result=[]
    seen=set()
    for court in availability['site_situation']:
        court_id = court['site_id']
        if not isinstance(court_id,int) or court_id<1 or court_id in seen:
            raise BookingError('场地编号格式异常')
        seen.add(court_id)
        booked=set(court['book_time_list']); toggled=set(court['oci_time_list'])
        # Same rule as the school's current SiteTable: oci toggles the base schedule.
        if all(i not in booked and ((i in rest)==(i in toggled)) for i in wanted):
            result.append((court_id,total))
    return result

@dataclass(frozen=True)
class Option:
    site: int
    total: float
    plan: Plan

def booking_options(meta,availability,plan,shuffle=None):
    variants=[plan]
    if plan.allow_one_hour and (plan.start,plan.end)==('19:00','21:00'):
        variants.extend([replace(plan,end='20:00'),replace(plan,start='20:00')])
    options=[]
    for variant in variants:
        try:courts=candidates(meta,availability,variant)
        except PriceError:continue
        if shuffle:shuffle(courts)
        options.extend(Option(site,total,variant) for site,total in courts)
    return options

def order_date(order):
    raw = str(order.get('order_date',order.get('date',''))).split('T')[0].split(' ')[0]
    try: return date.fromisoformat(raw.replace('/','-')).isoformat()
    except ValueError: return raw

def overlaps(order,plan):
    return (order_date(order)==plan.day and str(order.get('start_time',''))<plan.end
        and str(order.get('end_time',''))>plan.start)

def exact_order(order,plan,site=None,number=None):
    return (order_date(order)==plan.day and order.get('venue_name')==plan.venue
        and order.get('start_time')==plan.start and order.get('end_time')==plan.end
        and bool(order.get('order_num')) and not order.get('is_cancelled',False)
        and (site is None or str(order.get('site_id'))==str(site))
        and (number is None or str(order.get('order_num'))==str(number)))

def payload_for(session,meta,plan,site,total):
    return {'orderData':{'openid':session['openid'],'nickname':session['nickname'],
        'phone':session['phone'],'date':plan.day,'venue_name':plan.venue,'venue_type':'badminton',
        'site_id':site,'total_price':total,'time_list':indices(meta,plan),
        'start_time':plan.start,'end_time':plan.end}}

def mark_submission(path,plan,site):
    """A durable one-shot marker prevents a restart from duplicating an uncertain write."""
    try: descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError as exc:
        raise BookingError('今晚已经发起过订单提交，请先在钉钉核对结果') from exc
    with os.fdopen(descriptor,'w',encoding='utf-8') as handle:
        json.dump({'day':plan.day,'start':plan.start,'end':plan.end,'site_id':site},handle,ensure_ascii=False)
        handle.flush();os.fsync(handle.fileno())

class Trial:
    def __init__(self,api,plan,publish=None,stop=None,clock=None,shuffle=None,before_create=None):
        self.api=api; self.plan=plan
        self.publish=publish or (lambda **event:None)
        self.stop=stop or threading.Event()
        self.clock=clock or ServerClock()
        self.shuffle=shuffle or random.SystemRandom().shuffle
        self.before_create=before_create or (lambda plan,site:None)
        self.first_request_time=None
        self.order_reply_at=None

    def emit(self,state,message,**extra):
        self.publish(state=state,message=message,**extra)

    def check_stop(self):
        if self.stop.is_set(): raise Stopped('脚本已停止')

    def read_meta(self):
        reply=self.api.call('post_venue_info',{'date':self.plan.day,'venue_name':self.plan.venue})
        if reply.status!=200: raise BookingError('场馆信息查询失败')
        indices(reply.data,self.plan)
        return reply.data

    def read_availability(self):
        reply=self.api.call('post_site_situation',{'date':self.plan.day,'venue_name':self.plan.venue,'is_permanent':False})
        if reply.status!=200: raise BookingError('场地状态查询失败')
        return reply.data

    def preflight(self):
        self.clock.sync(self.api)
        mine=self.api.orders(False)
        invited=self.api.orders(True)
        if any(overlaps(order,self.plan) for order in mine+invited):
            raise BookingError('你在目标时段已有预约或受邀记录，请先在钉钉核对')
        meta=self.read_meta()
        courts=booking_options(meta,self.read_availability(),self.plan,self.shuffle)
        self.emit('READY','账号、查询和时段已检查；等待今晚放场',
            candidates=len(courts),server_now=self.clock.now())
        return meta,courts

    def reconcile(self,site,number=None,plan=None):
        # An uncertain write is never retried: first check the authoritative order list.
        for delay in (0,.5,1):
            if delay: time.sleep(delay)
            try:
                for order in self.api.orders(False):
                    if exact_order(order,plan or self.plan,site,number): return order
            except BookingError: continue
        return None

    def finish(self,order,started):
        self.emit('CREATED_PENDING_INVITE','订单已在“我的预约”核实，请在十分钟内手动邀请同伴',
            order_num=str(order['order_num']),site_id=order.get('site_id'),
            booked_start=order['start_time'],booked_end=order['end_time'],
            elapsed_ms=round((time.monotonic()-started)*1000),
            release_to_first_request_ms=round((self.first_request_time-self.plan.release.timestamp())*1000,3) if self.first_request_time else None,
            start_to_order_reply_ms=round((self.order_reply_at-started)*1000,3) if self.order_reply_at else None,
            request_timings=getattr(self.api,'timings',[])[-6:])
        return order

    def attempt(self,meta,courts,prechecked=False):
        self.check_stop()
        if self.clock.now()<self.plan.release.timestamp(): raise BookingError('尚未到放场时间，停止提交')
        if self.clock.now()>self.plan.release.timestamp()+20: raise BookingError('已超过本次试约窗口，停止提交')
        # A fresh order check also prevents a manually created booking from being duplicated.
        if not prechecked and any(overlaps(order,self.plan) for order in self.api.orders(False)):
            raise BookingError('检测到你已预约此时段，停止重复提交')
        queue=[item if isinstance(item,Option) else Option(item[0],item[1],self.plan) for item in courts]
        started=time.monotonic()
        if not queue: raise BookingError('19:00–21:00 内没有可预约的整小时场地')
        tried=set();checks=0;refreshes=0
        while queue and checks<36:
            option=queue.pop(0);site,total=option.site,option.total;active=option.plan
            key=(site,active.start,active.end)
            if key in tried:continue
            tried.add(key)
            self.check_stop()
            if self.clock.now()>self.plan.release.timestamp()+20: break
            payload=payload_for(self.api.session,meta,active,site,total)
            self.emit('ATTEMPTING',f'正在尝试场地 {site}，{active.start}–{active.end}',site_id=site)
            checks+=1
            if self.first_request_time is None:self.first_request_time=self.clock.now()
            try: check=self.api.call('creat_book_info',payload)
            except BookingError as exc:
                raise Uncertain('预约检查未得到明确结果，本次停止；请查看钉钉我的预约') from exc
            if check.status==201:
                raise BookingError('预约检查未通过：'+str(check.data.get('message','请查看钉钉'))[:100])
            if check.status!=200: raise BookingError('预约检查返回异常，本次停止')
            conflict=check.data.get('conflict_times'); available=check.data.get('available_times')
            if not isinstance(conflict,list) or not isinstance(available,list):
                raise BookingError('预约检查返回格式发生变化，本次停止')
            if conflict:
                if refreshes<3:
                    refreshes+=1
                    queue=booking_options(meta,self.read_availability(),self.plan,self.shuffle)
                continue
            if available and not set(payload['orderData']['time_list']).issubset(set(available)):
                raise BookingError('服务器没有确认目标时段，本次停止')
            self.check_stop()
            self.before_create(active,site)
            self.emit('VERIFYING','正在提交并核对订单',site_id=site)
            try:
                created=self.api.call('creat_order',payload)
                self.order_reply_at=time.monotonic()
            except BookingError as exc:
                order=self.reconcile(site,plan=active)
                if order: return self.finish(order,started)
                raise Uncertain('提交回复不明确；已查订单但暂未确认，请在钉钉手动核对') from exc
            raw=created.data.get('data')
            number=raw.get('order_num') if isinstance(raw,dict) else None
            order=self.reconcile(site,number,active)
            if order: return self.finish(order,started)
            # Do not make a second create request after an ambiguous or malformed reply.
            raise Uncertain('提交后未能在“我的预约”核实；本次停止，请手动核对')
        raise BookingError('目标时段已被占用，本次没有创建订单')

    def wait_until(self,target):
        while self.clock.now()<target:
            self.check_stop()
            self.stop.wait(min(.2,max(.005,target-self.clock.now())))

    def run(self,refresh_session=None):
        try:
            if self.clock.now()>self.plan.release.timestamp()+20:
                raise BookingError('今晚的试约时间已过，请勿重复运行旧计划')
            meta,courts=self.preflight()
            self.wait_until(self.plan.release.timestamp()-10)
            self.check_stop()
            if refresh_session: self.api.session=refresh_session()
            meta,courts=self.preflight()
            self.wait_until(self.plan.release.timestamp()-1)
            self.clock.sync(self.api)  # Refresh time and warm the connection before release.
            self.wait_until(self.plan.release.timestamp())
            # The normal UI's creat_book_info call authoritatively checks release,
            # identity, account limits and conflicts; do not add read requests here.
            return self.attempt(meta,courts,prechecked=True)
        except Stopped as exc: self.emit('STOPPED',str(exc))
        except Uncertain as exc: self.emit('UNCERTAIN',str(exc))
        except (BookingError,KeyError,TypeError,ValueError) as exc:
            self.emit('FAILED',str(exc) if isinstance(exc,BookingError) else '返回数据格式变化，已停止，请在钉钉检查')
        except Exception:
            self.emit('FAILED','出现未预料的异常，脚本已停止，请在钉钉核对')
        return None
