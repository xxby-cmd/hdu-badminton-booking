import ctypes
import argparse
from ctypes import wintypes
from datetime import datetime
import json
import os
from pathlib import Path
import threading
import tkinter as tk
from tkinter import ttk,messagebox

from booking import Api,plan_for,Trial,ServerClock,TZ,BookingError,mark_submission
from session import load_session,PRIVATE

parser=argparse.ArgumentParser()
parser.add_argument('--plan',choices=['trial','formal'],default='trial')
MODE=parser.parse_args().plan
PLAN=plan_for(MODE)
STATE_FILE=PRIVATE/f'status-{MODE}.json'
EVENTS_FILE=PRIVATE/f'events-{MODE}.jsonl'
SUBMITTED=PRIVATE/f'submitted-{PLAN.release:%Y%m%d}.json'
END_STATES={'CREATED_PENDING_INVITE','FAILED','UNCERTAIN','STOPPED'}
LABELS={'STARTING':'正在检查','READY':'已就绪，等待放场','ATTEMPTING':'正在试约',
 'VERIFYING':'正在核对订单','CREATED_PENDING_INVITE':'订单已创建，等待你邀请同伴',
 'FAILED':'本次已停止','UNCERTAIN':'请到钉钉核对结果','STOPPED':'已停止'}

def main():
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
    kernel.CreateMutexW.restype=wintypes.HANDLE
    mutex=kernel.CreateMutexW(None,False,f'Local\\HDU_Badminton_{PLAN.release:%Y%m%d}_{MODE}')
    existing=ctypes.get_last_error()==183
    root=tk.Tk()
    if not mutex or existing:
        root.withdraw()
        messagebox.showinfo('羽毛球试约','本次试约窗口已经在运行，请查看已有窗口。',parent=root)
        root.destroy()
        if mutex:kernel.CloseHandle(mutex)
        return
    PRIVATE.mkdir(parents=True,exist_ok=True)
    stop=threading.Event();guard=threading.Lock();clock=ServerClock()
    current={};notified=False

    def publish(**event):
        event.update(updated_at=datetime.now(TZ).isoformat(),pid=os.getpid(),
            plan={'release':PLAN.release.isoformat(),'day':PLAN.day,'venue':PLAN.venue,'start':PLAN.start,'end':PLAN.end})
        with guard:
            current.clear();current.update(event)
            temporary=STATE_FILE.with_suffix('.tmp')
            temporary.write_text(json.dumps(event,ensure_ascii=False,indent=2),encoding='utf-8')
            temporary.replace(STATE_FILE)
            with EVENTS_FILE.open('a',encoding='utf-8') as log:
                log.write(json.dumps(event,ensure_ascii=False)+'\n')

    def worker():
        kernel.SetThreadExecutionState(0x80000001)
        try:
            publish(state='STARTING',message='正在检查场馆登录和预约条件')
            if SUBMITTED.exists():
                raise BookingError('今晚已经发起过订单提交，请先在钉钉核对结果')
            session,_=load_session(PLAN.release.timestamp())
            api=Api(session)
            Trial(api,PLAN,publish=publish,stop=stop,clock=clock,
                before_create=lambda active,site:mark_submission(SUBMITTED,active,site)).run(
                refresh_session=lambda:load_session(PLAN.release.timestamp())[0])
        except BookingError as exc:
            publish(state='FAILED',message=str(exc))
        except Exception:
            publish(state='FAILED',message='准备检查未通过，请保持钉钉场馆首页已登录后重试')
        finally:kernel.SetThreadExecutionState(0x80000000)

    root.title('羽毛球预约 · '+('周四测试' if MODE=='trial' else '周五正式'))
    root.geometry('620x550');root.minsize(580,520)
    root.configure(bg='#f5f7fa')
    frame=ttk.Frame(root,padding=26);frame.pack(fill='both',expand=True,padx=16,pady=16)
    ttk.Label(frame,text=f'{PLAN.release:%m 月 %d 日} 20:00 自动预约',font=('Microsoft YaHei UI',20,'bold')).pack(anchor='w')
    day_label='周四 11 月 5 日' if MODE=='trial' else '周五 11 月 6 日'
    ttk.Label(frame,text=day_label+' · 综合馆羽毛球\n优先 19:00–21:00；其次 19:00–20:00 或 20:00–21:00\n任意场地 · 费用上限 ¥0',
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
        with guard:event=dict(current)
        state=event.get('state','STARTING')
        heading.configure(text=LABELS.get(state,state))
        detail.configure(text=event.get('message','正在启动'))
        left=max(0,int(PLAN.release.timestamp()-clock.now()))
        timer.configure(text=f'距放场 {left//3600:02}:{left%3600//60:02}:{left%60:02}' if state not in END_STATES else '')
        if state in END_STATES:
            button.configure(state='disabled')
            if not notified:
                notified=True;root.bell();root.deiconify();root.lift()
                if state=='CREATED_PENDING_INVITE':
                    detail.configure(text=f"场地 {event.get('site_id')}，{event.get('booked_start')}–{event.get('booked_end')}。订单已核实。\n请现在到钉钉手动邀请同伴。")
        root.after(500,refresh)

    def close_when_stopped():
        stop.set()
        if thread.is_alive():
            button.configure(state='disabled');detail.configure(text='正在停止并核对，请稍候…')
            root.after(200,close_when_stopped)
        else:root.destroy()
    root.protocol('WM_DELETE_WINDOW',close_when_stopped)
    thread.start();refresh()
    try:root.mainloop()
    finally:
        stop.set();thread.join(timeout=12)
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.CloseHandle(mutex)

if __name__=='__main__':main()
