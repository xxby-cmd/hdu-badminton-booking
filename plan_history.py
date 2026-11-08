"""Persistent local-plan history and time-based admission, independent of court IDs."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from booking import BookingError,TZ,indices,order_date
from plan_config import parse_config,load_config
from selection_plan import SelectionPlan,SelectedSlot,config_for_slots

PENDING={'scheduled','waiting','starting','updating','registering'}
BLOCKING=PENDING|{'running','stopping','uncertain','confirmed','partial'}
LABELS={'scheduled':'待定时启动','waiting':'正在等待放场','starting':'正在启动',
        'updating':'正在更新','registering':'正在登记','running':'正在执行','stopping':'正在停止',
        'uncertain':'结果待核实','confirmed':'学校订单已核实','partial':'部分已完成',
        'failed':'未完成','stopped':'已停止','deleted':'已删除本地计划','superseded':'已被后续计划替换'}

class ReplaceRequired(BookingError):
    def __init__(self,record):
        self.record=record
        super().__init__('该日期时段已有本地计划，换场地也属于同一时段。是否覆盖原计划？')

def slots_for(data,meta):
    plan=parse_config(data)
    if isinstance(plan,SelectionPlan):return list(plan.selections)
    if len(plan.site_ids)>1:raise BookingError('旧计划含多片候选，请在历史中删除后重新选择')
    site=plan.site_ids[0] if plan.site_ids else 0
    return [SelectedSlot(meta['time_list'][i],meta['time_list'][i+1],site) for i in indices(meta,plan)]

def spans(data):
    plan=parse_config(data)
    if isinstance(plan,SelectionPlan):return [(s.start,s.end) for s in plan.selections]
    return [(plan.start,plan.end)]

def intersects(left,right):
    return any(a<d and b>c for a,b in left for c,d in right)

class PlanHistory:
    def __init__(self,private):
        self.private=Path(private);self.path=self.private/'plan-history.json'
        self.lock_path=self.private/'plan-history.lock'
    @contextmanager
    def lock(self):
        self.private.mkdir(parents=True,exist_ok=True)
        with self.lock_path.open('a+b') as lock:
            if self.lock_path.stat().st_size==0:lock.write(b'0');lock.flush()
            acquired=False
            try:
                if os.name=='nt':
                    import msvcrt
                    for _ in range(40):
                        try:lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1);acquired=True;break
                        except OSError:time.sleep(.05)
                    if not acquired:raise BookingError('其他窗口正在更新计划，请稍后重试')
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(),fcntl.LOCK_EX);acquired=True
                yield
            finally:
                if acquired:
                    if os.name=='nt':lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
                    else:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)
    def load(self):
        if not self.path.exists():return {'schema':1,'records':[],'hidden_school':[]}
        try:
            data=json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('schema')!=1 or not isinstance(data['records'],list):raise ValueError()
            return data
        except (OSError,ValueError,TypeError,KeyError):
            raise BookingError('本地历史记录无法读取，已禁止新增计划，请保留文件并检查') from None
    def save(self,data):
        temporary=self.path.with_suffix('.tmp')
        with temporary.open('w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(temporary,self.path)
    def records(self,include_deleted=False):
        with self.lock():data=self.load()
        return [r for r in data['records'] if include_deleted or r['status']!='deleted']
    def find(self,record_id):
        return next((r for r in self.records(True) if r['id']==record_id),None)
    def import_task(self,task):
        cfg=task.get('config')
        if not cfg or not task.get('enabled'):return
        data,plan=load_config(cfg,task.get('sha256'))
        digest=hashlib.sha256(Path(cfg).read_bytes()).hexdigest()
        with self.lock():
            document=self.load();rows=document['records']
            same=next((r for r in reversed(rows) if r.get('task_name')==task['name'] and r.get('config_sha256')==digest),None)
            if same:
                if same.get('registration_uncertain') and same['status']=='uncertain':
                    same['status']='scheduled' if datetime.now(TZ)<plan.release else 'uncertain'
                    same['registration_uncertain']=False
                    same['message']='已对照系统任务核实当前配置'
                    self.save(document)
                if (same['status'] in ('stopped','failed') and same.get('mode')=='schedule'
                        and datetime.now(TZ)<plan.release.replace(hour=19)):
                    same['status']='scheduled';same['message']='系统仍保留尚未到时的定时任务'
                    self.save(document)
                return
            for row in rows:
                if row.get('task_name')==task['name'] and row['status'] in PENDING:row['status']='superseded'
            status='scheduled' if datetime.now(TZ)<plan.release else 'failed'
            rows.append({'id':uuid.uuid4().hex,'data':data,'status':status,'mode':'schedule',
                         'task_name':task['name'],'config':str(cfg),'config_sha256':digest,
                         'created_at':datetime.now(TZ).isoformat(),'updated_at':datetime.now(TZ).isoformat(),
                         'message':'从现有Windows任务导入；已过放场的记录不代表约成订单',
                         'pid':None})
            self.save(document)
    def blocks(self,day,venue,meta):
        blocked=[];replaceable=[];reserved=0
        for row in self.records():
            data=row['data']
            if row['status'] not in BLOCKING or data['day']!=day:continue
            if data['venue']==venue and row['status']=='scheduled':
                try:cells=slots_for(data,meta)
                except BookingError:blocked.extend(spans(data));reserved=2;continue
                replaceable.extend(cells);reserved+=len(cells)
            else:
                blocked.extend(spans(data))
                if data['venue']==venue:
                    try:reserved+=len(slots_for(data,meta))
                    except BookingError:reserved=2
        return blocked,replaceable,min(2,reserved)
    def apply(self,data,meta,mode,activate,replace=False,expected_id=None):
        """Reserve first, then side effect. Same-day additions merge into one task."""
        incoming=parse_config(data);new_cells=slots_for(data,meta)
        with self.lock():
            document=self.load();rows=document['records']
            active=[r for r in rows if r['status'] in BLOCKING and r['data']['day']==data['day']]
            if any(r['data']['venue']!=data['venue'] for r in active):
                raise BookingError('当天已有另一场馆计划，请先在历史中删除或处理原计划')
            if len(active)>1:raise BookingError('当天存在多条活动记录，请先在历史中核对，禁止重复提交')
            old=active[0] if active else None
            if expected_id and (not old or old['id']!=expected_id):
                raise BookingError('原计划已被其他窗口更新，请刷新历史后重新确认')
            if old and old['status']!='scheduled':
                raise BookingError('当天计划已在运行或结果待核实，请先在历史中处理，不得重复提交')
            overlap=old and intersects(spans(old['data']),spans(data))
            if overlap and not replace:raise ReplaceRequired(deepcopy(old))
            merged=data
            effective_mode=mode
            if old:
                old_hash=old['data'].get('account_hash');new_hash=data.get('account_hash')
                if old_hash and new_hash and old_hash!=new_hash:raise BookingError('两份计划的账号不同，不能合并')
                kept=[s for s in slots_for(old['data'],meta) if not any(s.start<t.end and s.end>t.start for t in new_cells)]
                merged=config_for_slots(data['day'],data['venue'],data['venue_type'],kept+new_cells,
                                         account_hash=new_hash or old_hash)
                parse_config(merged)
                effective_mode='schedule'
            previous=deepcopy(old) if old else None
            if old:old['status']='superseded'
            row={'id':uuid.uuid4().hex,'created_at':datetime.now(TZ).isoformat(),'pid':None}
            row.update(data=merged,status='updating' if old else 'registering',mode=effective_mode,
                       updated_at=datetime.now(TZ).isoformat())
            rows.append(row)
            self.save(document)
            result=None
            try:
                result=activate(merged,effective_mode,row['id'])
                row.update(result)
                row['status']='scheduled' if effective_mode=='schedule' else 'waiting'
                row['updated_at']=datetime.now(TZ).isoformat()
                self.save(document)
                return deepcopy(row),bool(old)
            except Exception as exc:
                if result is not None or getattr(exc,'side_effect_uncertain',False):
                    row.update(result if result is not None else exc.details)
                    row.update(status='uncertain',registration_uncertain=effective_mode=='schedule',
                               message='本地任务可能已登记，请刷新历史核实，禁止重复执行')
                else:
                    if previous:old.clear();old.update(previous)
                    row['status']='failed';row['message']='未能登记或启动'
                self.save(document)
                raise
    def attach_configured(self,data,config,activate):
        """A registered job may execute its own entry, but cannot revive deletion."""
        digest=hashlib.sha256(Path(config).read_bytes()).hexdigest()
        with self.lock():
            doc=self.load();row=next((r for r in reversed(doc['records']) if r.get('config_sha256')==digest),None)
            if row and row['status'] in ('deleted','superseded','stopping','running','waiting','starting','uncertain','confirmed','partial'):
                raise BookingError('该计划已删除、已运行或结果待核实，禁止自动重复启动')
            if row is None:
                if any(r['data']['day']==data['day'] and r['status'] in BLOCKING for r in doc['records']):
                    raise BookingError('当天已有其他版本的计划，禁止启动未登记的旧配置')
                row={'id':uuid.uuid4().hex,'data':data,'mode':'wait','status':'registering',
                     'config':str(config),'config_sha256':digest,'created_at':datetime.now(TZ).isoformat(),'pid':None}
                doc['records'].append(row)
            row['status']='starting';self.save(doc)
            result=None
            try:
                result=activate(data,'wait',row['id']);row.update(result);row['status']='waiting';self.save(doc)
            except Exception:
                row['status']='uncertain' if result is not None else 'failed'
                self.save(doc);raise
            return deepcopy(row)
    def assert_active(self,record_id,config_sha256=None):
        if not record_id:return
        row=self.find(record_id)
        if row is None or row['status'] not in PENDING|{'running'}:
            raise BookingError('该本地计划已删除或停止，禁止继续提交')
        if config_sha256 and row.get('config_sha256')!=config_sha256:
            raise BookingError('运行配置与当前计划版本不同，禁止继续提交')
    def update_run(self,record_id,event):
        if not record_id:return
        mapping={'STARTING':'running','READY':'waiting','WAITING_LOGIN':'waiting','ATTEMPTING':'running',
                 'VERIFYING':'running','RECOVERING':'running','DIAGNOSING':'running',
                 'CREATED_PENDING_INVITE':'confirmed','FAILED':'failed','UNCERTAIN':'uncertain',
                 'STOPPED':'stopped','PARTIAL_SUCCESS':'partial','PARTIAL_UNCERTAIN':'uncertain'}
        with self.lock():
            doc=self.load();row=next((r for r in doc['records'] if r['id']==record_id),None)
            if row is None or row['status'] in ('deleted','superseded'):return
            if row.get('pid') and row['pid']!=event.get('pid'):return
            row.update(status=mapping.get(event.get('state'),row['status']),message=event.get('message',''),
                       updated_at=event.get('updated_at',datetime.now(TZ).isoformat()),
                       selection_orders=event.get('selection_orders',[]))
            self.save(doc)
    def delete_local(self,record_id,cancel):
        with self.lock():
            doc=self.load();row=next((r for r in doc['records'] if r['id']==record_id),None)
            if row is None:raise BookingError('找不到所选记录')
            if row['status'] not in ('scheduled','failed','stopped','superseded'):
                raise BookingError('该计划已在执行或已有学校结果，请先停止并核对，不能当作未提交记录删除')
            prior=row['status'];row['status']='stopping';self.save(doc)
            try:
                if prior=='scheduled':cancel(deepcopy(row))
                row.update(status='deleted',updated_at=datetime.now(TZ).isoformat(),message='仅删除本地计划，未取消学校订单')
                self.save(doc)
            except Exception:
                row['status']=prior;self.save(doc);raise
    def school_hidden(self):
        with self.lock():return set(self.load().get('hidden_school',[]))
    def hide_school(self,number):
        with self.lock():
            doc=self.load();doc.setdefault('hidden_school',[])
            if str(number) not in doc['hidden_school']:doc['hidden_school'].append(str(number))
            self.save(doc)
