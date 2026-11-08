"""One shared calendar/court-grid entry point for local and portable users."""
import argparse
from datetime import datetime,timedelta,date
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk,messagebox

from booking import Api,BookingError,TZ
from plan_config import default_config,parse_config,load_config,save_config,ensure_future,paths_for,bookable_days
from selection_plan import (SelectionPlan,SelectedSlot,GridSelection,config_for_slots,
                            ensure_executable,validate_cells)
from court_grid import CourtGrid
from schedule_data import ScheduleReader,validate_snapshot
from session import PRIVATE,read_live_session,validate_session
from system_schedule import register_task,list_booking_tasks,disable_booking_task
from plan_history import PlanHistory,ReplaceRequired,slots_for,spans
from history_view import HistoryWindow
from venue_startup import ENTRANCE

ROOT=Path(__file__).resolve().parent
LAST=PRIVATE/'last-plan.json'
BG='#f4f7fb';INK='#172b4d';MUTED='#526581';BLUE='#2563eb'
END={'CREATED_PENDING_INVITE','FAILED','UNCERTAIN','STOPPED','PARTIAL_SUCCESS','PARTIAL_UNCERTAIN'}
LABELS={'STARTING':'正在检查预约条件','WAITING_LOGIN':'等待你登录钉钉场馆',
        'READY':'已就绪，等待放场','ATTEMPTING':'正在尝试所选时段','VERIFYING':'正在核对学校订单',
        'RECOVERING':'正在恢复查询','DIAGNOSING':'正在核对异常',
        'CREATED_PENDING_INVITE':'预约已核实，请邀请同伴','FAILED':'已停止，请查看说明',
        'UNCERTAIN':'结果待核实，请查看钉钉订单','STOPPED':'已停止后续提交',
        'PARTIAL_CREATED':'已有订单核实，正在处理剩余时段',
        'PARTIAL_SUCCESS':'部分完成，其余已停止','PARTIAL_UNCERTAIN':'部分完成，请核对其余结果'}

