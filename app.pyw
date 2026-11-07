import ctypes
import argparse
from ctypes import wintypes
from datetime import datetime
import os
import json
from pathlib import Path
import threading
import tkinter as tk
from tkinter import ttk,messagebox

from booking import Api,plan_for,Trial,ServerClock,TZ,BookingError,Stopped,mark_submission
from event_log import EventLog,EventLogError
from session import load_session,PRIVATE
from venue_startup import wait_for_session
from plan_config import load_config, paths_for

parser=argparse.ArgumentParser()
parser.add_argument('--plan',choices=['trial','formal'],default='trial')
parser.add_argument('--config',type=Path)
parser.add_argument('--hidden',action='store_true')
args=parser.parse_args()
MODE='custom' if args.config else args.plan
PLAN=load_config(args.config)[1] if args.config else plan_for(MODE)
STATE_FILE=PRIVATE/f'status-{MODE}.json'
EVENTS_FILE=PRIVATE/f'events-{MODE}.jsonl'
SUBMITTED=PRIVATE/f'submitted-{PLAN.release:%Y%m%d}.json'
VALIDATION_INTENT=PRIVATE/f'validation-{PLAN.release:%Y%m%d}.json'
STOP_FILE=None
if args.config:
    configured_paths=paths_for(PLAN,PRIVATE)
    STATE_FILE,EVENTS_FILE,STOP_FILE=(configured_paths[k] for k in ('state','events','stop'))
END_STATES={'CREATED_PENDING_INVITE','FAILED','UNCERTAIN','STOPPED'}
LABELS={'STARTING':'正在检查','READY':'已就绪，等待放场','ATTEMPTING':'正在试约',
 'WAITING_LOGIN':'等待钉钉场馆登录','RECOVERING':'正在恢复连接','DIAGNOSING':'正在检查异常原因',
 'VERIFYING':'正在核对订单','CREATED_PENDING_INVITE':'订单已创建，等待你邀请同伴',
 'FAILED':'本次已停止','UNCERTAIN':'请到钉钉核对结果','STOPPED':'已停止'}

def refresh_interval(state,seconds_to_release):
    if state in {'ATTEMPTING','VERIFYING','RECOVERING','DIAGNOSING'} or -3<=seconds_to_release<=3:
        return 50
    return 500

