"""Current-user logon startup and recovery of existing scheduled booking tasks."""
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from booking import BookingError, TZ
from plan_config import load_config, paths_for
from plan_history import PlanHistory
from session import PRIVATE
from system_schedule import list_booking_tasks

ROOT = Path(__file__).resolve().parent
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = 'HDUVenueAssistant'


def startup_command():
    executable = Path(sys.executable).with_name('pythonw.exe')
    if not executable.exists():
        executable = Path(sys.executable)
    return subprocess.list2cmdline([str(executable), '-E', '-s', '-B',
                                   str(ROOT / 'startup.py'), '--autostart'])


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_NAME)
        return value == startup_command()
    except FileNotFoundError:
        return False


def set_autostart(enabled):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass
    if autostart_enabled() != enabled:
        raise BookingError('开机启动设置回读失败，请检查当前 Windows 账户权限')


def due_record(task, records, private, now):
    """Admit only the latest enabled, future, unsubmitted scheduled revision."""
    if not task.get('enabled') or task.get('state') == 'Running' or not task.get('sha256'):
        return None
    row = next((r for r in reversed(records)
                if r.get('task_name') == task['name']
                and r.get('config_sha256') == task['sha256']), None)
    if row is None or row['status'] != 'scheduled':
        return None
    data, plan = load_config(task['config'], task['sha256'])
    if data != row['data'] or not plan.release - timedelta(hours=1) <= now < plan.release:
        return None
    paths = paths_for(plan, Path(private))
    if paths['validation'].exists() or paths['submitted'].exists():
        return None
    return row


def process_matches(row):
    """Check creation time too, so a reused Windows PID cannot impersonate a worker."""
    if not row.get('pid') or not row.get('started_at'):
        return True
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, row['pid'])
    if not handle:
        return ctypes.get_last_error() != 87  # Access denied or unknown errors remain blocked.
    try:
        creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return True
        if exit_time.dwLowDateTime or exit_time.dwHighDateTime:
            return False
        seconds = ((creation.dwHighDateTime << 32) | creation.dwLowDateTime) / 10000000 - 11644473600
        return abs(seconds - datetime.fromisoformat(row['started_at']).timestamp()) < 10
    finally:
        kernel.CloseHandle(handle)


def recover_due_tasks(private=PRIVATE, now=None, tasks=None, start=None, alive=None):
    now = now or datetime.now(TZ)
    tasks = list_booking_tasks() if tasks is None else tasks
    history = PlanHistory(private)
    started = []
    start = start or start_existing_task
    alive = alive or process_matches
    for task in tasks:
        with history.lock():
            document = history.load()
            latest = next((r for r in reversed(document['records'])
                           if r.get('task_name') == task['name']
                           and r.get('config_sha256') == task.get('sha256')), None)
            if task.get('enabled') and task.get('state') != 'Running' and latest:
                history.recover_waiting(latest, now, alive)
            row = due_record(task, document['records'], private, now)
            if row is None:
                continue
            last = row.get('boot_launch_at')
            if last and (now - datetime.fromisoformat(last)).total_seconds() < 60:
                continue
            if row.get('boot_launch_count', 0) >= 3:
                continue
            row['boot_launch_at'] = now.isoformat()
            row['boot_launch_count'] = row.get('boot_launch_count', 0) + 1
            history.save(document)
            # Windows IgnoreNew, immutable history IDs and the worker mutex remain authoritative.
            start(task['name'])
            started.append(task['name'])
    return started


def start_existing_task(name):
    import re
    if not re.fullmatch(r'HDU-Booking-\d{8}', name):
        raise BookingError('预约任务名称无效')
    result = subprocess.run(['schtasks.exe', '/Run', '/TN', name],
                            capture_output=True, timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise BookingError('已有预约任务暂时无法启动，后台会在期限内重查')


def launch_guardian():
    command = startup_command()
    # Use the same argument construction for registry and immediate activation.
    return subprocess.Popen(command, cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW)


def guardian_main():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel.CreateMutexW(None, False, 'Local\\HDU_VenueStartupGuardian')
    if not mutex:
        return 1
    duplicate = ctypes.get_last_error() == 183
    if duplicate:
        kernel.CloseHandle(mutex)
        return 0
    status_path = Path(PRIVATE) / 'boot-status.json'
    try:
        from login_settings import LoginSettings
        login = LoginSettings(PRIVATE)
        login.launch_client()
        try:
            recover_due_tasks()
            login.assist_with_retries(datetime.now(TZ).replace(hour=20, minute=0, second=0).timestamp())
        except (OSError, ValueError, BookingError, subprocess.SubprocessError):
            pass  # The booking worker publishes its own WAITING_LOGIN and keeps checking.
        executable=Path(sys.executable).with_name('pythonw.exe')
        if not executable.exists():executable=Path(sys.executable)
        subprocess.Popen([str(executable),'-E','-s','-B',str(ROOT/'startup.py'),'--tray'],
                         cwd=ROOT,creationflags=subprocess.CREATE_NO_WINDOW)
        while autostart_enabled():
            state = 'RUNNING'
            try:
                started = recover_due_tasks()
                message = '已检查本机待执行计划' if not started else '已唤起到时的预约任务'
            except (OSError, ValueError, BookingError, subprocess.SubprocessError):
                state = 'RETRYING'
                message = '计划检查暂时失败，将在一分钟内重查，请查看预约助手'
            temporary = status_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'pid': os.getpid(), 'state': state,
                'checked_at': datetime.now(TZ).isoformat(), 'message': message},
                ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, status_path)
            time.sleep(30)
    finally:
        kernel.CloseHandle(mutex)
    return 0
