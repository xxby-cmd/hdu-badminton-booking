from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import leveldb_reader as reader

ORIGIN = b'_https://sportmeta.hdu.edu.cn\x00'


def integer(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result + bytes([value]))


def string(value):
    return integer(len(value)) + value


def masked(data):
    crc = reader.crc32c(data)
    return (((crc >> 15) | (crc << 17)) + 0xa282ead8) & 0xffffffff


def log_record(data, kind=1):
    return struct.pack('<IHB', masked(bytes([kind]) + data), len(data), kind) + data


def block(items):
    data, previous, restarts = bytearray(), b'', []
    for index, (key, value) in enumerate(items):
        shared = 0
        if index % 2 == 0:
            restarts.append(len(data))
        else:
            while shared < min(len(previous), len(key)) and previous[shared] == key[shared]:
                shared += 1
        data += integer(shared) + integer(len(key) - shared) + integer(len(value))
        data += key[shared:] + value
        previous = key
    restarts = restarts or [0]
    return bytes(data) + b''.join(struct.pack('<I', value) for value in restarts) + struct.pack('<I', len(restarts))


def snappy_literal(data):
    length = len(data) - 1
    if length < 60:
        literal = bytes([length << 2])
    else:
        count = (length.bit_length() + 7) // 8
        literal = bytes([(59 + count) << 2]) + length.to_bytes(count, 'little')
    return integer(len(data)) + literal + data


def table(values, compressed=False):
    items = [(ORIGIN + b'\x01' + name.encode() + ((seq << 8) | (value is not None)).to_bytes(8, 'little'),
              value or b'') for name, seq, value in values]
    raw = bytearray()

    def append(body, compressed=False):
        offset = len(raw)
        kind = int(compressed)
        stored = snappy_literal(body) if compressed else body
        raw.extend(stored + bytes([kind]) + struct.pack('<I', masked(stored + bytes([kind]))))
        return integer(offset) + integer(len(stored))

    data_handle = append(block(items), compressed)
    meta_handle = append(block([]))
    index_handle = append(block([(items[-1][0], data_handle)]))
    footer = meta_handle + index_handle
    return bytes(raw) + footer + bytes(40 - len(footer)) + struct.pack('<Q', 0xdb4775248b80fb57)


def batch(sequence, values):
    result = struct.pack('<QI', sequence, len(values))
    for name, value in values:
        key = ORIGIN + b'\x01' + name.encode()
        result += bytes([int(value is not None)]) + string(key)
        if value is not None:
            result += string(value)
    return result


def manifest(files, log=4, previous=0, deleted=()):
    edit = integer(1) + string(b'leveldb.BytewiseComparator')
    edit += integer(2) + integer(log) + integer(9) + integer(previous)
    for number, size in files:
        edit += integer(7) + integer(0) + integer(number) + integer(size) + string(b'') + string(b'')
    result = log_record(edit)
    if deleted:
        result += log_record(b''.join(integer(6) + integer(0) + integer(number) for number in deleted))
    return result