def main():
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
    kernel.CreateMutexW.restype=wintypes.HANDLE
    mutex=kernel.CreateMutexW(None,False,f'Local\\HDU_Booking_{PLAN.release:%Y%m%d}')
    existing=ctypes.get_last_error()==183
    root=tk.Tk()
    if args.hidden:root.withdraw()
    if not mutex or existing:
        root.withdraw()
        if not args.hidden:messagebox.showinfo('羽毛球试约','本次试约窗口已经在运行，请查看已有窗口。',parent=root)
        root.destroy()
        if mutex:kernel.CloseHandle(mutex)
        return
    PRIVATE.mkdir(parents=True,exist_ok=True)
    stop=threading.Event();guard=threading.Lock();clock=ServerClock()
    current={};notified=False
    event_log=EventLog(STATE_FILE,EVENTS_FILE)

    thread=None
    try:
        def publish(**event):
            event.update(updated_at=datetime.now(TZ).isoformat(),pid=os.getpid(),
                plan={'release':PLAN.release.isoformat(),'day':PLAN.day,'venue':PLAN.venue,'start':PLAN.start,'end':PLAN.end})
            with guard:
                current.clear();current.update(event)
                event_log.submit(event)

        def before_create(active,site):
            try:event_log.check()
            except EventLogError as exc:raise BookingError(str(exc)) from None
            mark_submission(SUBMITTED,active,site)

        def before_attempt():
            try:event_log.check()
            except EventLogError as exc:raise BookingError(str(exc)) from None
            mark_submission(VALIDATION_INTENT,PLAN,None,stage='validation_intent')

        def worker():
            kernel.SetThreadExecutionState(0x80000001)
            try:
                publish(state='STARTING',message='正在检查场馆登录和预约条件')
                if SUBMITTED.exists() or VALIDATION_INTENT.exists():
                    raise BookingError('已有提交或可能的校验记录，请先核对，勿重启重试')
                def acquire_session():
                    if MODE in ('formal','custom'):
                        return wait_for_session(PLAN.release.timestamp(),stop,publish,load_session)[0]
                    return load_session(PLAN.release.timestamp())[0]

                api=Api(acquire_session())
                Trial(api,PLAN,publish=publish,stop=stop,clock=clock,
                    before_create=before_create,before_attempt=before_attempt).run(
                    refresh_session=acquire_session)
            except Stopped as exc:
                publish(state='STOPPED',message=str(exc))
            except BookingError as exc:
                publish(state='FAILED',message=str(exc))
            except Exception:
                publish(state='FAILED',message='准备检查未通过，请保持钉钉场馆首页已登录后重试')
            finally:
                try:event_log.flush()
                except EventLogError:pass  # The UI displays this independently of the booking result.
                kernel.SetThreadExecutionState(0x80000000)

        root.title('场馆预约助手 · 运行进度')
        root.geometry('620x550');root.minsize(580,520)
        root.configure(bg='#f5f7fa')
        frame=ttk.Frame(root,padding=26);frame.pack(fill='both',expand=True,padx=16,pady=16)
        ttk.Label(frame,text=f'{PLAN.release:%m 月 %d 日 %H:%M} 自动预约',font=('Microsoft YaHei UI',20,'bold')).pack(anchor='w')
        sites='任意一片场地' if not PLAN.site_ids else '场地 '+','.join(map(str,PLAN.site_ids))
        fallback='；无整段空位时尝试原定单小时' if PLAN.allow_one_hour else '；仅预约完整时段'
        ttk.Label(frame,text=f'{PLAN.day} · {PLAN.venue}\n{PLAN.start}–{PLAN.end}{fallback}\n{sites} · 费用上限 ¥0',
            font=('Microsoft YaHei UI',12),justify='left').pack(anchor='w',pady=(12,14))
        timer=ttk.Label(frame,font=('Microsoft YaHei UI',21,'bold'));timer.pack(anchor='w',pady=(0,8))
        heading=ttk.Label(frame,font=('Microsoft YaHei UI',12,'bold'));heading.pack(anchor='w')
        detail=ttk.Label(frame,wraplength=490,justify='left',font=('Microsoft YaHei UI',10));detail.pack(anchor='w',pady=(6,8))
        ttk.Label(frame,text='请保持电脑开机、联网，并保留本窗口。\n订单创建后，请在十分钟内到钉钉邀请同伴。',
            font=('Microsoft YaHei UI',10),foreground='#536575').pack(anchor='w',pady=(8,8))

        def request_stop():
            stop.set();button.configure(state='disabled');detail.configure(text='正在停止后续操作，并核对已发出的请求…')
        button=ttk.Button(frame,text='停止本次脚本',command=request_stop);button.pack(anchor='w',pady=(8,0))
        thread=threading.Thread(target=worker,name='HDU-booking-trial',daemon=False)

        def refresh():
            nonlocal notified
            if STOP_FILE is not None and STOP_FILE.exists():
                try:
                    if json.loads(STOP_FILE.read_text(encoding='utf-8')).get('pid')==os.getpid():stop.set()
                except (OSError,ValueError):pass
            with guard:event=dict(current)
            state=event.get('state','STARTING')
            heading.configure(text=LABELS.get(state,state))
            message=event.get('message','正在启动')
            if state=='CREATED_PENDING_INVITE':
                message=f"场地 {event.get('site_id')}，{event.get('booked_start')}–{event.get('booked_end')}。订单已核实。\n请现在到钉钉手动邀请同伴。"
            if event_log.error:message+='\n\n'+event_log.error
            detail.configure(text=message)
            remaining=PLAN.release.timestamp()-clock.now()
            left=max(0,int(remaining))
            timer.configure(text=f'距放场 {left//3600:02}:{left%3600//60:02}:{left%60:02}' if state not in END_STATES else '')
            if state in END_STATES:
                button.configure(state='disabled')
                if not notified:
                    notified=True
                    if not args.hidden:root.deiconify();root.lift()
                if args.hidden and not thread.is_alive():
                    root.destroy()
                    return
            root.after(refresh_interval(state,remaining),refresh)

        def close_when_stopped():
            stop.set()
            if thread.is_alive():
                button.configure(state='disabled');detail.configure(text='正在停止并核对，请稍候…')
                root.after(200,close_when_stopped)
            else:
                try:event_log.close()
                except EventLogError:pass  # Already exposed by refresh; preserve the actual booking result.
                root.destroy()
        root.protocol('WM_DELETE_WINDOW',close_when_stopped)
        thread.start();refresh()
        root.mainloop()
    finally:
        stop.set()
        if thread is not None and thread.ident is not None:thread.join()
        try:event_log.close()
        except EventLogError:pass
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.CloseHandle(mutex)

if __name__=='__main__':main()
