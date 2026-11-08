"""Single-account, single-order trial. Readiness checks never create reservations."""
from dataclasses import dataclass,replace
from collections import deque
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
import json
import math
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
READ_ONLY_ENDPOINTS = frozenset({'post_server_time', 'post_order_info',
    'post_venue_info', 'post_site_situation', 'choose_venue'})

@dataclass(frozen=True)
class Plan:
    release: datetime = datetime(2026,11,3,20,0,tzinfo=TZ)
    day: str = '2026-11-05'
    venue: str = '综合馆羽毛球'
    start: str = '19:00'
    end: str = '21:00'
    max_price: float = 0
    allow_one_hour: bool = True
    venue_type: str = 'badminton'
    site_ids: tuple[int, ...] = ()

def plan_for(name):
    if name=='trial': return Plan()
    if name=='formal': return replace(Plan(),release=Plan().release+timedelta(days=1),day='2026-11-06')
    raise ValueError('Unknown booking plan')

class BookingError(Exception): pass
class AuthenticationError(BookingError):
    def __init__(self,message,status=None):
        super().__init__(message)
        self.status=status
class NetworkError(BookingError): pass
class ReadServiceError(NetworkError):
    def __init__(self,status):
        super().__init__(f'只读查询服务暂时不可用（HTTP {status}）')
        self.status,self.issue=status,'read_service_unavailable'
class Uncertain(BookingError): pass
class Stopped(BookingError): pass
class PriceError(BookingError): pass

class ResponseError(BookingError):
    def __init__(self,message,status,issue):
        super().__init__(message)
        self.status,self.issue=status,issue

def response_issue(status,data):
    message=data.get('message',data.get('error',''))
    if status==500 and isinstance(message,str) and 'self_book' in message and 'before assignment' in message:
        return 'uninitialized_self_book'
    if status==429:return 'rate_limited'
    if status in (500,502,503,504):return 'server_error'
    if status==201:return 'business_rejected'
    return None

@dataclass
class Reply:
    status: int
    data: dict
    issue: str | None = None

class Api:
    def __init__(self,session):
        self.session = session
        self.opener = urllib.request.build_opener()
        self.connection=None
        self.timings=[]

    def call(self,endpoint,payload=None,timeout=5):
        request = urllib.request.Request(BASE+endpoint,
            data=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode('utf-8') if payload is not None else None,
            headers={'Authorization':'Bearer '+self.session['token'],'Content-Type':'application/json'},
            method='POST' if payload is not None else 'GET')
        attempts = 2 if endpoint in READ_ONLY_ENDPOINTS else 1
        for attempt in range(attempts):
            begin=time.perf_counter()
            try:
                if urllib.request.getproxies().get('https'):
                    with self.opener.open(request,timeout=timeout) as response:
                        status=response.status;raw=response.read(2*1024*1024)
                else:
                    if self.connection is None:
                        self.connection=http.client.HTTPSConnection('sportmeta.hdu.edu.cn',timeout=timeout)
                    self.connection.timeout=timeout
                    if self.connection.sock is not None:
                        self.connection.sock.settimeout(timeout)
                    self.connection.request(request.method,'/book/client/'+endpoint,body=request.data,
                        headers={'Authorization':'Bearer '+self.session['token'],'Content-Type':'application/json',
                            'User-Agent':'Python-urllib/'+str(sys.version_info.major)+'.'+str(sys.version_info.minor)})
                    response=self.connection.getresponse()
                    status=response.status;raw=response.read(2*1024*1024)
                break
            except urllib.error.HTTPError as exc:
                status = exc.code
                raw = exc.read(128*1024)
                break
            except (OSError,TimeoutError,urllib.error.URLError,http.client.HTTPException) as exc:
                self.timings.append({'endpoint':endpoint,'error_type':type(exc).__name__,
                    'attempt':attempt+1,'elapsed_ms':round((time.perf_counter()-begin)*1000,3)})
                if self.connection:self.connection.close();self.connection=None
                if attempt + 1 == attempts:
                    raise NetworkError('网络请求未得到完整回复') from exc
        self.timings.append({'endpoint':endpoint,'status':status,'attempt':attempt+1,
            'elapsed_ms':round((time.perf_counter()-begin)*1000,3)})
        if status in (401,403):
            self.timings[-1]['issue']='authentication_required' if status==401 else 'forbidden'
            raise AuthenticationError(f'登录失效或请求被拒绝（HTTP {status}），请在钉钉检查登录',status)
        if endpoint in READ_ONLY_ENDPOINTS and status in (502,503,504):
            self.timings[-1]['issue']='read_service_unavailable'
            raise ReadServiceError(status)
        try: data = json.loads(raw)
        except (ValueError,UnicodeError) as exc:
            self.timings[-1]['issue']='invalid_json'
            raise ResponseError(f'服务器返回了无法识别的页面（HTTP {status}）',status,'invalid_json') from exc
        if not isinstance(data,dict):
            self.timings[-1]['issue']='invalid_shape'
            raise ResponseError(f'服务器返回格式发生变化（HTTP {status}）',status,'invalid_shape')
        issue=response_issue(status,data)
        if issue:self.timings[-1]['issue']=issue
        return Reply(status,data,issue)

    def orders(self,invited=False,timeout=5):
        reply = self.call('post_order_info',{'openid':self.session['openid'],'is_invite':invited},timeout=timeout)
        result = reply.data.get('order_info')
        if reply.status != 200 or not isinstance(result,list): raise BookingError('无法读取我的预约')
        return result

