"""Local opt-in DingTalk login settings, encrypted for the current Windows user."""
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import uuid
import time

from booking import BookingError, TZ
from plan_history import PlanHistory
from session import protect

ROOT = Path(__file__).resolve().parent


def detect_client():
    script = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
              "Get-Process DingTalk -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Path -Unique")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
    paths = [line.strip() for line in result.stdout.decode('utf-8', errors='replace').splitlines()
             if line.strip()]
    # Powershell's legacy pipe encoding cannot represent every installed path.
    if len(paths) == 1 and Path(paths[0]).is_file():
        return paths[0]
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r'dingtalk\shell\open\command') as key:
            command, _ = winreg.QueryValueEx(key, None)
        import re
        match = re.match(r'"([^"]+\.exe)"', command)
        if match and Path(match.group(1)).is_file():
            return match.group(1)
    except FileNotFoundError:
        pass
    return ''


class LoginSettings:
    def __init__(self, private):
        self.private = Path(private)
        self.path = self.private / 'dingtalk-login.dpapi'
        self.guard = PlanHistory(self.private)
        self.guard.lock_path = self.private / 'dingtalk-auth.lock'

    def read(self):
        if not self.path.exists():
            return {'schema': 1, 'authorized': False, 'client': '', 'launch_client': True}
        try:
            data = json.loads(protect(self.path.read_bytes(), decrypt=True))
            if data['schema'] != 1 or type(data['authorized']) is not bool:
                raise ValueError()
            return data
        except (OSError, ValueError, KeyError, TypeError):
            raise BookingError('本机钉钉登录设置无法解密，请在设置中重新填写') from None

    def save(self, *, client, launch_client, authorized, account='', password=''):
        if type(authorized) is not bool or type(launch_client) is not bool:
            raise BookingError('登录授权选项无效')
        client = client.strip()
        if client and (Path(client).name.lower() != 'dingtalk.exe' or not Path(client).is_file()):
            raise BookingError('请选择本机安装的 DingTalk.exe')
        if authorized and (not client or not account.strip() or not password):
            raise BookingError('授权自动登录时，请填写钉钉账号、密码并选择客户端')
        if '\n' in account or '\r' in account or '\x00' in account or '\x00' in password:
            raise BookingError('账号或密码包含不支持的字符')
        data = {'schema': 1, 'authorized': authorized, 'client': client,
                'launch_client': launch_client, 'revision': uuid.uuid4().hex,
                'account': account.strip() if authorized else '',
                'password': password if authorized else ''}
        encrypted = protect(json.dumps(data, ensure_ascii=False).encode('utf-8'))
        with self.guard.lock():
            temporary = self.path.with_suffix('.tmp')
            with temporary.open('wb') as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)

    def launch_client(self):
        try:
            data = self.read()
            client = Path(data['client'])
            if not data.get('launch_client') or not client.is_file() or client.name.lower() != 'dingtalk.exe':
                return False
            launcher = client.parents[2] / 'DingtalkLauncher.exe' if len(client.parents) > 2 else client
            command = [str(launcher), '/run:desktop'] if launcher.is_file() and launcher != client else [str(client)]
            subprocess.Popen(command, cwd=Path(command[0]).parent,
                             creationflags=subprocess.CREATE_NO_WINDOW)
            return True
        except (BookingError, OSError, KeyError):
            return False

    def active_client(self, configured):
        """Follow the official updater's current/current_new switch, same install only."""
        original = Path(configured)
        versions = {'current', 'current_new'}
        if original.parent.name.lower() not in versions:
            return configured
        running = Path(detect_client())
        if (running.is_file() and running.name.lower() == 'dingtalk.exe'
                and original.parent.name.lower() in versions
                and running.parent.name.lower() in versions
                and original.parent.parent == running.parent.parent):
            return str(running)
        return configured

    def assist(self, release):
        # Revocation and submission share a separate auth lock. A completed revocation
        # cannot be followed by an older authorization's credential submission.
        with self.guard.lock():
            data = self.read()
            if not data['authorized']:
                return 'MANUAL'
            self.launch_client()
            identity = self.login_process_identity()
            if identity is None:
                return 'NO_FORM'
            pid, started = identity
            key = data['revision'] + ':' + datetime.fromtimestamp(release, TZ).date().isoformat() + ':' + str(pid) + ':' + started
            journal = self.private / 'dingtalk-login-attempts.json'
            attempts = json.loads(journal.read_text(encoding='utf-8')) if journal.exists() else {}
            if attempts.get(key) not in (None, 'NO_FORM', 'HOST_BLOCKED_BEFORE_EXECUTION'):
                return 'MANUAL'
            attempts[key] = 'RESERVED'
            self.record_attempts(journal, attempts)
            data = {**data, 'client': self.active_client(data['client']),
                    'expected_pid': pid, 'expected_started': started}
            # Secrets are stdin only, never command arguments, environment or clipboard.
            process = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                      '-Command', self.provider_command()],
                                     input=json.dumps(data, ensure_ascii=True).encode('ascii') + b'\n',
                                     capture_output=True, timeout=18,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            result = process.stdout.decode('ascii', errors='replace').strip()
            if process.returncode or result not in ('SUBMITTED', 'NO_FORM', 'MANUAL', 'ALREADY_LOGGED_IN'):
                return 'MANUAL'
            if result in ('NO_FORM', 'ALREADY_LOGGED_IN'):
                # The native provider returns NO_FORM only before either field is filled.
                attempts[key] = 'NO_FORM'
                self.record_attempts(journal, attempts)
            return result

    def assist_with_retries(self, release, *, attempts=10, pause=time.sleep):
        """Retry readiness, retaining the persistent credential submission guard."""
        if not 1 <= attempts <= 10:
            raise ValueError('Login attempts must be between 1 and 10')
        submitted = False
        result = 'NO_FORM'
        for number in range(1, attempts + 1):
            if not self.read()['authorized']:
                return 'MANUAL'
            try:
                if self.client_logged_in():
                    result = 'ALREADY_LOGGED_IN'
                elif not submitted:
                    result = self.assist(release)
                    submitted = result == 'SUBMITTED'
            except (OSError, ValueError, subprocess.SubprocessError):
                result = 'CHECK_FAILED'
            self.record_attempts(self.private / 'dingtalk-login-status.json', {
                'checked_at': datetime.now(TZ).isoformat(), 'attempt': number,
                'max_attempts': attempts, 'state': result,
                'credential_submitted': submitted})
            if result in ('ALREADY_LOGGED_IN', 'MANUAL'):
                return result
            if number < attempts:
                pause(2)
        return result

    def provider_command(self, probe=False):
        code = (ROOT / 'dingtalk_login.ps1').read_text(encoding='utf-8-sig')
        return '& {\n' + code + '\n}' + (' -Probe' if probe else '')

    def client_logged_in(self):
        result = self.probe_client()
        return result.returncode == 0 and result.stdout.strip() == b'ALREADY_LOGGED_IN'

    def login_process_identity(self):
        result = self.probe_client()
        if result.returncode:
            return None
        try:
            state = json.loads(result.stdout)
            if state['state'] != 'LOGIN_REQUIRED' or type(state['pid']) is not int or state['pid'] <= 0:
                return None
            return state['pid'], str(state['started'])
        except (ValueError, KeyError, TypeError):
            return None

    def probe_client(self):
        target = {'client': self.active_client(self.read()['client'])}
        return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                               '-Command', self.provider_command(probe=True)],
                              input=json.dumps(target,ensure_ascii=True).encode('ascii')+b'\n',
                              capture_output=True, timeout=15,
                              creationflags=subprocess.CREATE_NO_WINDOW)

    def record_attempts(self, journal, attempts):
        temporary = journal.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(attempts, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, journal)


