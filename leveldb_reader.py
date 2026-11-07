"""Read-only, origin-scoped LevelDB recovery using the standard library."""
from pathlib import Path
import re
import struct

MAX_FILE = 128 * 1024 * 1024
MAX_BLOCK = 16 * 1024 * 1024


class DataError(ValueError):
    """The source is corrupt or unsupported; do not silently use old credentials."""


class SourceUnavailable(OSError):
    pass


def varint(raw, position):
    value = 0
    for shift in range(0, 64, 7):
        if position >= len(raw):
            raise DataError('Incomplete LevelDB integer')
        byte = raw[position]
        position += 1
        if shift == 63 and byte > 1:
            raise DataError('LevelDB integer overflow')
        value |= (byte & 127) << shift
        if byte < 128:
            return value, position
    raise DataError('Invalid LevelDB integer')


def _crc_table():
    table = []
    for value in range(256):
        for _ in range(8):
            value = (value >> 1) ^ (0x82F63B78 if value & 1 else 0)
        table.append(value)
    return table


_CRC = _crc_table()


def crc32c(data):
    crc = 0xffffffff
    for byte in data:
        crc = _CRC[(crc ^ byte) & 255] ^ (crc >> 8)
    return crc ^ 0xffffffff


def _masked(data):
    crc = crc32c(data)
    return (((crc >> 15) | (crc << 17)) + 0xa282ead8) & 0xffffffff


def records(raw):
    """Yield CRC-checked complete logical records; ignore only an unfinished tail."""
    fragments = None
    for block_at in range(0, len(raw), 32768):
        block = raw[block_at:block_at + 32768]
        position = 0
        while position + 7 <= len(block):
            checksum, size, kind = struct.unpack_from('<IHB', block, position)
            position += 7
            if checksum == size == kind == 0:
                if any(block[position:]):
                    raise DataError('Invalid LevelDB padding')
                break
            if position + size > len(block):
                if block_at + len(block) == len(raw):
                    return
                raise DataError('LevelDB fragment crosses a block')
            data = block[position:position + size]
            position += size
            if checksum != _masked(bytes([kind]) + data):
                raise DataError('LevelDB log checksum mismatch')
            if kind == 1:
                if fragments is not None:
                    raise DataError('Unfinished LevelDB fragment sequence')
                yield data
            elif kind == 2:
                if fragments is not None:
                    raise DataError('Overlapping LevelDB fragments')
                fragments = bytearray(data)
            elif kind in (3, 4):
                if fragments is None:
                    raise DataError('Orphan LevelDB fragment')
                fragments.extend(data)
                if len(fragments) > MAX_BLOCK:
                    raise DataError('LevelDB record too large')
                if kind == 4:
                    yield bytes(fragments)
                    fragments = None
            else:
                raise DataError('Unsupported LevelDB log record')
        if len(block) == 32768 and any(block[position:]):
            raise DataError('Invalid LevelDB trailer')


def _slice(raw, position):
    size, position = varint(raw, position)
    end = position + size
    if end > len(raw):
        raise DataError('Truncated LevelDB field')
    return raw[position:end], end


def _manifest(raw):
    tables, log, previous = {}, 0, 0
    seen = False
    for edit in records(raw):
        seen = True
        position = 0
        while position < len(edit):
            tag, position = varint(edit, position)
            if tag == 1:
                comparator, position = _slice(edit, position)
                if comparator != b'leveldb.BytewiseComparator':
                    raise DataError('Unsupported LevelDB comparator')
            elif tag in (2, 3, 4, 9):
                value, position = varint(edit, position)
                if tag == 2:
                    log = value
                elif tag == 9:
                    previous = value
            elif tag == 5:
                _, position = varint(edit, position)
                _, position = _slice(edit, position)
            elif tag in (6, 7):
                level, position = varint(edit, position)
                number, position = varint(edit, position)
                if level > 6:
                    raise DataError('Unsupported LevelDB table level')
                if tag == 6:
                    tables.pop((level, number), None)
                else:
                    size, position = varint(edit, position)
                    _, position = _slice(edit, position)
                    _, position = _slice(edit, position)
                    tables[level, number] = size
            else:
                raise DataError('Unsupported LevelDB manifest tag')
    if not seen:
        raise DataError('Empty LevelDB manifest')
    return tables, log, previous