class ServerClock:
    def __init__(self):
        self.anchor_time = time.time()
        self.anchor_mono = time.monotonic()
    def sync(self,api,timeout=5):
        start = time.monotonic()
        response = api.call('post_server_time',timeout=timeout)
        end = time.monotonic()
        value = response.data.get('data')
        if response.status != 200 or type(value) not in (int,float) or not math.isfinite(value) or value < 1e12:
            raise BookingError('无法校准学校服务器时间')
        # Use the received server timestamp as a lower bound. Adding half the
        # round trip can move the clock ahead when server processing is slow.
        rtt=(end-start)*1000
        previous=getattr(self,'best_round_trip_ms',None)
        fresh=previous is not None and 0<=end-self.anchor_mono<=90
        # A slow final response must not replace a recent, less uncertain sample.
        self.last_round_trip_ms=rtt
        self.last_sample_accepted=not fresh or rtt<=previous+2
        if self.last_sample_accepted:
            self.anchor_time=value/1000
            self.anchor_mono=end
            self.best_round_trip_ms=rtt
    def now(self): return self.anchor_time + time.monotonic()-self.anchor_mono

def _time_index(meta):
    times = meta.get('time_list')
    if (not isinstance(times,list) or not times or any(type(value) is not str for value in times)
            or len(times)!=len(set(times)) or times!=sorted(times)):
        raise BookingError('时段表格式发生变化')
    return {value:i for i,value in enumerate(times)}

def _indices_from(mapping,plan):
    try: start,end = mapping[plan.start],mapping[plan.end]
    except KeyError as exc: raise BookingError('场馆没有目标时段') from exc
    if start>=end: raise BookingError('预约起止时间错误')
    return tuple(range(start,end))

def indices(meta,plan):
    return list(_indices_from(_time_index(meta),plan))

def checked_price(value,limit):
    try:
        if isinstance(value,bool) or isinstance(limit,bool): raise ValueError()
        amount=float(value);maximum=float(limit)
    except (TypeError,ValueError,OverflowError) as exc:
        raise PriceError('费用数据格式异常，停止提交') from exc
    if not math.isfinite(amount) or not math.isfinite(maximum) or amount<0 or maximum<0 or amount>maximum:
        raise PriceError('当前费用超过本次免费试约范围，或费用数据无效')
    return amount

