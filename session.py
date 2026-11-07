"""Read the sportmeta session without modifying Chromium's LevelDB."""
from pathlib import Path
import json
import base64
import ctypes
from ctypes import wintypes
import os
import tempfile
import math
from datetime import datetime, timezone
from leveldb_reader import read_origin, varint, records

ORIGIN = b"_https://sportmeta.hdu.edu.cn\x00"
LOCAL_DATA = Path(os.environ['LOCALAPPDATA'])
DB = LOCAL_DATA / 'DingTalk_133/Default/Local Storage/leveldb'
PRIVATE = Path(os.environ.get('HDU_BOOKING_STATE_DIR',str(Path(__file__).resolve().parent / '.private')))
CACHE = PRIVATE / 'session.dpapi'

def origin_records():
    source=DB
    if not source.is_dir() and source==LOCAL_DATA/'DingTalk_133/Default/Local Storage/leveldb':
        alternatives=[p/'Default/Local Storage/leveldb' for p in LOCAL_DATA.glob('DingTalk*')
                      if (p.name=='DingTalk' or p.name.startswith('DingTalk_'))
                      and (p/'Default/Local Storage/leveldb').is_dir()]
        if len(alternatives)>1:
            raise SessionError('检测到多个钉钉数据目录，无法确认当前账号，请核对所用电脑版钉钉')
        if alternatives:source=alternatives[0]
    return read_origin(source, ORIGIN)


class SessionError(RuntimeError): pass
class SessionNotFound(SessionError): pass
class SessionAccountChanged(SessionError): pass


def decode_value(value):
    if value is None: return None
    if value[:1] == b'\x01': value = value[1:].decode('latin-1')
    elif value[:1] == b'\x00': value = value[1:].decode('utf-16-le')
    else: value = value.decode('utf-8')
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError): return value
    # uni-app writes strings verbatim; JSON-looking text is still a string.
    # Only a typed storage wrapper may replace the original text.
    if isinstance(decoded, dict) and set(decoded) == {'type', 'data'}:
        kind, data = decoded['type'], decoded['data']
        valid = (
            kind == 'string' and type(data) is str
            or kind == 'number' and type(data) in (int, float)
            or kind == 'boolean' and type(data) is bool
            or kind == 'object' and (data is None or type(data) in (dict, list))
        )
        if valid: return data
    return value

def read_live_session():
    current = origin_records()
    fields = ['token','openid','nickname','phone','code']
    if not any(key in current or 'uni-'+key in current for key in fields):
        raise SessionNotFound('本机尚无场馆登录资料')
    try:
        origin = {k:decode_value(v) for k, (_,v) in current.items()}
    except (ValueError, TypeError, UnicodeError) as exc:
        raise SessionError('场馆登录资料格式无效，请在钉钉重新登录') from exc
    def get(key): return origin.get(key, origin.get('uni-'+key))
    session = {k:get(k) for k in fields}
    if not all(session.values()):
        raise SessionError('场馆会话不完整或已注销，请在钉钉重新登录')
    return session


def _session_claims(session):
    try:
        token = session['token']
        if not isinstance(token,str) or len(token.split('.')) != 3:
            raise ValueError('Invalid token shape')
        claims = json.loads(base64.urlsafe_b64decode(token.split('.')[1]+'==='))
        if not isinstance(claims,dict) or claims.get('openid') is None or not session.get('openid'):
            raise ValueError('Missing identity')
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise SessionError('场馆登录凭证无效，请在钉钉重新登录') from exc
    if str(claims.get('openid')) != str(session['openid']):
        raise SessionError('场馆登录身份不一致，请在钉钉重新登录')
    return claims


def validate_session(session, valid_until=None):
    if not isinstance(session,dict) or not all(session.get(key) for key in ('token','openid','nickname','phone','code')):
        raise SessionError('场馆会话不完整，请在钉钉重新登录')
    claims = _session_claims(session)
    minimum = max(datetime.now(timezone.utc).timestamp(), valid_until or 0) + 300
    expiry = claims.get('exp')
    if type(expiry) not in (int,float) or not math.isfinite(expiry) or not expiry >= minimum:
        raise SessionError('场馆登录有效期不足，请在钉钉场馆首页重新登录')
    return session, claims


class Blob(ctypes.Structure):
    _fields_ = [('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]

def protect(data, decrypt=False):
    """DPAPI binds the stored session to this Windows user; no plaintext file."""
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypto = ctypes.WinDLL('crypt32',use_last_error=True)
    if decrypt:
        func = crypto.CryptUnprotectData
        func.argtypes = [ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        ok = func(ctypes.byref(source),None,None,None,None,1,ctypes.byref(result))
    else:
        func = crypto.CryptProtectData
        func.argtypes = [ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        ok = func(ctypes.byref(source),'HDU sports session',None,None,None,1,ctypes.byref(result))
    if not ok: raise ctypes.WinError(ctypes.get_last_error())
    try: return ctypes.string_at(result.data,result.size)
    finally:
        kernel = ctypes.WinDLL('kernel32')
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(result.data,ctypes.c_void_p))

def load_session(valid_until=None):
    try:
        live = read_live_session()
    except (OSError,SessionNotFound):
        if not CACHE.exists(): raise
        return validate_session(json.loads(protect(CACHE.read_bytes(),decrypt=True)),valid_until)
    live, claims = validate_session(live,valid_until)
    if CACHE.exists():
        try:
            previous = json.loads(protect(CACHE.read_bytes(),decrypt=True))
            _session_claims(previous)
        except (OSError,ValueError,TypeError,SessionError):
            previous = None
        if previous is not None and str(previous['openid']) != str(live['openid']):
            raise SessionAccountChanged('当前钉钉账号与已有场馆会话不同，请确认账号后重试')
    encrypted = protect(json.dumps(live,ensure_ascii=False).encode('utf-8'))
    PRIVATE.mkdir(parents=True,exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.session-',suffix='.tmp',dir=PRIVATE,delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,CACHE)
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)
    return live, claims


if __name__ == '__main__':
    _, claims = load_session()
    print('场馆登录已读取并加密保存；有效期至',datetime.fromtimestamp(claims['exp']).isoformat())