def _snappy(raw):
    size, position = varint(raw, 0)
    if size > MAX_BLOCK:
        raise DataError('Snappy block too large')
    result = bytearray()
    while position < len(raw):
        tag = raw[position]
        position += 1
        kind = tag & 3
        length = tag >> 2
        if kind == 0:
            if length >= 60:
                count = length - 59
                if position + count > len(raw):
                    raise DataError('Truncated Snappy literal length')
                length = int.from_bytes(raw[position:position + count], 'little')
                position += count
            length += 1
            if position + length > len(raw) or len(result) + length > size:
                raise DataError('Invalid Snappy literal')
            result.extend(raw[position:position + length])
            position += length
        else:
            count = 1 if kind == 1 else 2 if kind == 2 else 4
            if position + count > len(raw):
                raise DataError('Truncated Snappy copy')
            offset = int.from_bytes(raw[position:position + count], 'little')
            position += count
            if kind == 1:
                offset |= (tag & 224) << 3
                length = 4 + ((tag >> 2) & 7)
            else:
                length += 1
            if not 0 < offset <= len(result) or len(result) + length > size:
                raise DataError('Invalid Snappy copy')
            for _ in range(length):
                result.append(result[-offset])
    if len(result) != size:
        raise DataError('Snappy size mismatch')
    return bytes(result)


def _handle(raw, position):
    offset, position = varint(raw, position)
    size, position = varint(raw, position)
    return (offset, size), position


def _block(raw, location):
    offset, size = location
    if size > MAX_BLOCK or offset + size + 5 > len(raw) - 48:
        raise DataError('Invalid LevelDB block bounds')
    body = raw[offset:offset + size]
    kind = raw[offset + size]
    expected = struct.unpack_from('<I', raw, offset + size + 1)[0]
    if _masked(body + bytes([kind])) != expected:
        raise DataError('LevelDB table checksum mismatch')
    if kind == 0:
        return body
    if kind == 1:
        return _snappy(body)
    raise DataError('Unsupported LevelDB compression')


def _entries(raw):
    if len(raw) < 8:
        raise DataError('Truncated LevelDB restart array')
    count = struct.unpack_from('<I', raw, len(raw) - 4)[0]
    end = len(raw) - 4 * (count + 1)
    if not count or end < 0:
        raise DataError('Invalid LevelDB restart count')
    restarts = [struct.unpack_from('<I', raw, end + 4 * i)[0] for i in range(count)]
    if restarts[0] != 0 or restarts != sorted(set(restarts)) or restarts[-1] > end:
        raise DataError('Invalid LevelDB restart offsets')
    starts, key, position = set(), b'', 0
    while position < end:
        start = position
        starts.add(start)
        shared, position = varint(raw, position)
        extra, position = varint(raw, position)
        size, position = varint(raw, position)
        if shared > len(key) or position + extra + size > end or (start in restarts and shared):
            raise DataError('Invalid LevelDB prefix entry')
        key = key[:shared] + raw[position:position + extra]
        position += extra
        value = raw[position:position + size]
        position += size
        yield key, value
    if position != end or (end and not set(restarts).issubset(starts)):
        raise DataError('Invalid LevelDB restart target')


def _table(raw):
    if len(raw) < 48 or raw[-8:] != struct.pack('<Q', 0xdb4775248b80fb57):
        raise DataError('Invalid LevelDB table footer')
    meta, position = _handle(raw, len(raw) - 48)
    index, position = _handle(raw, position)
    if position > len(raw) - 8:
        raise DataError('Invalid LevelDB footer handles')
    for _, value in _entries(_block(raw, meta)):
        location, end = _handle(value, 0)
        if end != len(value):
            raise DataError('Invalid LevelDB metadata handle')
        _block(raw, location)
    for _, value in _entries(_block(raw, index)):
        location, end = _handle(value, 0)
        if end != len(value):
            raise DataError('Invalid LevelDB index handle')
        yield from _entries(_block(raw, location))


