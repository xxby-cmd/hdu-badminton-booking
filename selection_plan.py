"""Exact selected cells and school quota rules, independent of Tk and networking."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time
from pathlib import Path
from booking import Plan, TZ, BookingError, CandidatePlanner, indices, order_date, order_is_cancelled, overlaps

@dataclass(frozen=True,order=True)
class SelectedSlot:
    start: str
    end: str
    site_id: int

@dataclass(frozen=True)
class SelectionPlan:
    day: str
    venue: str
    venue_type: str
    selections: tuple[SelectedSlot,...]
    execution_mode: str = 'release'
    execution_until: datetime | None = None
    account_hash: str | None = None
    max_price: float = 0
    allow_one_hour: bool = False

    @property
    def release(self):
        return datetime.combine(date.fromisoformat(self.day)-timedelta(days=2),time(20),TZ)
    @property
    def start(self):return min(s.start for s in self.selections)
    @property
    def end(self):return max(s.end for s in self.selections)
    @property
    def site_ids(self):return tuple(sorted({s.site_id for s in self.selections if s.site_id}))

    def groups(self):
        groups=[]
        for slot in sorted(self.selections):
            if groups and groups[-1][-1].site_id==slot.site_id and groups[-1][-1].end==slot.start:
                groups[-1].append(slot)
            else:groups.append([slot])
        return tuple(Plan(release=self.release,day=self.day,venue=self.venue,venue_type=self.venue_type,
                          start=g[0].start,end=g[-1].end,max_price=0,allow_one_hour=False,
                          site_ids=(g[0].site_id,) if g[0].site_id else ()) for g in groups)

def _minutes(text):
    try:
        value=time.fromisoformat(text)
        if value.strftime('%H:%M')!=text:raise ValueError()
        return value.hour*60+value.minute
    except (TypeError,ValueError):raise BookingError('选中时段格式无效') from None

def parse_selection(data):
    expected={'schema','day','venue','venue_type','max_price','selections','execution_mode','execution_until','account_hash'}
    if not isinstance(data,dict) or set(data)!=expected or type(data['schema']) is not int or data['schema']!=2:
        raise BookingError('选格计划格式或版本无效')
    try:
        day=date.fromisoformat(data['day'])
        if day.isoformat()!=data['day']:raise ValueError()
    except (TypeError,ValueError):raise BookingError('预约日期无效') from None
    venue=data['venue']
    if not isinstance(venue,str) or not venue.strip() or len(venue)>60 or any(ord(c)<32 for c in venue):
        raise BookingError('场馆名称无效')
    kind=data['venue_type']
    if kind not in ('badminton','tennis') or ('羽毛球' in venue and kind!='badminton') or ('网球' in venue and kind!='tennis'):
        raise BookingError('场馆与运动类型不一致')
    if type(data['max_price']) not in (int,float) or data['max_price']!=0:
        raise BookingError('本助手仅支持免费场地')
    rows=data['selections']
    if not isinstance(rows,list) or not 1<=len(rows)<=2:raise BookingError('每天同一场馆最多选择两个时段')
    slots=[]
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'site_id','start','end'}:raise BookingError('选中格子格式无效')
        if type(row['site_id']) is not int or not 0<=row['site_id']<=1000:raise BookingError('场地编号无效')
        if _minutes(row['start'])>=_minutes(row['end']):raise BookingError('时段结束必须晚于开始')
        slots.append(SelectedSlot(row['start'],row['end'],row['site_id']))
    slots=tuple(sorted(slots))
    if len(set(slots))!=len(slots):raise BookingError('不能重复选择同一个格子')
    if len(slots)==2 and slots[0].end>slots[1].start:
        raise BookingError('同一时间只能预约一片场地，请选择不重叠的时段')
    mode=data['execution_mode'];until=None
    if mode not in ('release','now'):raise BookingError('执行方式无效')
    if mode=='now':
        try:until=datetime.fromisoformat(data['execution_until'])
        except (ValueError,TypeError):raise BookingError('即时预约的有效期无效') from None
        if until.tzinfo is None:raise BookingError('即时预约的有效期必须有时区')
    elif data['execution_until'] is not None:raise BookingError('定时计划不能带即时有效期')
    account=data['account_hash']
    if account is not None and (not isinstance(account,str) or len(account)!=64 or any(c not in '0123456789abcdef' for c in account)):
        raise BookingError('选格账号校验标记无效')
    return SelectionPlan(data['day'],venue.strip(),kind,slots,mode,until,account)

def config_for_slots(day,venue,kind,slots,now=None,immediate=False,account_hash=None):
    now=now or datetime.now(TZ)
    return {'schema':2,'day':day,'venue':venue,'venue_type':kind,'max_price':0,
            'selections':[{'site_id':s.site_id,'start':s.start,'end':s.end} for s in sorted(slots)],
            'execution_mode':'now' if immediate else 'release',
            'execution_until':(now+timedelta(seconds=25)).isoformat() if immediate else None,
            'account_hash':account_hash}

def ensure_executable(plan,now=None,scheduled=False):
    now=now or datetime.now(TZ)
    if plan.execution_mode=='release':
        if now>=plan.release:raise BookingError('此日期已放场，请刷新排期后使用“立即尝试所选”')
    elif scheduled or plan.execution_until is None or not now<plan.execution_until<=now+timedelta(seconds=30):
        raise BookingError('即时预约确认已过期，请刷新排期后重新选择启动')
    if now>=datetime.combine(date.fromisoformat(plan.day),time.fromisoformat(plan.start),TZ):
        raise BookingError('所选时段已开始，不再提交')

def selected_as_plan(plan,slot):
    return Plan(release=plan.release,day=plan.day,venue=plan.venue,venue_type=plan.venue_type,
                start=slot.start,end=slot.end,max_price=0,allow_one_hour=False,site_ids=(slot.site_id,) if slot.site_id else ())

def validate_quota(plan,own,invited,meta,remaining=None):
    remaining=tuple(remaining if remaining is not None else plan.selections)
    for row in own+invited:
        if order_is_cancelled(row):continue
        try:
            date.fromisoformat(order_date(row))
            if not isinstance(row.get('venue_name'),str) or not row['venue_name'].strip():raise ValueError()
            if _minutes(row.get('start_time'))>=_minutes(row.get('end_time')):raise ValueError()
        except (ValueError,TypeError,BookingError):
            raise BookingError('已有预约数据不完整，无法确认时段名额，请刷新后核对') from None
    used=0
    for row in own:
        if order_is_cancelled(row):continue
        if order_date(row)==plan.day and row.get('venue_name')==plan.venue:
            try:
                prior=Plan(day=plan.day,venue=plan.venue,start=row['start_time'],end=row['end_time'])
                used+=len(indices(meta,prior))
            except (KeyError,BookingError,TypeError):
                raise BookingError('已有订单的时段数量无法核实，停止新增预约') from None
    if used+len(remaining)>2:raise BookingError('每天同一场馆最多两个时段，已有预约占用了部分名额')
    for row in own+invited:
        if order_is_cancelled(row):continue
        if any(overlaps(row,selected_as_plan(plan,slot)) for slot in remaining):
            raise BookingError('所选时间与已有预约或受邀记录重叠')
    return used

def validate_cells(plan,meta,stock,own,invited,now=None):
    validate_quota(plan,own,invited,meta)
    now=now or datetime.now(TZ)
    for slot in plan.selections:
        active=selected_as_plan(plan,slot)
        if len(indices(meta,active))!=1:raise BookingError('格子与学校时间表不一致，请刷新')
        if now>=datetime.combine(date.fromisoformat(plan.day),time.fromisoformat(slot.start),TZ):
            raise BookingError('所选时段已开始')
        options=CandidatePlanner(meta,active).options(stock,strict_price=True)
        if not any(not slot.site_id or o.site==slot.site_id for o in options):
            raise BookingError('所选格子已关闭、被预约或收费，请刷新后重新选择')

class GridSelection:
    def __init__(self,day,venue,kind,meta,stock,own=(),invited=(),now=None,blocked=(),reserved_count=0,replaceable=()):
        self.day,self.venue,self.kind=day,venue,kind
        self.meta,self.stock=meta,stock
        self.own,self.invited=list(own),list(invited)
        self.now=now or datetime.now(TZ);self.now_provider=(lambda:now) if now is not None else (lambda:datetime.now(TZ))
        self.selected=set();self.blocked=tuple(blocked);self.reserved_count=reserved_count;self.replaceable=tuple(replaceable)
        self._states={}
        times=meta['time_list']
        for row in stock['site_situation']:
            for i in range(len(times)-1):
                slot=SelectedSlot(times[i],times[i+1],row['site_id'])
                state='available'
                if (i in meta['rest_list'])^(i in row['oci_time_list']):state='未开放'
                if i in row['book_time_list']:state='已被预约'
                if self.now>=datetime.combine(date.fromisoformat(day),time.fromisoformat(slot.start),TZ):state='时段已开始'
                if state=='available' and any(start<slot.end and end>slot.start for start,end in self.blocked):
                    state='已有本地计划，请到历史记录查看或取消'
                if state=='available':
                    data=config_for_slots(day,venue,kind,[slot])
                    try:validate_cells(parse_selection(data),meta,stock,self.own,self.invited,self.now)
                    except BookingError as exc:state=str(exc)
                self._states[slot]=state
    def state(self,slot):
        if self.now_provider()>=datetime.combine(date.fromisoformat(self.day),time.fromisoformat(slot.start),TZ):
            return '时段已开始'
        if slot in self.selected:return 'selected'
        base=self._states.get(slot,'排期未知')
        if base!='available':return base
        proposed=[*self.selected,slot]
        replaced=sum(any(s.start<t.end and s.end>t.start for t in proposed) for s in self.replaceable)
        if len(proposed)+self.reserved_count-replaced>2:return '当天已有计划占用名额，最多两个时段'
        try:
            plan=parse_selection(config_for_slots(self.day,self.venue,self.kind,[*self.selected,slot]))
            validate_quota(plan,self.own,self.invited,self.meta)
        except BookingError as exc:return str(exc)
        return 'available'
    def existing(self,slot):
        return any(s.start<slot.end and s.end>slot.start for s in self.replaceable)
    def expire(self):
        expired={s for s in self.selected if self.now_provider()>=datetime.combine(
                 date.fromisoformat(self.day),time.fromisoformat(s.start),TZ)}
        self.selected.difference_update(expired)
        return bool(expired)
    def toggle(self,slot):
        if slot in self.selected:self.selected.remove(slot);return
        state=self.state(slot)
        if state!='available':raise BookingError(state)
        self.selected.add(slot)
    def plan(self):
        return parse_selection(config_for_slots(self.day,self.venue,self.kind,self.selected))