class Panel:
    def __init__(self,root,data,auto_start=False,auto_read=True,sync_history=True):
        self.root=root;self.boot_data=data;self.process=None;self.active_plan=None;self.active_pid=None
        self.paths=None;self.progress_index=0;self.messages=queue.Queue();self.closing=False
        self.read_stop=threading.Event();self.reader_thread=None;self.snapshots={};self.models={}
        self.generation=0;self.has_terminal=False;self.clock_minute=''
        self.history=PlanHistory(PRIVATE);self.history_record=None;self.history_stamp=0;self.last_history_event=None
        self.history_error=False
        if sync_history:
            try:self.sync_history()
            except (BookingError,OSError,ValueError,subprocess.SubprocessError):self.history_error=True
        self.venues={'综合馆羽毛球':'badminton','穹顶网球场':'tennis'}
        self.venues[data['venue']]=data['venue_type']
        self.days=list(bookable_days())
        selected_day=data['day'] if data['day'] in self.days else self.days[0]
        self.vars={'venue':tk.StringVar(value=data['venue']),'day':tk.StringVar(value=selected_day)}
        root.title('场馆预约助手 · 七日选场');root.geometry('1280x900');root.minsize(1080,740);root.configure(bg=BG)
        style=ttk.Style(root);style.theme_use('clam')
        style.configure('.',font=('Microsoft YaHei UI',10))
        style.configure('TFrame',background=BG);style.configure('Card.TFrame',background='white')
        style.configure('TLabel',background=BG,foreground=INK)
        style.configure('Card.TLabel',background='white',foreground=INK)
        style.configure('Hint.TLabel',background='white',foreground=MUTED)
        style.configure('TCombobox',padding=8);style.configure('TButton',padding=(10,9))
        style.configure('Primary.TButton',background=BLUE,foreground='white',borderwidth=0)
        style.map('Primary.TButton',background=[('active','#1d4ed8'),('disabled','#dbe5f3')],
                  foreground=[('disabled','#526581')])
        style.configure('Day.TButton',padding=(8,10),background='white')
        style.configure('Selected.Day.TButton',padding=(8,10),background=BLUE,foreground='white')
        top=ttk.Frame(root,padding=(26,18,26,10));top.pack(fill='x')
        ttk.Label(top,text='场馆预约助手',font=('Microsoft YaHei UI',23,'bold')).pack(side='left')
        ttk.Label(top,text='选择日期，点选场地时段',foreground=MUTED).pack(side='left',padx=20)
        toolbar=ttk.Frame(root,padding=(26,0,26,12));toolbar.pack(fill='x')
        ttk.Label(toolbar,text='运动场馆').pack(side='left',padx=(0,12))
        self.venue=ttk.Combobox(toolbar,textvariable=self.vars['venue'],values=list(self.venues),
                                state='readonly',width=24)
        self.venue.pack(side='left')
        self.venue.bind('<<ComboboxSelected>>',lambda e:self.change_venue())
        self.refresh=ttk.Button(toolbar,text='刷新七天排期',command=self.read_week)
        self.refresh.pack(side='left',padx=10)
        self.catalog=ttk.Button(toolbar,text='更新场馆列表',command=self.read_catalog)
        self.catalog.pack(side='left')
        ttk.Button(toolbar,text='打开钉钉场馆登录',command=self.open_login).pack(side='right')
        calendar=ttk.Frame(root,padding=(26,0,26,12));calendar.pack(fill='x');self.calendar=calendar
        self.day_buttons={}
        weekdays='一二三四五六日'
        for i,day in enumerate(self.days):
            label=('最早可约' if i==0 else '周'+weekdays[date.fromisoformat(day).weekday()])+'\n'+day[5:].replace('-','/')
            button=ttk.Button(calendar,text=label,style='Day.TButton',command=lambda d=day:self.change_day(d))
            button.pack(side='left',expand=True,fill='x',padx=(0,8) if i<6 else 0)
            self.day_buttons[day]=button
        body=ttk.Frame(root,padding=(26,0,26,10));body.pack(fill='both',expand=True)
        body.columnconfigure(0,weight=1);body.rowconfigure(0,weight=1)
        left=ttk.Frame(body,style='Card.TFrame',padding=14);left.grid(row=0,column=0,sticky='nsew',padx=(0,14))
        legend=ttk.Frame(left,style='Card.TFrame');legend.pack(fill='x',pady=(0,9))
        ttk.Label(legend,text='场地与时段',style='Card.TLabel',font=('Microsoft YaHei UI',13,'bold')).pack(side='left')
        for color,label in (('#ffffff','可选'),('#e5e7eb','不可选'),('#eff6ff','已登记'),(BLUE,'已选')):
            swatch=tk.Canvas(legend,width=13,height=13,bg='white',highlightthickness=0)
            swatch.pack(side='left',padx=(16,4));swatch.create_rectangle(1,1,12,12,fill=color,outline='#94a3b8')
            ttk.Label(legend,text=label,style='Hint.TLabel').pack(side='left')
        self.grid=CourtGrid(left,self.selection_changed,self.show_hint);self.grid.pack(fill='both',expand=True)
        self.hint=ttk.Label(left,text='鼠标点选；也可用方向键移动，空格选择。',style='Hint.TLabel',wraplength=800)
        self.hint.pack(fill='x',pady=(10,0))
        side=ttk.Frame(body,style='Card.TFrame',padding=16,width=300);side.grid(row=0,column=1,sticky='nsew')
        side.pack_propagate(False)
        actions=ttk.Frame(side,style='Card.TFrame',padding=(0,8,0,0));actions.pack(side='bottom',fill='x')
        status_area=ttk.Frame(side,style='Card.TFrame',padding=(0,8,0,0));status_area.pack(side='bottom',fill='x')
        detail_scroll=ttk.Scrollbar(side,orient='vertical')
        detail_scroll.pack(side='right',fill='y')
        detail_canvas=tk.Canvas(side,bg='white',highlightthickness=0,yscrollcommand=detail_scroll.set)
        detail_canvas.pack(side='left',fill='both',expand=True)
        detail_scroll.configure(command=detail_canvas.yview)
        right=ttk.Frame(detail_canvas,style='Card.TFrame')
        detail_window=detail_canvas.create_window(0,0,window=right,anchor='nw')
        right.bind('<Configure>',lambda e:detail_canvas.configure(scrollregion=detail_canvas.bbox('all')))
        detail_canvas.bind('<Configure>',lambda e:detail_canvas.itemconfigure(detail_window,width=e.width))
        ttk.Label(right,text='本次选择',style='Card.TLabel',font=('Microsoft YaHei UI',15,'bold')).pack(anchor='w')
        self.preview=ttk.Label(right,text='',style='Card.TLabel',wraplength=245,justify='left')
        self.preview.pack(fill='x',pady=(16,10))
        self.rules=ttk.Label(right,text='每天同场馆最多 2 个时段\n同一时间只能预约一片场地\n受邀或已有预约的时间不可选\n仅免费，成功后 10 分钟内邀请',style='Hint.TLabel',wraplength=245,justify='left')
        self.rules.pack(fill='x',pady=(0,14))
        ttk.Separator(status_area).pack(fill='x',pady=4)
        self.heading=ttk.Label(status_area,text='尚未启动',style='Card.TLabel',font=('Microsoft YaHei UI',11,'bold'),wraplength=260)
        self.heading.pack(anchor='w',pady=(6,5))
        self.detail=tk.Text(status_area,height=4,wrap='word',font=('Microsoft YaHei UI',9),
                            bg='white',fg=MUTED,relief='flat',highlightthickness=0,width=1)
        self.detail.pack(fill='x')
        self.set_detail('先读取排期，再点击格子。\n预读排期不会提交预约。')
        self.countdown=ttk.Label(status_area,text='',style='Card.TLabel',font=('Consolas',14,'bold'))
        self.start=ttk.Button(actions,text='开始所选计划',style='Primary.TButton',command=self.begin)
        self.start.pack(fill='x',pady=5)
        self.schedule=ttk.Button(actions,text='设为定时启动',command=self.schedule_plan);self.schedule.pack(fill='x',pady=5)
        self.stop=ttk.Button(actions,text='停止后续提交',command=self.request_stop,state='disabled');self.stop.pack(fill='x',pady=5)
        bottom=ttk.Frame(root,padding=(26,0,26,15));bottom.pack(fill='x')
        self.load_info=ttk.Label(bottom,text='浅蓝格已有本地计划，提交时需确认覆盖；可到历史中删除或改场地。',foreground=MUTED);self.load_info.pack(side='left')
        self.history_button=ttk.Button(bottom,text='历史记录 / 更改场地',command=self.open_history);self.history_button.pack(side='right')
        self.update_days();self.selection_changed()
        root.protocol('WM_DELETE_WINDOW',self.close);root.after(200,self.poll)
        if auto_start:root.after(100,self.begin_configured)
        elif auto_read:root.after(250,self.read_week)

    def current_model(self):
        return self.models.get((self.vars['venue'].get(),self.vars['day'].get()))
    def update_days(self):
        for day,button in self.day_buttons.items():
            button.configure(style='Selected.Day.TButton' if day==self.vars['day'].get() else 'Day.TButton')
    def refresh_calendar(self,now):
        days=list(bookable_days(now))
        if self.days==days:return False
        self.days=days
        if self.vars['day'].get() not in self.days:self.vars['day'].set(self.days[0])
        for button in self.day_buttons.values():button.destroy()
        self.day_buttons={}
        weekdays='一二三四五六日'
        for i,day in enumerate(self.days):
            label=('最早可约' if i==0 else '周'+weekdays[date.fromisoformat(day).weekday()])+'\n'+day[5:].replace('-','/')
            button=ttk.Button(self.calendar,text=label,style='Day.TButton',command=lambda d=day:self.change_day(d))
            button.pack(side='left',expand=True,fill='x',padx=(0,8) if i<6 else 0)
            if self.process is not None:button.configure(state='disabled')
            self.day_buttons[day]=button
        self.update_days();self.show_day()
        return True
    def change_day(self,day):
        if self.process is not None:return
        self.vars['day'].set(day);self.update_days();self.show_day()
    def change_venue(self):
        if self.process is not None:return
        self.show_day()
        if not any(key[0]==self.vars['venue'].get() for key in self.models):self.read_week()
    def show_day(self):
        model=self.current_model()
        if model:self.grid.set_model(model)
        else:self.grid.clear('这一天尚未读取，请点击“刷新七天排期”')
        self.selection_changed()
    def selection_changed(self):
        if self.process is not None:return
        model=self.current_model();selected=sorted(model.selected) if model else []
        lines=[self.vars['day'].get(),self.vars['venue'].get(),'']
        if selected:
            plan=model.plan()
            lines.extend(f'{s.site_id}号场  {s.start}–{s.end}' for s in selected)
            lines.extend(['',f'已选 {len(selected)} / 2 个时段',f'预计 {len(plan.groups())} 笔订单 · 免费'])
            if len(plan.groups())==2:lines.append('分为两单，成功后分别邀请同伴')
            future=plan.release>datetime.now(TZ)
            self.start.configure(text='开始等待放场')
            self.schedule.configure(state='normal' if future else 'disabled')
            lines.extend(['',f'放场：{plan.release:%m月%d日 %H:%M}'])
        else:
            lines.extend(['点击可选格子后变为蓝色','最多选择两个不重叠的时段'])
            self.start.configure(text='开始所选计划')
        self.preview.configure(text='\n'.join(lines))
    def show_hint(self,text):self.hint.configure(text=text)
    def set_detail(self,text):
        self.detail.configure(state='normal')
        self.detail.delete('1.0','end');self.detail.insert('1.0',text)
        self.detail.configure(state='disabled')
    def data(self,execute=False):
        model=self.current_model()
        if model is None:raise BookingError('请先登录并刷新所选日期的排期')
        now=datetime.now(TZ)
        if model.day not in bookable_days(now):raise BookingError('此日期已过可选窗口，请选择新的日期')
        account=self.snapshots[(model.venue,model.day)].get('identity_hash')
        if not account:raise BookingError('缺少本次登录校验，请刷新排期后选择')
        data=config_for_slots(model.day,model.venue,model.kind,model.selected,now,account_hash=account)
        plan=parse_config(data)
        validate_cells(plan,model.meta,model.stock,model.own,model.invited,now)
        ensure_executable(plan,now)
        return data,plan
    def persist(self,data):
        plan=parse_config(data)
        save_config(LAST,data)
        key=hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:20]
        path=PRIVATE/'plans'/f'{key}.json';digest=save_config(path,data)
        return data,plan,path,digest
    def error(self,text,use_exception=False):
        exc=sys.exception()
        if use_exception and isinstance(exc,(BookingError,RuntimeError)):text=str(exc)
        self.set_detail(text);messagebox.showerror('请检查预约设置',text,parent=self.root)
    def set_running(self,running):
        self.venue.configure(state='disabled' if running else 'readonly')
        for button in (*self.day_buttons.values(),self.start,self.schedule,self.refresh,self.catalog):
            button.configure(state='disabled' if running else 'normal')
        self.stop.configure(state='normal' if running else 'disabled')
        self.grid.enabled=not running
        if not running:self.selection_changed()
    def launch(self,data,plan,path,record_id=None):
        paths=paths_for(plan,PRIVATE)
        if paths['validation'].exists() or paths['submitted'].exists():
            raise BookingError('该放场日已有校验或提交记录，请先核对原预约，不能重复启动')
        runner=Path(sys.executable).with_name('pythonw.exe')
        if not runner.exists():runner=Path(sys.executable)
        self.read_stop.set()
        self.generation+=1
        arguments=[str(runner),'-E','-s','-B',str(ROOT/'app.pyw'),'--config',str(path),'--hidden']
        if record_id:arguments.extend(['--history-id',record_id])
        self.process=subprocess.Popen(arguments,cwd=ROOT,creationflags=subprocess.CREATE_NO_WINDOW)
        self.history_record=record_id;self.last_history_event=None
        self.active_plan=plan;self.paths=paths;self.active_pid=self.process.pid;self.progress_index=0
        if not isinstance(plan,SelectionPlan):
            sites='任意一片场地' if not plan.site_ids else ','.join(map(str,plan.site_ids))+'号场'
            self.preview.configure(text=f'{plan.day}\n{plan.venue}\n\n{plan.start}–{plan.end}\n{sites}\n免费 · 最多一单\n\n放场：{plan.release:%m月%d日 %H:%M}')
        self.heading.configure(text='正在启动');self.set_detail('正在检查本人登录、剩余时段名额和所选场地。')
        self.set_running(True)
    def begin(self):
        self.submit('wait')
    def begin_configured(self):
        if self.process is not None:return
        try:
            data=self.boot_data;plan=parse_config(data)
            ensure_future(plan)
            _,_,path,digest=self.persist(data)
            self.history.attach_configured(data,path,self.activate)
        except (BookingError,OSError):self.error('定时计划未启动，请核对已有订单和计划时间。',True)
    def schedule_plan(self):
        self.submit('schedule')
    def activate(self,data,mode,record_id):
        data,plan,path,digest=self.persist(data)
        paths=paths_for(plan,PRIVATE)
        if paths['validation'].exists() or paths['submitted'].exists():
            raise BookingError('该放场日已有校验或提交记录，不能再次执行，请查看历史')
        if mode=='schedule':
            ensure_executable(plan,scheduled=True)
            result=register_task(path,digest)
            return {'task_name':result['task_name'],'config':str(path),'config_sha256':digest,'pid':None,
                    'message':'已登记 '+result['start_at'][:16].replace('T',' ')+' 自动启动'}
        self.launch(data,plan,path,record_id)
        return {'config':str(path),'config_sha256':digest,'pid':self.process.pid,
                'started_at':datetime.now(TZ).isoformat(),'message':'正在等待放场'}
    def submit(self,mode,replace=False,expected_id=None):
        if self.process is not None:return
        try:
            if self.history_error:raise BookingError('历史记录同步失败，请先刷新历史，避免重复登记')
            data,plan=self.data()
            paths=paths_for(plan,PRIVATE)
            if paths['validation'].exists() or paths['submitted'].exists():raise BookingError('该放场日已有提交记录，请到历史核对')
            row,merged=self.history.apply(data,self.current_model().meta,mode,self.activate,replace,expected_id)
            if self.process is None:
                self.refresh_blocks(clear=True)
                self.set_detail(('已更新同一天的原计划。\n' if merged else '已登记本地计划。\n')+row.get('message','')+'\n可在历史记录里删除或更改场地。')
        except ReplaceRequired as exc:
            description='、'.join(a+'–'+b for a,b in spans(exc.record['data']))
            if messagebox.askyesno('确认覆盖已有时段',description+'\n已存在本地计划。是否用新选场地覆盖重叠时段？其他时段保留。',parent=self.root):
                self.submit(mode,replace=True,expected_id=exc.record['id'])
        except (BookingError,OSError,subprocess.SubprocessError):self.error('登记失败，请检查日期与系统任务权限。',True)
    def open_login(self):
        try:os.startfile(ENTRANCE)
        except OSError:self.error('无法打开钉钉，请先安装并登录电脑版钉钉。')
    def sync_history(self):
        for task in list_booking_tasks():self.history.import_task(task)
        for record in self.history.records():
            if not record.get('pid'):continue
            path=paths_for(parse_config(record['data']),PRIVATE)['state']
            if not path.exists():continue
            try:
                status=json.loads(path.read_text(encoding='utf-8'))
                if status.get('pid')==record['pid'] and status.get('updated_at','')>=record.get('started_at',record['created_at']):
                    self.history.update_run(record['id'],status)
            except (OSError,ValueError):pass
        self.history_error=False
    def open_history(self):
        try:HistoryWindow(self)
        except (BookingError,OSError,ValueError):self.error('历史记录无法读取，请保留文件后检查。',True)
    def refresh_blocks(self,clear=False):
        if self.process is not None:return
        if clear:
            for model in self.models.values():model.selected.clear()
        for snapshot in list(self.snapshots.values()):self.accept_snapshot(snapshot)
        self.history_stamp=self.history.path.stat().st_mtime_ns if self.history.path.exists() else 0
    def delete_history_plan(self,record_id):
        existing=self.history.find(record_id)
        if existing and existing['status']=='scheduled':
            paths=paths_for(parse_config(existing['data']),PRIVATE)
            if paths['validation'].exists() or paths['submitted'].exists():
                raise BookingError('该计划已有校验或提交记录，不能通过删除释放时段，请先核对学校订单')
        def cancel(row):
            if row.get('task_name'):
                disable_booking_task(row['task_name'],row.get('config_sha256'))
        self.history.delete_local(record_id,cancel)
        self.refresh_blocks(clear=True)
        self.set_detail('本地计划已删除，对应定时任务已停用，可以重新选择；没有取消学校订单。')
    def change_history_sites(self,record_id,sites):
        row=self.history.find(record_id)
        if not row or row['status']!='scheduled':raise BookingError('仅能更改尚未启动的本地定时计划')
        old=row['data'];snapshot=self.snapshots.get((old['venue'],old['day']))
        if snapshot is None:raise BookingError('请先读取这一天的场馆排期')
        old_hash=old.get('account_hash');account=snapshot.get('identity_hash')
        if old_hash and old_hash!=account:raise BookingError('当前账号与原计划不同，不能直接覆盖')
        original=slots_for(old,snapshot['meta'])
        if len(original)!=len(sites):raise BookingError('修改的时段数量与原计划不同')
        selected=[SelectedSlot(s.start,s.end,site) for s,site in zip(original,sites)]
        data=config_for_slots(old['day'],old['venue'],old['venue_type'],selected,account_hash=account)
        plan=parse_config(data);ensure_executable(plan,scheduled=True)
        validate_cells(plan,snapshot['meta'],snapshot['stock'],snapshot['own'],snapshot['invited'])
        self.history.apply(data,snapshot['meta'],'schedule',self.activate,replace=True,expected_id=record_id)
        self.refresh_blocks(clear=True);self.set_detail('已更改同一个本地计划的场地，预约日期和时段保持不变。')

    def accept_snapshot(self,snapshot):
        validate_snapshot(snapshot)
        key=(snapshot['venue'],snapshot['day']);old=self.models.get(key)
        blocked,replaceable,reserved=self.history.blocks(snapshot['day'],snapshot['venue'],snapshot['meta'])
        model=GridSelection(snapshot['day'],snapshot['venue'],snapshot['venue_type'],
                            snapshot['meta'],snapshot['stock'],snapshot['own'],snapshot['invited'],
                            blocked=blocked,reserved_count=reserved,replaceable=replaceable)
        same_account=old and self.snapshots[key].get('identity_hash')==snapshot.get('identity_hash')
        if same_account:
            for slot in sorted(old.selected):
                try:model.toggle(slot)
                except BookingError:pass
        elif (self.boot_data.get('schema')==2 and self.boot_data['day']==snapshot['day'] and self.boot_data['venue']==snapshot['venue']
              and self.boot_data.get('account_hash')==snapshot.get('identity_hash') and not replaceable):
            for slot in parse_config(self.boot_data).selections:
                try:model.toggle(slot)
                except BookingError:pass
        if old and not same_account:self.show_hint('登录账号已变化，已清空原账号的选格，请重新选择。')
        self.snapshots[key]=snapshot;self.models[key]=model
        if key==(self.vars['venue'].get(),self.vars['day'].get()):self.show_day()
    def read_week(self):
        if self.process is not None:return
        self.refresh_calendar(datetime.now(TZ))
        self.read_stop.set()
        self.generation+=1;generation=self.generation
        venue=self.vars['venue'].get();kind=self.venues[venue]
        self.refresh.configure(state='disabled');self.load_info.configure(text='正在只读预读最早可约日起七天排期…')
        def start_after_previous():
            if generation!=self.generation or self.closing or self.process is not None:return
            if self.reader_thread is not None and self.reader_thread.is_alive():
                self.root.after(100,start_after_previous);return
            self.read_stop=threading.Event();stop=self.read_stop
            def read():
                try:
                    ScheduleReader(PRIVATE).read_week(venue,kind,lambda s:self.messages.put(('snapshot',generation,s)),stop)
                    self.messages.put(('loaded',generation))
                except Exception:self.messages.put(('read_error',generation))
            self.reader_thread=threading.Thread(target=read,daemon=True);self.reader_thread.start()
        start_after_previous()
    def read_catalog(self):
        if self.process is not None:return
        self.catalog.configure(state='disabled')
        def read():
            try:
                credentials,_=validate_session(read_live_session())
                class ReadOnly(Api):
                    def call(self,endpoint,*a,**kw):
                        if endpoint!='choose_venue':raise AssertionError()
                        return super().call(endpoint,*a,**kw)
                api=ReadOnly(credentials);names={}
                try:
                    for kind in ('badminton','tennis'):
                        reply=api.call('choose_venue',{'venue_type':kind})
                        if reply.status!=200 or not isinstance(reply.data.get('venue_list'),list):raise BookingError()
                        for row in reply.data['venue_list']:
                            name=row if isinstance(row,str) else row.get('venue_name') if isinstance(row,dict) else None
                            if isinstance(name,str) and name.strip():names[name]=kind
                finally:
                    if api.connection:api.connection.close()
                if not names:raise BookingError()
                self.messages.put(('catalog',names))
            except Exception:self.messages.put(('catalog_error',))
        threading.Thread(target=read,daemon=True).start()
    def request_stop(self):
        if self.active_pid is None or self.paths is None:return
        try:
            raw=json.dumps({'pid':self.active_pid}).encode()
            target=self.paths['stop'];temporary=target.with_suffix('.tmp')
            temporary.write_bytes(raw);os.replace(temporary,target)
            self.set_detail('正在停止后续提交；已发出的请求仍会查单核实，不会取消订单。')
            self.stop.configure(state='disabled')
        except OSError:self.error('停止指令无法保存，请保持窗口打开并检查目录权限。')
    def poll(self):
        now=datetime.now(TZ);minute=now.strftime('%Y-%m-%d %H:%M')
        if minute!=self.clock_minute:
            self.clock_minute=minute
            rolled=self.refresh_calendar(now)
            for model in self.models.values():model.expire()
            self.grid.draw()
            if self.process is None:self.selection_changed()
            if rolled and self.process is None:self.read_week()
        stamp=self.history.path.stat().st_mtime_ns if self.history.path.exists() else 0
        if stamp!=self.history_stamp and self.process is None:
            try:self.refresh_blocks()
            except (BookingError,OSError):self.history_error=True
        while not self.messages.empty():
            event=self.messages.get_nowait()
            if event[0] in ('catalog','catalog_error') and self.process is not None:continue
            if event[0] in ('snapshot','loaded','read_error') and event[1]!=self.generation:continue
            if event[0]=='snapshot':
                try:self.accept_snapshot(event[2])
                except (BookingError,KeyError,ValueError,TypeError):
                    self.show_hint('学校返回了一份不完整排期，已忽略；可以刷新重试。')
            elif event[0]=='loaded':
                self.refresh.configure(state='normal' if self.process is None else 'disabled')
                count=sum(v==self.vars['venue'].get() for v,d in self.models)
                self.load_info.configure(text=f'已读 {count}/7 天排期 · {datetime.now(TZ):%H:%M} · 放场前仍会复查')
            elif event[0]=='read_error':
                self.refresh.configure(state='normal' if self.process is None else 'disabled')
                self.load_info.configure(text='读取未完成，请在电脑钉钉进入场馆首页登录后刷新。')
            elif event[0]=='catalog':
                self.venues=event[1];self.venue.configure(values=list(self.venues))
                self.catalog.configure(state='normal' if self.process is None else 'disabled')
                if self.vars['venue'].get() not in self.venues:self.vars['venue'].set(next(iter(self.venues)));self.change_venue()
            elif event[0]=='catalog_error':
                self.catalog.configure(state='normal' if self.process is None else 'disabled')
                self.show_hint('场馆列表读取失败，请确认电脑钉钉的场馆登录。')
            elif event[0]=='school_history':
                try:event[1].show_school(event[2])
                except (tk.TclError,BookingError,ValueError,KeyError):pass
            elif event[0]=='school_history_error':
                try:event[1].message.configure(text='学校历史读取失败，请检查钉钉场馆登录后刷新；本地计划仍可查看。')
                except tk.TclError:pass
        if self.process is not None:
            try:
                row=json.loads(self.paths['state'].read_text(encoding='utf-8'))
                if row.get('pid')==self.active_pid:
                    if self.history_record and row.get('updated_at')!=self.last_history_event:
                        self.history.update_run(self.history_record,row)
                        self.last_history_event=row.get('updated_at')
                    state=row.get('state');self.heading.configure(text=LABELS.get(state,state or '正在运行'))
                    text=row.get('message','')
                    orders=row.get('selection_orders')
                    if orders:
                        text+='\n\n已核实：\n'+'\n'.join(f'{o["site_id"]}号场 {o["start"]}–{o["end"]}' for o in orders)
                    if state=='CREATED_PENDING_INVITE':
                        if not orders:text=f'{self.active_plan.day} {row.get("booked_start")}–{row.get("booked_end")}，{self.active_plan.venue} {row.get("site_id")}号场。'
                        text+='\n请在每单成功后10分钟内，分别手动邀请同伴。'
                    self.set_detail(text)
                    left=max(0,int(self.active_plan.release.timestamp()-datetime.now(TZ).timestamp()))
                    self.countdown.configure(text='' if state in END else f'{left//3600:02}:{left%3600//60:02}:{left%60:02}')
                    if state in END:self.countdown.pack_forget()
                    else:self.countdown.pack(anchor='w',pady=4)
                    if state in END:self.progress_index+=1
            except (OSError,ValueError):pass
            if self.process.poll() is not None:
                if self.progress_index==0:self.set_detail('运行进程已退出。请查看原窗口或到钉钉核对订单。')
                self.process=None;self.set_running(False)
                if self.closing:self.root.destroy();return
        self.root.after(250,self.poll)
    def close(self):
        self.read_stop.set()
        if self.process is not None and self.process.poll() is None:
            if not messagebox.askyesno('停止并退出','关闭将停止后续提交，已发出的请求会先核对完。继续？',parent=self.root):return
            self.closing=True;self.request_stop()
        else:self.root.destroy()

def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path);parser.add_argument('--expected-sha256');parser.add_argument('--start',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.config:data,plan=load_config(args.config,args.expected_sha256)
        elif LAST.exists():data,plan=load_config(LAST)
        else:data=default_config();plan=parse_config(data)
        if args.start:ensure_future(plan)
    except BookingError as exc:
        if args.start or args.config:
            root=tk.Tk();root.withdraw();messagebox.showerror('计划未启动',str(exc),parent=root);root.destroy();return 1
        data=default_config()
    root=tk.Tk();Panel(root,data,args.start);root.mainloop();return 0

if __name__=='__main__':raise SystemExit(main())