class LevelDBReaderTests(unittest.TestCase):
    def setup_db(self, folder, tables, wal=b'', obsolete=(), previous=0):
        root = Path(folder)
        for number, data in tables.items():
            (root / f'{number:06d}.ldb').write_bytes(data)
        (root / 'CURRENT').write_bytes(b'MANIFEST-000010\n')
        (root / 'MANIFEST-000010').write_bytes(manifest(
            [(number, len(data)) for number, data in tables.items()], previous=previous, deleted=obsolete))
        (root / '000004.log').write_bytes(wal)
        return root

    def test_active_manifest_excludes_obsolete_tables_and_logs(self):
        active = table([('openid', 10, b'\x01CURRENT'), ('phone', 11, b'\x011230')], True)
        obsolete = table([('openid', 999, b'\x01OBSOLETE')])
        with tempfile.TemporaryDirectory() as folder:
            root = self.setup_db(folder, {1: active, 2: obsolete}, log_record(batch(20, [('token', b'\x01FRESH')])), (2,))
            (root / '000003.log').write_bytes(log_record(batch(1000, [('openid', b'\x01OLD_LOG')])))
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            found = reader.read_origin(root, ORIGIN)
            self.assertEqual(found['openid'], (10, b'\x01CURRENT'))
            self.assertEqual(found['token'], (20, b'\x01FRESH'))
            self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, before)
            self.assertFalse((root / 'LOCK').exists())

    def test_wal_sequence_and_deletion_override_compressed_table(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.setup_db(folder, {1: table([('token', 3, b'OLD'), ('openid', 4, b'ID')], True)},
                log_record(batch(10, [('token', b'NEW'), ('openid', None)])))
            found = reader.read_origin(root, ORIGIN)
            self.assertEqual(found['token'], (10, b'NEW'))
            self.assertEqual(found['openid'], (11, None))

    def test_previous_log_is_recovered_but_unrelated_origins_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.setup_db(folder, {}, previous=3)
            (root / '000003.log').write_bytes(log_record(batch(2, [('token', b'PREVIOUS')])))
            self.assertEqual(reader.read_origin(root, ORIGIN)['token'], (2, b'PREVIOUS'))
            self.assertEqual(reader.read_origin(root, b'_https://unrelated.invalid\x00'), {})

    def test_active_table_tombstone_cannot_be_revived_by_older_wal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.setup_db(folder, {1: table([('token', 30, None)])},
                log_record(batch(20, [('token', b'OLDER_VALUE')])))
            self.assertEqual(reader.read_origin(root, ORIGIN)['token'], (30, None))

    def test_crc_reference_and_snappy_copy_variants(self):
        self.assertEqual(reader.crc32c(b'123456789'), 0xe3069283)
        self.assertEqual(reader._snappy(b'\x07\x08xab\x0e\x02\x00'), b'xababab')
        self.assertEqual(reader._snappy(b'\x08\x04ab\x09\x02'), b'abababab')
        self.assertEqual(reader._snappy(b'\x07\x08xab\x0f\x02\x00\x00\x00'), b'xababab')
        for invalid in (b'\x01\x02\x00\x00', b'\x05\x00x'):
            with self.subTest(invalid=invalid), self.assertRaises(reader.DataError):
                reader._snappy(invalid)

    def test_fragmented_logs_truncated_tail_and_corrupt_crc(self):
        first = b'a' * 32761
        raw = log_record(first, 2) + log_record(b'b', 4)
        self.assertEqual(list(reader.records(raw)), [first + b'b'])
        good = log_record(b'complete')
        self.assertEqual(list(reader.records(good + log_record(b'unfinished')[:-3])), [b'complete'])
        self.assertEqual(list(reader.records(log_record(b'unfinished', 2))), [])
        bad = bytearray(good)
        bad[-1] ^= 1
        with self.assertRaises(reader.DataError):
            list(reader.records(bad))

    def test_corrupt_active_table_or_manifest_is_not_silently_skipped(self):
        for target in ('000001.ldb', 'MANIFEST-000010'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as folder:
                root = self.setup_db(folder, {1: table([('token', 2, b'VALUE')], True)})
                path = root / target
                data = bytearray(path.read_bytes())
                data[8] ^= 1
                path.write_bytes(data)
                with self.assertRaises(reader.DataError):
                    reader.read_origin(root, ORIGIN)

    def test_changing_source_is_retried_only_three_times(self):
        inventory = [([], [], bytes([i])) for i in range(6)]
        with patch.object(reader, '_inventory', side_effect=inventory) as inspect:
            with self.assertRaises(reader.SourceUnavailable):
                reader.read_origin(Path('UNUSED_TEST_PATH'), ORIGIN)
        self.assertEqual(inspect.call_count, 6)

    def test_current_cannot_escape_source_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'CURRENT').write_bytes(b'../MANIFEST-000001\n')
            with self.assertRaises(reader.DataError):
                reader.read_origin(root, ORIGIN)

    def test_restart_offset_must_point_to_an_unshared_entry(self):
        good = bytearray(block([(b'prefix-one', b'1'), (b'prefix-two', b'2')]))
        struct.pack_into('<I', good, len(good) - 8, 1)
        with self.assertRaises(reader.DataError):
            list(reader._entries(good))


if __name__ == '__main__':
    unittest.main()