@dataclass(frozen=True)
class Option:
    site: int
    total: float
    plan: Plan

class CandidatePlanner:
    """Compile time/plan data once; scan each fresh court snapshot only once."""
    def __init__(self,meta,plan,include_fallback=True):
        self.meta,self.plan=meta,plan
        self.mapping=_time_index(meta)
        self.slot_count=len(self.mapping)-1
        self.bits=tuple(1<<i for i in range(self.slot_count))
        self.all_slots=(1<<self.slot_count)-1
        self.rest_mask=self.mask(meta['rest_list'])
        variants=[plan]
        if include_fallback and plan.allow_one_hour and (plan.start,plan.end)==('19:00','21:00'):
            variants.extend([replace(plan,end='20:00'),replace(plan,start='20:00')])
        self.variants=[(variant,_indices_from(self.mapping,variant)) for variant in variants]
        self.allowed_plans=frozenset(variant for variant,_ in self.variants)
        self.wanted={ (variant.start,variant.end):wanted for variant,wanted in self.variants }
        self.time_signature=tuple(meta['time_list'])

    def mask(self,values):
        if not isinstance(values,(list,tuple)):
            raise BookingError('场地时段数组格式异常')
        result=0
        for i in values:
            if type(i) is not int or not 0<=i<self.slot_count:
                raise BookingError('场地时段下标格式异常')
            result |= self.bits[i]
        return result

    def options(self,availability,shuffle=None,blocked=None,strict_price=False):
        extra=availability['extra_list']
        extra_times,extra_prices=extra['extra_price_time'],extra['extra_price_list']
        if not isinstance(extra_times,list) or not isinstance(extra_prices,list) or len(extra_times)!=len(extra_prices):
            raise BookingError('附加价格表不完整，停止提交')
        self.mask(extra_times)
        if len(set(extra_times))!=len(extra_times):
            raise BookingError('附加价格表含重复时段，停止提交')
        price=dict(zip(extra_times,extra_prices))
        priced=[]
        for variant,wanted in self.variants:
            try:
                total=checked_price(sum(checked_price(price.get(i,self.meta['base_price']),variant.max_price)
                                        for i in wanted),variant.max_price)
            except PriceError:
                if strict_price:raise
                continue
            priced.append((variant,sum(self.bits[i] for i in wanted),total))
        if not priced:return []
        buckets=[[] for _ in priced]
        seen=set();blocked=blocked or {}
        rows=availability['site_situation']
        if not isinstance(rows,list):raise BookingError('场地状态列表格式异常')
        for court in rows:
            site=court['site_id']
            if type(site) is not int or site<1 or site in seen:
                raise BookingError('场地编号格式异常')
            seen.add(site)
            if self.plan.site_ids and site not in self.plan.site_ids:
                continue
            # Frontend semantics: oci toggles the base schedule; bookings always win.
            free=(self.all_slots & ~(self.rest_mask ^ self.mask(court['oci_time_list']))
                  & ~self.mask(court['book_time_list']) & ~blocked.get(site,0))
            for bucket,(variant,wanted_mask,total) in zip(buckets,priced):
                if free & wanted_mask == wanted_mask:
                    bucket.append(Option(site,total,variant))
        result=[]
        for bucket in buckets:
            if shuffle:shuffle(bucket)
            result.extend(bucket)
        return result

    def payload(self,session,option):
        wanted=self.wanted.get((option.plan.start,option.plan.end))
        if wanted is None or option.plan not in self.allowed_plans:
            raise BookingError('候选计划不在本次授权范围内')
        return _payload_for(session,option.plan,option.site,option.total,wanted)

def candidates(meta,availability,plan):
    options=CandidatePlanner(meta,plan,include_fallback=False).options(availability,strict_price=True)
    return [(option.site,option.total) for option in options]

def booking_options(meta,availability,plan,shuffle=None):
    return CandidatePlanner(meta,plan).options(availability,shuffle)

