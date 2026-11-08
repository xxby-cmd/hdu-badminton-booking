"""Execute exact selected cells through the original Trial, serially and durably."""
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from booking import Trial,BookingError,Stopped,Uncertain,TZ,mark_submission,ServerClock,exact_order
from selection_plan import validate_quota,ensure_executable,SelectedSlot

class BatchJournal:
    def __init__(self,private,plan,session):
        self.private=Path(private);self.plan=plan;self.session=session
        self.validation=self.private/f'validation-{plan.release:%Y%m%d}.json'
        self.submission=self.private/f'submitted-{plan.release:%Y%m%d}.json'
        self.path=self.private/f'selection-batch-{plan.release:%Y%m%d}.json'
        self.data=None;self.owner=False;self.has_submission=False
    def identity(self,session):
        return hashlib.sha256(json.dumps([session[k] for k in ('openid','nickname','phone')],
                                         ensure_ascii=False).encode()).hexdigest()
    def arm(self):
        if self.owner:return
        self.private.mkdir(parents=True,exist_ok=True)
        if self.validation.exists() or self.submission.exists() or self.path.exists():
            raise BookingError('该放场日已有校验或提交记录，请先核对，不得重复启动')
        # This shared marker also blocks every legacy single-order entry.
        mark_submission(self.validation,self.plan,None,stage='selection_batch_validation_intent')
        self.data={'schema':1,'day':self.plan.day,'venue':self.plan.venue,
                   'identity_hash':self.identity(self.session),
                   'selections':[{'site_id':s.site_id,'start':s.start,'end':s.end} for s in self.plan.selections],
                   'groups':[{'start':p.start,'end':p.end,'site_id':p.site_ids[0] if p.site_ids else None,
                              'check_reserved':False,'create_reserved':False,'verified':False} for p in self.plan.groups()]}
        with self.path.open('x',encoding='utf-8') as f:
            json.dump(self.data,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        self.owner=True
    def save(self):
        temporary=self.path.with_suffix('.tmp')
        with temporary.open('w',encoding='utf-8') as f:
            json.dump(self.data,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        os.replace(temporary,self.path)
    def reserve(self,index,stage,session):
        self.arm()
        if not self.owner or self.identity(session)!=self.data['identity_hash']:
            raise BookingError('账号发生变化，停止后续提交')
        group=self.data['groups'][index]
        key=stage+'_reserved'
        if group[key] or (stage=='create' and not group['check_reserved']):
            raise BookingError('这笔请求已登记或缺少校验，禁止重复提交')
        guard=self.private/f'selection-{self.plan.release:%Y%m%d}-{index}-{stage}.json'
        with guard.open('x',encoding='utf-8') as f:
            json.dump({'group':index,'stage':stage},f);f.flush();os.fsync(f.fileno())
        if stage=='create' and not self.has_submission:
            mark_submission(self.submission,self.plan,group['site_id'],stage='selection_batch_create_reserved')
            self.has_submission=True
        group[key]=True;self.save()
    def verified(self,index,order):
        if not self.owner or not self.data['groups'][index]['create_reserved']:
            raise BookingError('缺少本次建单保护记录')
        self.data['groups'][index]['verified']=True
        self.data['groups'][index]['order_num']=str(order['order_num'])
        self.save()

class SelectionRunner:
    def __init__(self,api,plan,private,publish=None,stop=None,clock=None,guard_check=None):
        self.api,self.plan=api,plan
        self.publish=publish or (lambda **e:None)
        self.stop=stop;self.clock=clock or ServerClock()
        self.guard_check=guard_check or (lambda:None)
        self.journal=BatchJournal(private,plan,api.session)
        self.created=[];self.index=0
        self.groups=plan.groups()
    def remaining(self):
        complete=self.groups[:self.index]
        return tuple(s for s in self.plan.selections if not any(
            (not p.site_ids or s.site_id in p.site_ids) and p.start<=s.start and s.end<=p.end for p in complete))
    def preflight(self,mine,invited,meta):
        actual=hashlib.sha256(str(self.api.session['openid']).encode()).hexdigest()
        if self.plan.account_hash is not None and actual!=self.plan.account_hash:
            raise BookingError('当前账号与选格时不同，请重新读取排期')
        if self.plan.execution_mode=='now':
            ensure_executable(self.plan,datetime.fromtimestamp(self.clock.now(),TZ))
        validate_quota(self.plan,mine,invited,meta,self.remaining())
        for slot in self.remaining():
            if (slot.start,slot.end) not in list(zip(meta['time_list'],meta['time_list'][1:])):
                raise BookingError('学校时间表已改变，停止所选格子的预约')
    def emit(self,**event):
        state=event.get('state')
        summaries=[{'site_id':o['site_id'],'start':o['start_time'],'end':o['end_time']} for o in self.created]
        if state=='CREATED_PENDING_INVITE':
            summaries.append({'site_id':event.get('site_id'),'start':event.get('booked_start'),'end':event.get('booked_end')})
            if self.index<len(self.groups)-1:
                event.update(state='PARTIAL_CREATED',message='已有一笔订单核实，正在处理剩余所选时段；请及时邀请同伴')
        event.update(selection_orders=summaries,planned_orders=len(self.groups),group_index=self.index)
        if state in ('FAILED','UNCERTAIN','STOPPED') and self.created:
            event['state']='PARTIAL_SUCCESS' if state!='UNCERTAIN' else 'PARTIAL_UNCERTAIN'
            event['message']='部分订单已核实，其余已停止；请邀请已成功订单的同伴，并核对未确定的结果。'+event.get('message','')
        self.publish(**event)
    def reserve(self,stage,session):
        self.guard_check()
        if self.stop and self.stop.is_set():raise Stopped('已停止后续提交')
        self.journal.reserve(self.index,stage,session)
    def run(self,refresh_session=None):
        try:
            ensure_executable(self.plan,datetime.fromtimestamp(self.clock.now(),TZ))
            if self.journal.validation.exists() or self.journal.submission.exists() or self.journal.path.exists():
                raise BookingError('该放场日已有校验/提交记录，禁止重复启动')
            for self.index,active in enumerate(self.groups):
                kwargs={'immediate_until':self.plan.execution_until} if self.plan.execution_mode=='now' else {}
                trial=Trial(self.api,active,publish=self.emit,stop=self.stop,clock=self.clock,
                    preflight_check=self.preflight,
                    before_attempt=lambda:self.reserve('check',self.api.session),
                    before_create=lambda p,s:self.reserve('create',self.api.session),**kwargs)
                if self.index==0:
                    result=trial.run(refresh_session)
                else:
                    meta,courts=trial.prepare()
                    result=trial.attempt(meta,courts,prechecked=True)
                if result is None:return self.created
                self.created.append(result)
                self.journal.verified(self.index,result)
            return self.created
        except Stopped as exc:self.emit(state='STOPPED',message=str(exc))
        except Uncertain as exc:self.emit(state='UNCERTAIN',message=str(exc))
        except (BookingError,OSError,ValueError,KeyError,TypeError):
            self.emit(state='FAILED',message='所选计划的条件或保护记录无法确认，已停止后续请求；请核对已有订单')
        return self.created
