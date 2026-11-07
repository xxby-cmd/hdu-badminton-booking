"""Shared local configuration and monitoring window; bookings use app.pyw."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from booking import Api, BookingError, TZ
from plan_config import default_config, parse_config, load_config, save_config, ensure_future, paths_for
from session import PRIVATE, read_live_session, validate_session
from system_schedule import register_task
from venue_startup import ENTRANCE

ROOT=Path(__file__).resolve().parent
LAST=PRIVATE/'last-plan.json'
TEAL='#0f766e';BG='#f1f6f5';INK='#163e3a';MUTED='#49635f'
END={'CREATED_PENDING_INVITE','FAILED','UNCERTAIN','STOPPED'}
LABELS={'STARTING':'正在检查预约条件','WAITING_LOGIN':'等待你登录钉钉场馆',
        'READY':'已就绪，等待放场','ATTEMPTING':'正在预约','VERIFYING':'正在核对学校订单',
        'RECOVERING':'连接恢复中','DIAGNOSING':'正在核对异常',
        'CREATED_PENDING_INVITE':'预约已核实，请邀请同伴','FAILED':'已停止，请查看说明',
        'UNCERTAIN':'结果待核实，请查看钉钉订单','STOPPED':'已停止后续提交'}

class Panel:
    def __init__(self,root,data,auto_start=False):
        self.root=root;self.process=None;self.active_plan=None;self.active_pid=None
        self.messages=queue.Queue();self.closing=False;self.inputs=[];self.paths=None;self.progress_index=0
        root.title('场馆预约助手')
        root.geometry('1040x840');root.minsize(940,800);root.configure(bg=BG)
        style=ttk.Style(root);style.theme_use('clam')
        style.configure('.',font=('Microsoft YaHei UI',11))
        style.configure('TFrame',background=BG)
        style.configure('Card.TFrame',background='white')
        style.configure('TLabel',background=BG,foreground=INK)
        style.configure('Card.TLabel',background='white',foreground=INK)
        style.configure('Hint.TLabel',background='white',foreground=MUTED,font=('Microsoft YaHei UI',10))
        style.configure('TEntry',padding=8)
        style.configure('TCombobox',padding=8)
        style.configure('TButton',padding=(12,10))
        style.configure('Primary.TButton',background=TEAL,foreground='white',borderwidth=0)
        style.map('Primary.TButton',background=[('active','#115e59'),('disabled','#d3e4e1')],
                  foreground=[('disabled','#49635f')])
        top=ttk.Frame(root,padding=(30,20,30,12));top.pack(fill='x')
        ttk.Label(top,text='场馆预约助手',font=('Microsoft YaHei UI',24,'bold')).pack(anchor='w')
        ttk.Label(top,text='选好日期与时段，提前就绪，放场时自动尝试。',foreground=MUTED).pack(anchor='w',pady=(5,0))
        body=ttk.Frame(root,padding=(30,4,30,10));body.pack(fill='both',expand=True)
        body.columnconfigure(0,weight=5);body.columnconfigure(1,weight=4);body.rowconfigure(0,weight=1)
        form=ttk.Frame(body,style='Card.TFrame',padding=24);form.grid(row=0,column=0,sticky='nsew',padx=(0,16))
        form.columnconfigure(0,weight=1);form.columnconfigure(1,weight=1)
        ttk.Label(form,text='01  预约计划',style='Card.TLabel',font=('Microsoft YaHei UI',15,'bold')).grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,16))
        self.vars={k:tk.StringVar(value=data[k]) for k in ('day','start','end','venue')}
        self.vars['sport']=tk.StringVar(value='羽毛球' if data['venue_type']=='badminton' else '网球')
        self.vars['sites']=tk.StringVar(value=','.join(map(str,data['site_ids'])))
        def field(label,key,row,col=0,choices=None,span=2):
            ttk.Label(form,text=label,style='Card.TLabel').grid(row=row,column=col,columnspan=span,sticky='w',pady=(9,5))
            if choices:
                widget=ttk.Combobox(form,textvariable=self.vars[key],values=choices,state='readonly' if key=='sport' else 'normal',width=16)
            else:widget=ttk.Entry(form,textvariable=self.vars[key],width=17)
            widget.grid(row=row+1,column=col,columnspan=span,sticky='ew',padx=(0,8) if span==1 and col==0 else 0)
            self.inputs.append(widget);return widget
        field('预约日期  ·  YYYY-MM-DD','day',1)
        field('开始时间','start',3,0,span=1);field('结束时间','end',3,1,span=1)
        field('运动项目','sport',5,choices=['羽毛球','网球'])
        self.venue=field('场馆名称  ·  与学校页面一致','venue',7,choices=['综合馆羽毛球','穹顶网球场'])
        field('场地编号  ·  留空为任意一片','sites',9)
        ttk.Label(form,text='多片候选用逗号分隔，只预约其中一片。',style='Hint.TLabel').grid(row=11,column=0,columnspan=2,sticky='w',pady=(7,0))
        right=ttk.Frame(body,style='Card.TFrame',padding=24);right.grid(row=0,column=1,sticky='nsew')
        ttk.Label(right,text='02  执行与进度',style='Card.TLabel',font=('Microsoft YaHei UI',15,'bold')).pack(anchor='w')
        self.preview=ttk.Label(right,text='',style='Card.TLabel',wraplength=355,justify='left',font=('Microsoft YaHei UI',12))
        self.preview.pack(fill='x',pady=(18,12))
        ttk.Label(right,text='免费 · 最多一单 · 同一片场地\n只预约完整时段，不自动取消',style='Hint.TLabel').pack(anchor='w',pady=(0,16))
        self.heading=ttk.Label(right,text='尚未启动',style='Card.TLabel',font=('Microsoft YaHei UI',14,'bold'),wraplength=355)
        self.heading.pack(anchor='w',pady=(5,8))
        self.detail=ttk.Label(right,text='保存计划不会发送预约请求。\n启动后请保持电脑开机、联网并登录 Windows。',style='Hint.TLabel',wraplength=355,justify='left')
        self.detail.pack(fill='x')
        self.countdown=ttk.Label(right,text='',style='Card.TLabel',font=('Consolas',19,'bold'))
        self.countdown.pack(anchor='w',pady=(16,8))
        self.start=ttk.Button(right,text='现在开始等待放场',style='Primary.TButton',command=self.begin)
        self.start.pack(fill='x',pady=(7,5))
        self.schedule=ttk.Button(right,text='设为定时启动（放场前 1 小时）',command=self.schedule_plan)
        self.schedule.pack(fill='x',pady=5)
        self.stop=ttk.Button(right,text='停止后续提交',command=self.request_stop,state='disabled')
        self.stop.pack(fill='x',pady=5)
        bottom=ttk.Frame(root,padding=(30,0,30,20));bottom.pack(fill='x')
        self.save_button=ttk.Button(bottom,text='保存计划',command=self.save)
        self.save_button.pack(side='left')
        ttk.Button(bottom,text='打开钉钉场馆登录',command=self.open_login).pack(side='left',padx=10)
        self.catalog=ttk.Button(bottom,text='读取学校场馆列表',command=self.read_catalog)
        self.catalog.pack(side='left')
        ttk.Label(bottom,text='成功后 10 分钟内手动邀请同伴',foreground=MUTED).pack(side='right')
        def sport_changed(*args):
            names=['综合馆羽毛球'] if self.vars['sport'].get()=='羽毛球' else ['穹顶网球场']
            self.venue.configure(values=names);self.vars['venue'].set(names[0])
        self.vars['sport'].trace_add('write',sport_changed)
        for var in self.vars.values():var.trace_add('write',lambda *args:self.update_preview())
        self.update_preview();root.protocol('WM_DELETE_WINDOW',self.close)
        self.root.after(200,self.poll)
        if auto_start:self.root.after(100,self.begin)

    def data(self):
        sites=self.vars['sites'].get().strip().replace('，',',')
        try:sites=[int(x.strip()) for x in sites.split(',')] if sites else []
        except ValueError:raise BookingError('场地编号请填正整数，多个编号用逗号分隔') from None
        data={'schema':1,'day':self.vars['day'].get().strip(),'start':self.vars['start'].get().strip(),
              'end':self.vars['end'].get().strip(),'venue':self.vars['venue'].get().strip(),
              'venue_type':'badminton' if self.vars['sport'].get()=='羽毛球' else 'tennis',
              'site_ids':sites,'max_price':0,'allow_one_hour':False}
        return data,parse_config(data)

    def update_preview(self):
        try:
            _,plan=self.data()
            sites='任意一片' if not plan.site_ids else ','.join(map(str,plan.site_ids))+' 号场'
            self.preview.configure(text=f'{plan.day}  {plan.start}–{plan.end}\n{plan.venue} · {sites}\n\n放场：{plan.release:%m月%d日 %H:%M}（北京时间）')
        except BookingError as exc:self.preview.configure(text=str(exc))

    def snapshot(self):
        data,plan=self.data();ensure_future(plan)
        save_config(LAST,data)
        import hashlib
        key=hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()[:20]
        path=PRIVATE/'plans'/f'{key}.json'
        digest=save_config(path,data)
        return data,plan,path,digest

    def save(self):
        try:
            self.snapshot()
            self.detail.configure(text='计划已保存。尚未开始预约；已登记的定时任务使用登记时的计划，更改后请重新登记。')
        except (BookingError,OSError):self.error('请检查日期、时段和可写目录后重试。',use_exception=True)

    def error(self,text,use_exception=False):
        exc=sys.exception()
        if use_exception and isinstance(exc,BookingError):text=str(exc)
        self.detail.configure(text=text)
        messagebox.showerror('请检查预约设置',text,parent=self.root)

    def set_running(self,running):
        for widget in self.inputs:
            widget.configure(state='disabled' if running else 'readonly' if widget.cget('textvariable')==str(self.vars['sport']) else 'normal')
        for button in (self.start,self.schedule,self.save_button,self.catalog):
            button.configure(state='disabled' if running else 'normal')
        self.stop.configure(state='normal' if running else 'disabled')

    def begin(self):
        if self.process is not None and self.process.poll() is None:return
        try:
            _,plan,path,digest=self.snapshot()
            paths=paths_for(plan,PRIVATE)
            if paths['validation'].exists() or paths['submitted'].exists():
                raise BookingError('该放场日已有校验或提交记录，已禁止再次启动；请先到钉钉核对预约')
            runner=Path(sys.executable).with_name('pythonw.exe')
            if not runner.exists():runner=Path(sys.executable)
            self.process=subprocess.Popen([str(runner),'-E','-s','-B',str(ROOT/'app.pyw'),
                    '--config',str(path),'--hidden'],cwd=ROOT,creationflags=subprocess.CREATE_NO_WINDOW)
            self.active_pid=self.process.pid;self.active_plan=plan;self.paths=paths;self.progress_index=0
            self.heading.configure(text='正在启动');self.detail.configure(text='正在打开运行流程，等待有效场馆登录。')
            self.set_running(True)
        except (BookingError,OSError):self.error('无法启动，请检查程序完整性和目录权限。',use_exception=True)

    def schedule_plan(self):
        try:
            _,plan,path,digest=self.snapshot()
            paths=paths_for(plan,PRIVATE)
            if paths['validation'].exists() or paths['submitted'].exists():
                raise BookingError('该放场日已有提交记录，不能再设置自动提交')
            result=register_task(path,digest)
            self.detail.configure(text=f'已登记并核实：{result["start_at"][:16].replace("T"," ")} 自动启动。\n请保持本文件夹位置不变，并提前登录 Windows 和钉钉。')
        except (BookingError,OSError,subprocess.SubprocessError):self.error('登记失败，请直接启动或检查 Windows 任务计划程序。',use_exception=True)

    def open_login(self):
        try:os.startfile(ENTRANCE)
        except OSError:self.error('无法打开钉钉，请先安装并登录电脑版钉钉。')

    def read_catalog(self):
        self.catalog.configure(state='disabled')
        sport='badminton' if self.vars['sport'].get()=='羽毛球' else 'tennis'
        def read():
            try:
                session,_=validate_session(read_live_session())
                class ReadOnly(Api):
                    def call(self,endpoint,*a,**kw):
                        if endpoint!='choose_venue':raise BookingError('禁止非查询接口')
                        return super().call(endpoint,*a,**kw)
                reply=ReadOnly(session).call('choose_venue',{'venue_type':sport})
                rows=reply.data.get('venue_list')
                if reply.status!=200 or not isinstance(rows,list):raise BookingError('学校场馆列表暂时无法读取')
                names=[r if isinstance(r,str) else r.get('venue_name') for r in rows if isinstance(r,(str,dict))]
                names=[n for n in names if isinstance(n,str) and n.strip()]
                if not names:raise BookingError('学校没有返回可选场馆，请核对已登录账号和项目')
                self.messages.put(('catalog',sport,names))
            except Exception:
                self.messages.put(('error','请先在电脑钉钉打开场馆首页并登录，再读取场馆列表。'))
        threading.Thread(target=read,daemon=True).start()

    def request_stop(self):
        if self.active_pid is None or self.paths is None:return
        try:
            raw=json.dumps({'pid':self.active_pid}).encode('utf-8')
            target=self.paths['stop'];temp=target.with_suffix('.tmp')
            temp.write_bytes(raw);os.replace(temp,target)
            self.detail.configure(text='正在停止后续提交；已经发出的请求仍会查单核实，不会取消预约。')
            self.stop.configure(state='disabled')
        except OSError:self.error('停止指令保存失败，请保持窗口打开并检查目录权限。')

    def poll(self):
        while not self.messages.empty():
            event=self.messages.get_nowait()
            self.catalog.configure(state='normal' if self.process is None or self.process.poll() is not None else 'disabled')
            if event[0]=='error':self.detail.configure(text=event[1])
            elif event[0]=='catalog':
                sport='badminton' if self.vars['sport'].get()=='羽毛球' else 'tennis'
                if event[1]==sport:
                    self.venue.configure(values=event[2])
                    if self.vars['venue'].get() not in event[2]:self.vars['venue'].set(event[2][0])
                    self.detail.configure(text='学校可选场馆已只读加载，请选择预约场馆。')
        if self.process is not None:
            try:
                row=json.loads(self.paths['state'].read_text(encoding='utf-8'))
                if row.get('pid')==self.active_pid:
                    state=row.get('state')
                    self.heading.configure(text=LABELS.get(state,state or '正在运行'))
                    self.detail.configure(text=row.get('message',''))
                    if state=='CREATED_PENDING_INVITE':
                        self.detail.configure(text=f'{self.active_plan.day} {row.get("booked_start")}–{row.get("booked_end")}，{self.active_plan.venue} {row.get("site_id")}号场。\n请现在到钉钉邀请至少一位同伴，10分钟内完成。')
                    left=max(0,int(self.active_plan.release.timestamp()-datetime.now(TZ).timestamp()))
                    self.countdown.configure(text='' if state in END else f'{left//3600:02}:{left%3600//60:02}:{left%60:02}')
                    if state in END:self.progress_index+=1
            except (OSError,ValueError):pass
            if self.process.poll() is not None:
                if self.progress_index==0:self.detail.configure(text='运行进程已退出。若另一个窗口已运行，请查看原窗口；需要时到钉钉核对订单。')
                self.set_running(False);self.process=None
                if self.closing:self.root.destroy();return
        self.root.after(250,self.poll)

    def close(self):
        if self.process is not None and self.process.poll() is None:
            if not messagebox.askyesno('停止并退出','关闭窗口将停止后续提交，已发出的请求会先核对完毕。继续？',parent=self.root):return
            self.closing=True;self.request_stop()
        else:self.root.destroy()

def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--start',action='store_true')
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
    root=tk.Tk();Panel(root,data,args.start);root.mainloop()
    return 0

if __name__=='__main__':raise SystemExit(main())