def order_date(order):
    raw = str(order.get('order_date',order.get('date',''))).split('T')[0].split(' ')[0]
    try: return date.fromisoformat(raw.replace('/','-')).isoformat()
    except ValueError: return raw

def overlaps(order,plan):
    return (not order_is_cancelled(order) and order_date(order)==plan.day and str(order.get('start_time',''))<plan.end
        and str(order.get('end_time',''))>plan.start)

def order_is_cancelled(order):
    value=order.get('is_cancelled',False)
    if type(value) is bool: return value
    if type(value) is int and value in (0,1): return bool(value)
    raise BookingError('订单状态格式无法确认')

def exact_order(order,plan,site=None,number=None):
    matches=(order_date(order)==plan.day and order.get('venue_name')==plan.venue
        and order.get('start_time')==plan.start and order.get('end_time')==plan.end
        and type(order.get('order_num')) in (str,int) and bool(order.get('order_num'))
        and not order_is_cancelled(order)
        and (site is None or str(order.get('site_id'))==str(site))
        and (number is None or str(order.get('order_num'))==str(number)))
    if not matches or order.get('venue_type')!=plan.venue_type:return False
    if plan.site_ids and str(order.get('site_id')) not in {str(i) for i in plan.site_ids}:return False
    try:checked_price(order.get('total_price'),plan.max_price)
    except PriceError:return False
    return True

def payload_for(session,meta,plan,site,total):
    return _payload_for(session,plan,site,total,indices(meta,plan))

def _payload_for(session,plan,site,total,wanted):
    total=checked_price(total,plan.max_price)
    if type(site) is not int or site<1:
        raise BookingError('场地编号格式异常，停止提交')
    if plan.site_ids and site not in plan.site_ids:
        raise BookingError('场地不在本次选定范围内')
    if not isinstance(plan.venue_type,str) or not plan.venue_type.strip():
        raise BookingError('体育项目类型缺失，停止提交')
    return {'orderData':{'openid':session['openid'],'nickname':session['nickname'],
        'phone':session['phone'],'date':plan.day,'venue_name':plan.venue,'venue_type':plan.venue_type,
        'site_id':site,'total_price':total,'time_list':list(wanted),
        'start_time':plan.start,'end_time':plan.end}}

def mark_submission(path,plan,site,stage='create_reserved'):
    """A durable one-shot marker prevents a restart from duplicating an uncertain write."""
    try: descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError as exc:
        raise BookingError('今晚已经发起过订单提交，请先在钉钉核对结果') from exc
    with os.fdopen(descriptor,'w',encoding='utf-8') as handle:
        json.dump({'day':plan.day,'start':plan.start,'end':plan.end,'site_id':site,
                   'stage':stage},handle,ensure_ascii=False)
        handle.flush();os.fsync(handle.fileno())