def _read(path):
    if path.stat().st_size > MAX_FILE:
        raise DataError('LevelDB file exceeds read limit')
    with path.open('rb') as stream:
        raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise DataError('LevelDB file exceeds read limit')
    return raw


def _inventory(db):
    current = _read(db / 'CURRENT')
    try:
        name = current.decode('ascii').strip()
    except UnicodeError as exc:
        raise DataError('Invalid LevelDB CURRENT') from exc
    if not re.fullmatch(r'MANIFEST-\d+', name):
        raise DataError('Invalid LevelDB manifest name')
    manifest_path = db / name
    manifest = _read(manifest_path)
    tables, minimum_log, previous_log = _manifest(manifest)
    paths = []
    for (_, number), expected_size in sorted(tables.items()):
        path = db / f'{number:06d}.ldb'
        if not path.exists():
            path = db / f'{number:06d}.sst'
        if path.stat().st_size != expected_size:
            raise DataError('LevelDB table size mismatch')
        paths.append(path)
    logs = sorted(path for path in db.glob('*.log')
                  if re.fullmatch(r'\d+\.log', path.name)
                  and (int(path.stem) >= minimum_log or int(path.stem) == previous_log))
    files = [db / 'CURRENT', manifest_path] + paths + logs
    fingerprint = (current, manifest, tuple((path.name, path.stat().st_size,
                    path.stat().st_mtime_ns) for path in files))
    return paths, logs, fingerprint


def read_origin(db, prefix, retries=3):
    """Read only active files, merge sequence numbers and retain tombstones."""
    db = Path(db)
    if not 1 <= retries <= 3:
        raise ValueError('LevelDB read retries must be between one and three')
    for _ in range(retries):
        before = None
        try:
            tables, logs, before = _inventory(db)
            found = {}

            def merge(key, sequence, value):
                if not key.startswith(prefix):
                    return
                encoded = key[len(prefix):]
                try:
                    if encoded[:1] == b'\x01':
                        name = encoded[1:].decode('latin-1')
                    elif encoded[:1] == b'\x00':
                        name = encoded[1:].decode('utf-16-le')
                    else:
                        name = encoded.decode('utf-8')
                except UnicodeError as exc:
                    raise DataError('Invalid origin key encoding') from exc
                if sequence >= found.get(name, (-1, None))[0]:
                    found[name] = sequence, value

            for path in tables:
                for key, value in _table(_read(path)):
                    if len(key) < 8:
                        raise DataError('Truncated LevelDB internal key')
                    tag = int.from_bytes(key[-8:], 'little')
                    if tag & 255 not in (0, 1):
                        raise DataError('Unsupported LevelDB value kind')
                    merge(key[:-8], tag >> 8, value if tag & 255 else None)
            for path in logs:
                for batch in records(_read(path)):
                    if len(batch) < 12:
                        raise DataError('Truncated LevelDB write batch')
                    sequence, count = struct.unpack_from('<QI', batch)
                    position = 12
                    for index in range(count):
                        if position >= len(batch):
                            raise DataError('Truncated LevelDB batch record')
                        kind = batch[position]
                        position += 1
                        key, position = _slice(batch, position)
                        value = None
                        if kind == 1:
                            value, position = _slice(batch, position)
                        elif kind != 0:
                            raise DataError('Unsupported LevelDB batch kind')
                        merge(key, sequence + index, value)
                    if position != len(batch):
                        raise DataError('Unexpected LevelDB batch bytes')
            if _inventory(db)[2] == before:
                return found
        except DataError:
            try:
                if before is None or _inventory(db)[2] == before:
                    raise
            except OSError:
                pass
        except OSError:
            pass
    raise SourceUnavailable('场馆本机数据不可读或正在变化，请稍后重试')
