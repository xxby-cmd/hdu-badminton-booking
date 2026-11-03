"""Read only the user's sportmeta origin from Chromium's recent local-storage log."""
from pathlib import Path
import struct
import json
import base64
import ctypes
from ctypes import wintypes
import os
from datetime import datetime, timezone

ORIGIN = b"_https://sportmeta.hdu.edu.cn\x00"
DB = Path(os.environ['LOCALAPPDATA']) / 'DingTalk_133/Default/Local Storage/leveldb'
PRIVATE = Path(os.environ.get('HDU_BOOKING_STATE_DIR',str(Path(__file__).resolve().parent / '.private')))
CACHE = PRIVATE / 'session.dpapi'

def varint(b, pos):
    value = shift = 0
    while pos < len(b) and shift <= 63:
        byte = b[pos]; pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
        shift += 7
    raise ValueError("Incomplete varint")

def records(raw):
    fragments = bytearray()
    for block_at in range(0, len(raw), 32768):
        block = raw[block_at:block_at+32768]
        pos = 0
        while pos + 7 <= len(block):
            _, length, kind = struct.unpack_from('<IHB', block, pos)
            pos += 7
            if length == 0 and kind == 0: break
            if pos + length > len(block): break
            data = block[pos:pos+length]; pos += length
            if kind == 1: yield data
            elif kind == 2: fragments = bytearray(data)
            elif kind == 3: fragments.extend(data)
            elif kind == 4:
                fragments.extend(data); yield bytes(fragments); fragments.clear()

def origin_records():
    found = {}
    for filename in sorted(DB.glob('*.log')):
        for batch in records(filename.read_bytes()):
            if len(batch) < 12: continue
            sequence, count = struct.unpack_from('<QI', batch)
            pos = 12
            for index in range(count):
                kind = batch[pos]; pos += 1
                size, pos = varint(batch, pos)
                key = batch[pos:pos+size]; pos += size
                value = None
                if kind == 1:
                    size, pos = varint(batch, pos)
                    value = batch[pos:pos+size]; pos += size
                elif kind != 0: raise ValueError('Unknown LevelDB record')
                if key.startswith(ORIGIN):
                    name = key[len(ORIGIN):].lstrip(b'\x01').decode('utf-8')
                    previous = found.get(name, (-1, None))
                    if sequence + index >= previous[0]: found[name] = (sequence+index, value)
    return found

def decode_value(value):
    if value is None: return None
    if value[:1] == b'\x01': value = value[1:].decode('latin-1')
    elif value[:1] == b'\x00': value = value[1:].decode('utf-16-le')
    else: value = value.decode('utf-8')
    try:
        decoded = json.loads(value)
        return decoded.get('data', decoded) if isinstance(decoded, dict) else decoded
    except (ValueError, TypeError): return value

def read_live_session():
    origin = {k:decode_value(v) for k, (_,v) in origin_records().items()}
    def get(key): return origin.get(key, origin.get('uni-'+key))
    session = {k:get(k) for k in ['token','openid','nickname','phone','code']}
    if not session['token'] or not session['openid']:
        raise RuntimeError('No current sports session found')
    return session

def validate_session(session, valid_until=None):
    claims = json.loads(base64.urlsafe_b64decode(session['token'].split('.')[1]+'==='))
    if str(claims.get('openid')) != str(session['openid']):
        raise RuntimeError('Identity mismatch')
    minimum = max(datetime.now(timezone.utc).timestamp(), valid_until or 0) + 300
    if claims.get('exp',0) < minimum:
        raise RuntimeError('场馆登录有效期不足，请在钉钉场馆首页重新登录')
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
        session, claims = validate_session(read_live_session(),valid_until)
        PRIVATE.mkdir(parents=True,exist_ok=True)
        CACHE.write_bytes(protect(json.dumps(session,ensure_ascii=False).encode('utf-8')))
        return session, claims
    except (OSError,RuntimeError,ValueError,KeyError):
        if not CACHE.exists(): raise
        return validate_session(json.loads(protect(CACHE.read_bytes(),decrypt=True)),valid_until)

if __name__ == '__main__':
    _, claims = load_session()
    print('场馆登录已读取并加密保存；有效期至',datetime.fromtimestamp(claims['exp']).isoformat())