def test_login(store):
    """Verify a user-requested login test through the venue's read-only order query."""
    from venue_startup import ENTRANCE
    from session import read_live_session, validate_session
    from booking import Api
    store.launch_client()
    try:
        previous_token = read_live_session()['token']
    except (RuntimeError, OSError, ValueError):
        previous_token = None
    os.startfile(ENTRANCE)
    result = 'ALREADY_LOGGED_IN' if store.client_logged_in() else store.assist_with_retries(
        datetime.now(TZ).replace(hour=20, minute=0, second=0).timestamp())
    if result not in ('SUBMITTED', 'ALREADY_LOGGED_IN'):
        return '自动登录未完成（最多检查10轮）。请查看钉钉密码错误、验证码或设备确认提示；诊断状态已保存在本机。'
    # The pre-login deep link is normally delivered once login completes.
    # Reopen only if that first window did not establish a fresh venue session.
    for check in range(16):
        if check == 8:
            os.startfile(ENTRANCE)
        time.sleep(2)
        try:
            credentials = read_live_session()
            if credentials['token'] == previous_token:
                continue
            validate_session(credentials)
        except (RuntimeError, OSError, ValueError):
            continue
        class ReadOnly(Api):
            def call(self, endpoint, *args, **kwargs):
                if endpoint != 'post_order_info':
                    raise BookingError('登录测试只允许读取本人预约')
                return super().call(endpoint, *args, **kwargs)
        api = ReadOnly(credentials)
        try:
            api.orders(False)
            return '已取得新的学校场馆登录，并通过只读查询。'
        finally:
            if api.connection:
                api.connection.close()
    return '已提交钉钉登录，尚未核实新的场馆会话，请查看钉钉主界面或处理验证码。'


