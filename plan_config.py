"""User-editable plans. No identities, secrets, or booking writes in this module."""
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from booking import Plan, TZ, BookingError

SCHEMA = 1

def default_config(now=None):
    day = ((now or datetime.now(TZ)).date() + timedelta(days=3)).isoformat()
    return {'schema':SCHEMA, 'day':day, 'start':'15:00', 'end':'17:00',
            'venue':'综合馆羽毛球', 'venue_type':'badminton', 'site_ids':[],
            'max_price':0, 'allow_one_hour':False}

def parse_config(data):
    if not isinstance(data, dict) or set(data) != set(default_config()):
        raise BookingError('预约配置不完整或版本不匹配，请用界面重新保存')
    if type(data['schema']) is not int or data['schema'] != SCHEMA:
        raise BookingError('预约配置版本不匹配')
    try:
        day = date.fromisoformat(data['day'])
        if day.isoformat() != data['day']: raise ValueError()
        hours=[]
        for key in ('start','end'):
            if not isinstance(data[key],str) or not re.fullmatch(r'\d{2}:\d{2}',data[key]):raise ValueError()
            hours.append(time.fromisoformat(data[key]))
    except (TypeError,ValueError):
        raise BookingError('日期请填 YYYY-MM-DD，时间请填 HH:MM') from None
    minutes=lambda t:t.hour*60+t.minute
    duration=minutes(hours[1])-minutes(hours[0])
    if not 0 < duration <= 120:
        raise BookingError('请选同一天内、开始早于结束且不超过两小时的时段')
    venue=data['venue']
    if not isinstance(venue,str) or not venue.strip() or len(venue)>60 or any(ord(c)<32 for c in venue):
        raise BookingError('请填写学校系统中的完整场馆名称')
    if data['venue_type'] not in ('badminton','tennis'):
        raise BookingError('当前支持羽毛球和网球')
    if (data['venue_type']=='badminton' and '网球' in venue
            or data['venue_type']=='tennis' and '羽毛球' in venue):
        raise BookingError('运动项目与场馆名称不一致')
    if type(data['max_price']) not in (int,float) or data['max_price'] != 0:
        raise BookingError('本助手仅允许免费预约')
    if data['allow_one_hour'] is not False:
        raise BookingError('可编辑计划只预约所填完整时段，不自动缩短时间')
    sites=data['site_ids']
    if not isinstance(sites,list) or len(sites)>100 or any(type(i) is not int or not 1<=i<=1000 for i in sites) or len(set(sites))!=len(sites):
        raise BookingError('场地编号用逗号分隔的正整数，留空表示任意一片')
    release=datetime.combine(day-timedelta(days=2),time(20),TZ)
    return Plan(release=release,day=day.isoformat(),venue=venue.strip(),start=data['start'],
                end=data['end'],venue_type=data['venue_type'],max_price=0,
                allow_one_hour=False,site_ids=tuple(sorted(sites)))

def ensure_future(plan, now=None):
    if (now or datetime.now(TZ)) >= plan.release:
        raise BookingError('这份计划的放场时间已到或已过，请勿用旧计划重新提交')

def load_config(path, expected_sha256=None):
    try:
        raw=Path(path).read_bytes()
        if expected_sha256 and hashlib.sha256(raw).hexdigest()!=expected_sha256:
            raise BookingError('定时计划已被修改，已阻止自动启动，请重新登记任务')
        data=json.loads(raw.decode('utf-8-sig'))
    except (OSError,ValueError,UnicodeError):
        raise BookingError('无法读取预约配置，请重新选择或保存') from None
    return data,parse_config(data)

def save_config(path, data):
    parse_config(data)
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    raw=(json.dumps(data,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf-8')
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.plan-',delete=False) as f:
            temporary=Path(f.name);f.write(raw);f.flush();os.fsync(f.fileno())
        os.replace(temporary,path)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()

def paths_for(plan, private):
    private=Path(private)
    key=f'custom-{plan.release:%Y%m%d}'
    return {'state':private/f'status-{key}.json','events':private/f'events-{key}.jsonl',
            'stop':private/f'stop-{key}.json',
            'submitted':private/f'submitted-{plan.release:%Y%m%d}.json',
            'validation':private/f'validation-{plan.release:%Y%m%d}.json'}