class Trial:
    # Preserve coverage of 12 courts across the full interval and both fallbacks.
    MAX_CHECKS=36
    MAX_REFRESHES=3
    RETRY_DELAY=.12
    CONFLICT_DELAY=.025

    def __init__(self,api,plan,publish=None,stop=None,clock=None,shuffle=None,before_create=None,
                 recovery_until=None,before_attempt=None,preflight_check=None,immediate_until=None):
        if not isinstance(plan.release,datetime) or plan.release.tzinfo is None:
            raise ValueError('放场时间必须带时区')
        self.api=api; self.plan=plan
        self.deadline=plan.release.timestamp()+20
        if recovery_until is not None:
            if not isinstance(recovery_until,datetime) or recovery_until.tzinfo is None:
                raise ValueError('恢复截止时间必须带时区')
            if not plan.release<=recovery_until<=plan.release+timedelta(minutes=30):
                raise ValueError('恢复期限必须在原放场后的30分钟内')
            self.deadline=recovery_until.timestamp()
        self.publish=publish or (lambda **event:None)
        self.stop=stop or threading.Event()
        self.clock=clock or ServerClock()
        if immediate_until is not None:
            now=self.clock.now()
            start=datetime.combine(date.fromisoformat(plan.day),datetime.strptime(plan.start,'%H:%M').time(),TZ).timestamp()
            if (recovery_until is not None or not isinstance(immediate_until,datetime) or immediate_until.tzinfo is None
                    or not plan.release.timestamp()<=now<start
                    or not now<immediate_until.timestamp()<=now+30):
                raise BookingError('即时预约确认无效或已过期，请刷新后重新确认')
            self.deadline=immediate_until.timestamp()
        self.shuffle=shuffle or random.SystemRandom().shuffle
        self.before_create=before_create or (lambda plan,site:None)
        self.before_attempt=before_attempt or (lambda:None)
        self.preflight_check=preflight_check
        self._armed=False
        self._planner=None
        self._prepared={}
        self._identity=None
        self._time_signature=None
        self.first_request_time=None
        self.order_reply_at=None

    def emit(self,state,message,**extra):
        extra.setdefault('request_timings',getattr(self.api,'timings',[])[-6:])
        self.publish(state=state,message=message,**extra)

    def check_stop(self):
        if self.stop.is_set(): raise Stopped('脚本已停止')

    def timeout(self,readonly=False):
        self.check_stop()
        now=self.clock.now()
        if not math.isfinite(now):raise BookingError('当前校时时间无效，停止请求')
        remaining=self.deadline-now
        if remaining<=0:raise BookingError('已超过本次尝试期限，停止新请求')
        # Read APIs may reconnect once; reserve time for both transport attempts.
        return min(1.0 if readonly else 5.0,remaining/(2 if readonly else 1))

    def arm(self):
        if not self._armed:
            self.check_stop()
            self.before_attempt()
            self._armed=True

    def prepare_payloads(self,meta,options):
        if self._planner is None or self._planner.meta is not meta:
            self._planner=CandidatePlanner(meta,self.plan)
        self._identity=tuple(self.api.session[key] for key in ('openid','nickname','phone'))
        self._time_signature=tuple(meta['time_list'])
        self._prepared={}
        for option in options:
            self._prepared[(option.site,option.plan.start,option.plan.end)]=self._planner.payload(self.api.session,option)

    def prepared_payload(self,meta,option):
        if type(option.site) is not int or option.site<1:
            raise BookingError('候选场地编号格式异常')
        if self._identity!=tuple(self.api.session[key] for key in ('openid','nickname','phone')):
            raise AuthenticationError('准备后账号信息发生变化，停止提交，请重新核对')
        if self._time_signature!=tuple(meta['time_list']):
            raise BookingError('准备后场馆时间表发生变化，停止提交')
        if option.plan not in self._planner.allowed_plans:
            raise BookingError('候选计划不在本次授权范围内')
        key=(option.site,option.plan.start,option.plan.end)
        if key not in self._prepared:
            self._prepared[key]=self._planner.payload(self.api.session,option)
        if self._prepared[key]['orderData']['total_price']!=checked_price(option.total,self.plan.max_price):
            raise PriceError('准备后候选费用发生变化，停止提交并重新核对')
        return self._prepared[key]

    def pause(self,seconds):
        self.check_stop()
        remaining=self.deadline-self.clock.now()
        if remaining<=0:return
        if self.stop.wait(min(seconds,remaining)):raise Stopped('脚本已停止')
        self.check_stop()

    def read_meta(self):
        reply=self.api.call('post_venue_info',{'date':self.plan.day,'venue_name':self.plan.venue},
                            timeout=self.timeout(readonly=True))
        if reply.status!=200: raise BookingError('场馆信息查询失败')
        indices(reply.data,self.plan)
        return reply.data

    def read_availability(self,timeout=None):
        reply=self.api.call('post_site_situation',{'date':self.plan.day,'venue_name':self.plan.venue,'is_permanent':False},
                            timeout=self.timeout(readonly=True) if timeout is None else timeout)
        if reply.status!=200: raise BookingError('场地状态查询失败')
        return reply.data

    def preflight(self):
        self.clock.sync(self.api,timeout=self.timeout(readonly=True))
        mine=self.api.orders(False,timeout=self.timeout(readonly=True))
        invited=self.api.orders(True,timeout=self.timeout(readonly=True))
        if any(overlaps(order,self.plan) for order in mine+invited):
            raise BookingError('你在目标时段已有预约或受邀记录，请先在钉钉核对')
        meta=self.read_meta()
        if self.preflight_check:self.preflight_check(mine,invited,meta)
        self._planner=CandidatePlanner(meta,self.plan)
        courts=self._planner.options(self.read_availability(),self.shuffle)
        self.prepare_payloads(meta,courts)
        self.emit('READY','账号、查询和时段已检查；等待今晚放场',
            candidates=len(courts),server_now=self.clock.now())
        return meta,courts

    def prepare(self):
        for attempt in range(3):
            try:return self.preflight()
            except NetworkError:
                if attempt==2:raise
                self.emit('RECOVERING','只读准备查询暂时断连，正在有界恢复',retry=attempt+1)
                self.pause(.2*(attempt+1))
        raise BookingError('准备阶段未得到结果')

    def diagnose_validation(self,status,issue):
        self.emit('DIAGNOSING','预约校验异常，正在只读核对；不会直接重发校验或创建',
                  stage='booking_check',http_status=status,issue=issue)
        details={'http_status':status,'issue':issue,'own_conflict':None,'invited_conflict':None,
                 'available_candidates':None,'diagnostic_errors':[]}
        for invited,key in ((False,'own_conflict'),(True,'invited_conflict')):
            try:
                rows=self.api.orders(invited,timeout=1)
                if not isinstance(rows,list):raise BookingError('订单列表格式无法确认')
                details[key]=any(overlaps(order,self.plan) for order in rows)
            except (BookingError,KeyError,TypeError,ValueError) as exc:
                details['diagnostic_errors'].append(type(exc).__name__)
        try:
            details['available_candidates']=len(self._planner.options(self.read_availability(timeout=1)))
        except (BookingError,KeyError,TypeError,ValueError) as exc:
            details['diagnostic_errors'].append(type(exc).__name__)
        self.emit('DIAGNOSING','异常核对完成；库存和订单为空不能证明校验没有临时状态，已停止后续写请求',
                  **details)

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
        if self.clock.now()>self.deadline: raise BookingError('已超过本次试约窗口，停止提交')
        # A fresh order check also prevents a manually created booking from being duplicated.
        if not prechecked and any(overlaps(order,self.plan) for order in self.api.orders(False,timeout=self.timeout(readonly=True))):
            raise BookingError('检测到你已预约此时段，停止重复提交')
        options=[item if isinstance(item,Option) else Option(item[0],item[1],self.plan) for item in courts]
        if self._planner is None or self._planner.meta is not meta:
            self.prepare_payloads(meta,options)
        elif self._identity!=tuple(self.api.session[key] for key in ('openid','nickname','phone')):
            raise AuthenticationError('准备后账号信息发生变化，停止提交，请重新核对')
        queue=deque(options)
        started=time.monotonic()
        tried=set();blocked={};checks=0;refreshes=0
        while checks<self.MAX_CHECKS:
            self.check_stop()
            if self.clock.now()>=self.deadline:
                raise BookingError('本次尝试期限已到，未再发新提交，请核对已有状态')
            if not queue:
                if refreshes>=self.MAX_REFRESHES:break
                self.pause(self.RETRY_DELAY*(refreshes+1))
                refreshes+=1
                try:fresh=self._planner.options(self.read_availability(),self.shuffle,blocked)
                except NetworkError:
                    self.emit('RECOVERING','只读空位查询暂时失败，正在期限内恢复',retry=refreshes)
                    continue
                queue=deque(option for option in fresh if (option.site,option.plan.start,option.plan.end) not in tried)
                continue
            option=queue.popleft();site,total=option.site,option.total;active=option.plan
            key=(site,active.start,active.end)
            if key in tried:continue
            wanted=self._planner.wanted.get((active.start,active.end))
            if wanted is None:raise BookingError('候选时间不在授权范围内')
            if blocked.get(site,0) & sum(self._planner.bits[i] for i in wanted):continue
            tried.add(key)
            self.check_stop()
            payload=self.prepared_payload(meta,option)
            self.arm()
            self.emit('ATTEMPTING',f'正在尝试场地 {site}，{active.start}–{active.end}',site_id=site)
            checks+=1
            if self.first_request_time is None:self.first_request_time=self.clock.now()
            try: check=self.api.call('creat_book_info',payload,timeout=self.timeout())
            except BookingError as exc:
                self.diagnose_validation(getattr(exc,'status',None),getattr(exc,'issue',type(exc).__name__))
                raise Uncertain(f'预约检查未得到明确结果：{exc}；本次停止，请查看钉钉我的预约') from exc
            if check.status==201:
                raise BookingError('预约检查未通过（HTTP 201），请在钉钉查看原因')
            if check.status!=200:
                self.diagnose_validation(check.status,getattr(check,'issue',None) or response_issue(check.status,check.data))
                raise BookingError(f'预约检查返回异常（HTTP {check.status}），本次停止；已留存脱敏诊断')
            conflict=check.data.get('conflict_times'); available=check.data.get('available_times')
            wanted_set=set(payload['orderData']['time_list'])
            if (not isinstance(conflict,list) or not isinstance(available,list)
                    or any(type(i) is not int or i not in wanted_set for i in conflict)
                    or any(type(i) is not int or not 0<=i<self._planner.slot_count for i in available)
                    or set(conflict)&set(available)):
                raise BookingError(f'预约检查返回格式发生变化（HTTP {check.status}），本次停止')
            if conflict:
                blocked[site]=blocked.get(site,0)|self._planner.mask(conflict)
                self.emit('RECOVERING','学校明确返回时段冲突，已排除对应场地时段，继续选择授权候选',
                          check_number=checks,conflict_slots=conflict,site_id=site)
                self.pause(self.CONFLICT_DELAY)
                continue
            if available and not set(payload['orderData']['time_list']).issubset(set(available)):
                raise BookingError('服务器没有确认目标时段，本次停止')
            self.check_stop()
            if self.clock.now()>self.deadline:
                raise BookingError('已超过本次试约窗口，停止提交')
            self.before_create(active,site)
            self.emit('VERIFYING','正在提交并核对订单',site_id=site)
            try:
                created=self.api.call('creat_order',payload,timeout=self.timeout())
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
        if checks>=self.MAX_CHECKS:raise BookingError('已达到本次校验次数上限，停止后续请求')
        raise BookingError('已在授权时段内核对候选，但没有可继续尝试的空位；未创建订单')

    def wait_until(self,target):
        while self.clock.now()<target:
            self.check_stop()
            self.stop.wait(min(.2,max(.005,target-self.clock.now())))

    def run(self,refresh_session=None):
        try:
            if self.clock.now()>self.deadline:
                raise BookingError('今晚的试约时间已过，请勿重复运行旧计划')
            meta,courts=self.prepare()
            if self.clock.now()<self.plan.release.timestamp()-60:
                self.wait_until(self.plan.release.timestamp()-60)
                self.check_stop()
                if refresh_session: self.api.session=refresh_session()
                meta,courts=self.prepare()
            self.wait_until(self.plan.release.timestamp()-3)
            try:
                self.clock.sync(self.api,timeout=.4)
            except NetworkError:
                # The successful preflight(s) synced this run's clock already.
                # A final transport failure must not discard that time anchor.
                pass
            self.arm()
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