def show_settings(panel):
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    from boot_service import autostart_enabled, set_autostart, launch_guardian
    from session import PRIVATE
    store = LoginSettings(PRIVATE)
    saved = store.read()
    dialog = tk.Toplevel(panel.root)
    dialog.title('开机启动与钉钉登录')
    dialog.geometry('700x680')
    dialog.minsize(660, 650)
    frame = ttk.Frame(dialog, padding=22)
    frame.pack(fill='both', expand=True)
    boot = tk.BooleanVar(value=autostart_enabled())
    client_start = tk.BooleanVar(value=saved.get('launch_client', True))
    consent = tk.BooleanVar(value=saved['authorized'])
    client = tk.StringVar(value=saved['client'] or detect_client())
    account = tk.StringVar(value=saved.get('account', ''))
    password = tk.StringVar()
    ttk.Checkbutton(frame, text='登录 Windows 后自动运行预约守护程序', variable=boot).pack(anchor='w', pady=5)
    ttk.Label(frame, text='守护程序每30秒检查本机计划，到时唤起已有定时任务。\n请在放场前开机并登录 Windows，保持联网。',
              wraplength=590).pack(anchor='w', pady=(4, 14))
    ttk.Checkbutton(frame, text='启动时打开钉钉客户端', variable=client_start).pack(anchor='w')
    ttk.Label(frame, text='钉钉客户端位置').pack(anchor='w', pady=(12, 3))
    line = ttk.Frame(frame)
    line.pack(fill='x')
    ttk.Entry(line, textvariable=client).pack(side='left', fill='x', expand=True)
    def browse():
        path = filedialog.askopenfilename(parent=dialog, title='选择 DingTalk.exe',
                                           filetypes=[('钉钉客户端', 'DingTalk.exe')])
        if path:
            client.set(path)
    ttk.Button(line, text='选择', command=browse).pack(side='right', padx=(8, 0))
    ttk.Separator(frame).pack(fill='x', pady=16)
    ttk.Checkbutton(frame, text='我授权本机加密保存账号密码，自动填入钉钉并提交登录',
                    variable=consent).pack(anchor='w')
    ttk.Label(frame, text='钉钉账号').pack(anchor='w', pady=(12, 3))
    account_entry = ttk.Entry(frame, textvariable=account)
    account_entry.pack(fill='x')
    ttk.Label(frame, text='钉钉密码（已保存时留空表示沿用）').pack(anchor='w', pady=(10, 3))
    password_entry = ttk.Entry(frame, textvariable=password, show='●')
    password_entry.pack(fill='x')
    ttk.Label(frame, text='密码使用当前 Windows 账户加密。取消授权并保存会清除已存密码。\n仅识别本机指定钉钉的账号密码页。验证码、扫码和设备确认需本人完成。',
              wraplength=590).pack(anchor='w', pady=14)
    def update_fields(*args):
        state = 'normal' if consent.get() else 'disabled'
        account_entry.configure(state=state)
        password_entry.configure(state=state)
    consent_trace = consent.trace_add('write', update_fields)
    def cleanup(event):
        if event.widget is dialog:
            consent.trace_remove('write', consent_trace)
            password.set('')
    dialog.bind('<Destroy>', cleanup, add='+')
    update_fields()
    def save(test=False):
        try:
            if test and not consent.get():
                raise BookingError('测试自动登录前，请先勾选本机登录授权')
            secret = password.get()
            if consent.get() and not secret:
                if account.get().strip() != saved.get('account', ''):
                    raise BookingError('更换账号时请重新填写密码')
                secret = saved.get('password', '')
            store.save(client=client.get(), launch_client=client_start.get(), authorized=consent.get(),
                       account=account.get(), password=secret)
            set_autostart(boot.get())
            if boot.get():
                launch_guardian()
            password.set('')
            dialog.destroy()
            panel.set_detail('开机与登录设置已保存。可再次打开此窗口查看或关闭。')
            if test:
                import threading
                panel.set_detail('正在验证授权登录，请留意钉钉的验证码或设备确认提示。')
                def verify():
                    try:
                        message = test_login(store)
                    except Exception:
                        message = '登录验证未完成，请在钉钉处理登录或验证码。'
                    panel.messages.put(('login_test', message))
                threading.Thread(target=verify, daemon=True).start()
        except (BookingError, OSError, subprocess.SubprocessError) as exc:
            messagebox.showerror('设置未完成', str(exc) if isinstance(exc, BookingError) else '本机设置保存失败，请检查权限后重试', parent=dialog)
    buttons = ttk.Frame(frame)
    buttons.pack(fill='x', pady=4)
    ttk.Button(buttons, text='保存并测试登录', command=lambda: save(True)).pack(side='right')
    ttk.Button(buttons, text='保存设置', command=save).pack(side='right', padx=8)
    return dialog
