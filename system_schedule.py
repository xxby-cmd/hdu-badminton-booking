"""Windows once-only scheduler for the same control panel used interactively."""
from datetime import datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import json
import re
from booking import TZ, BookingError
from plan_config import ensure_future, load_config

ROOT=Path(__file__).resolve().parent
NS='http://schemas.microsoft.com/windows/2004/02/mit/task'
ET.register_namespace('',NS)

class TaskRegistrationUncertain(BookingError):
    side_effect_uncertain=True
    def __init__(self,config,digest,name):
        super().__init__('任务可能已登记，但回读未完成，请刷新历史核实，勿重复启动')
        self.details={'config':str(config),'config_sha256':digest,'task_name':name,'pid':None}

def list_booking_tasks():
    script=("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
            "Get-ScheduledTask -TaskName 'HDU-Booking-*' -ErrorAction SilentlyContinue | "
            "ForEach-Object { [pscustomobject]@{name=$_.TaskName; enabled=($_.State.ToString() -ne 'Disabled'); "
            "arguments=$_.Actions.Arguments} } | ConvertTo-Json -Compress")
    process=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
                           capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
    if process.returncode:raise BookingError('无法读取本机预约任务，请稍后刷新历史')
    raw=process.stdout.decode('utf-8-sig').strip()
    if not raw:return []
    rows=json.loads(raw);rows=rows if isinstance(rows,list) else [rows]
    result=[]
    for row in rows:
        args=row.get('arguments','')
        match=re.search(r'(?:^|\s)--config\s+(?:"([^"]+)"|(\S+))',args)
        sha=re.search(r'(?:^|\s)--expected-sha256\s+([0-9a-f]{64})(?:\s|$)',args)
        if match and re.fullmatch(r'HDU-Booking-\d{8}',row.get('name','')):
            result.append({'name':row['name'],'enabled':row['enabled'],
                           'config':match.group(1) or match.group(2),'sha256':sha.group(1) if sha else None})
    return result

def disable_booking_task(name,expected_sha):
    tasks=list_booking_tasks()
    current=next((r for r in tasks if r['name']==name),None)
    if current is None or not current['enabled']:return
    if not expected_sha or current.get('sha256')!=expected_sha:
        raise BookingError('系统任务已经发生变化，请刷新历史后再删除，避免误删别的计划')
    process=subprocess.run(['schtasks.exe','/Change','/TN',name,'/DISABLE'],capture_output=True,
                           timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
    if process.returncode:raise BookingError('无法停用本地定时任务，计划未删除')
    refreshed=next((r for r in list_booking_tasks() if r['name']==name),None)
    if refreshed and refreshed['enabled']:raise BookingError('本地任务仍启用，计划未删除')

def command_for(config, expected):
    python=Path(sys.executable)
    pythonw=python.with_name('pythonw.exe')
    if pythonw.exists():python=pythonw
    args=subprocess.list2cmdline(['-E','-s','-B',str(ROOT/'startup.py'),
                                 '--config',str(Path(config).resolve()),
                                 '--expected-sha256',expected,'--start'])
    return str(python),args

def task_xml(plan, config, expected, user_sid, now=None):
    now=now or datetime.now(TZ)
    ensure_future(plan,now)
    when=plan.release-timedelta(hours=1)
    if when<=now:
        raise BookingError('已过提前一小时的启动点，请直接点击“现在开始等待放场”')
    def add(parent,name,text=None,**attrs):
        node=ET.SubElement(parent,'{'+NS+'}'+name,attrs)
        if text is not None:node.text=str(text)
        return node
    task=ET.Element('{'+NS+'}Task',{'version':'1.2'})
    info=add(task,'RegistrationInfo')
    add(info,'Description',f'{plan.day} {plan.venue} {plan.start}-{plan.end}，免费单单，手动邀请，不自动取消')
    triggers=add(task,'Triggers')
    trigger=add(triggers,'TimeTrigger')
    add(trigger,'StartBoundary',when.isoformat())
    add(trigger,'Enabled','true')
    principals=add(task,'Principals')
    principal=add(principals,'Principal',id='Author')
    add(principal,'UserId',user_sid);add(principal,'LogonType','InteractiveToken')
    add(principal,'RunLevel','LeastPrivilege')
    settings=add(task,'Settings')
    for key,value in {'MultipleInstancesPolicy':'IgnoreNew','DisallowStartIfOnBatteries':'false',
                      'StopIfGoingOnBatteries':'false','StartWhenAvailable':'true',
                      'RunOnlyIfNetworkAvailable':'false','AllowHardTerminate':'false',
                      'Enabled':'true','Hidden':'false','WakeToRun':'true',
                      'ExecutionTimeLimit':'PT3H'}.items():add(settings,key,value)
    actions=add(task,'Actions',Context='Author')
    action=add(actions,'Exec')
    executable,arguments=command_for(config,expected)
    add(action,'Command',executable);add(action,'Arguments',arguments)
    add(action,'WorkingDirectory',ROOT)
    return ET.tostring(task,encoding='unicode')

def register_task(config, expected, now=None):
    _,plan=load_config(config,expected)
    # Resolve SID without a shell or interpreting any user configuration as code.
    result=subprocess.run(['whoami.exe','/user','/fo','csv','/nh'],capture_output=True,
                          timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
    import re
    match=re.search(rb'S-1-[0-9-]+',result.stdout)
    if result.returncode or not match:raise BookingError('无法识别当前 Windows 账户')
    xml=task_xml(plan,config,expected,match.group().decode('ascii'),now)
    name=f'HDU-Booking-{plan.release:%Y%m%d}'
    temporary=None
    requested=False
    try:
        with tempfile.NamedTemporaryFile(suffix='.xml',delete=False) as f:
            temporary=Path(f.name);f.write(xml.encode('utf-16'))
        requested=True
        result=subprocess.run(['schtasks.exe','/Create','/TN',name,'/XML',str(temporary),'/F'],
                              capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:raise BookingError('Windows 定时任务登记失败，请使用当前账户或直接启动')
        check=subprocess.run(['schtasks.exe','/Query','/TN',name,'/XML'],capture_output=True,
                             timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
        if check.returncode:raise BookingError('任务登记后无法复核，请在任务计划程序检查')
        document=None
        for encoding in ('utf-16','utf-8-sig','gb18030'):
            try:
                document=ET.fromstring(check.stdout.decode(encoding))
                break
            except (UnicodeError,ET.ParseError):pass
        if document is None:raise BookingError('任务已登记，但无法核对 Windows 返回的任务配置')
        ns={'t':NS}
        expected_command,expected_args=command_for(config,expected)
        actual_time=document.findtext('.//t:TimeTrigger/t:StartBoundary',namespaces=ns)
        try:valid_time=datetime.fromisoformat(actual_time)==plan.release-timedelta(hours=1)
        except (TypeError,ValueError):valid_time=False
        if (not valid_time or document.findtext('.//t:Exec/t:Command',namespaces=ns)!=expected_command
                or document.findtext('.//t:Exec/t:Arguments',namespaces=ns)!=expected_args):
            raise BookingError('定时任务回读与预约计划不符，请检查 Windows 任务计划程序')
        # Query XML is retained only as non-private scheduling evidence by caller if needed.
        return {'task_name':name,'start_at':(plan.release-timedelta(hours=1)).isoformat(),
                'release':plan.release.isoformat(),'verified':True}
    except (BookingError,OSError,subprocess.SubprocessError) as exc:
        if requested:raise TaskRegistrationUncertain(config,expected,name) from exc
        raise
    finally:
        if temporary:temporary.unlink(missing_ok=True)
