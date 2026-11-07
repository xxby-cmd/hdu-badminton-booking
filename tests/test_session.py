import json
import base64
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session import decode_value
import session
from leveldb_reader import DataError, SourceUnavailable


class SessionValueTests(unittest.TestCase):
    def test_unwrapped_numeric_identity_and_phone_remain_strings(self):
        for text in ('123456789', '13800000000', '001234', '1e3'):
            with self.subTest(text=text):
                decoded = decode_value(b'\x01' + text.encode('ascii'))
                self.assertEqual(decoded, text)
                self.assertIs(type(decoded), str)

    def test_unwrapped_json_text_is_not_a_typed_storage_value(self):
        for text in ('true', 'false', 'null', '"12345"', '[1,2]',
                     '{"data":"12345"}', '{"nickname":"TEST_ONLY"}'):
            with self.subTest(text=text):
                self.assertEqual(decode_value(text.encode('utf-8')), text)

    def test_valid_typed_values_keep_the_stored_type(self):
        cases = [('string', '12345'), ('number', 12345), ('number', 1.5),
                 ('boolean', False), ('object', {'test': 1}),
                 ('object', [1, 2]), ('object', None)]
        for kind, data in cases:
            with self.subTest(kind=kind, data=data):
                raw = json.dumps({'type': kind, 'data': data}).encode('utf-8')
                decoded = decode_value(raw)
                self.assertEqual(decoded, data)
                self.assertIs(type(decoded), type(data))

    def test_invalid_or_extra_wrapper_fields_do_not_change_the_text(self):
        cases = [
            {'type': 'number', 'data': '12345'},
            {'type': 'number', 'data': True},
            {'type': 'string', 'data': 12345},
            {'type': 'boolean', 'data': 1},
            {'type': 'object', 'data': True},
            {'type': 'unsupported', 'data': '12345'},
            {'type': 'string', 'data': '12345', 'extra': True},
        ]
        for wrapper in cases:
            with self.subTest(wrapper=wrapper):
                text = json.dumps(wrapper)
                self.assertEqual(decode_value(text.encode('utf-8')), text)

    def test_chromium_text_encodings_and_deleted_values(self):
        self.assertIsNone(decode_value(None))
        self.assertEqual(decode_value(b'\x01'), '')
        self.assertEqual(decode_value(b'\x01caf\xe9'), 'caf\xe9')
        text = '测试昵称123'
        self.assertEqual(decode_value(b'\x00' + text.encode('utf-16-le')), text)
        wrapped = json.dumps({'type': 'string', 'data': text}, ensure_ascii=False)
        self.assertEqual(decode_value(b'\x00' + wrapped.encode('utf-16-le')), text)


class SessionLoadingTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.private = Path(folder) / 'private'
        self.private.mkdir()
        self.cache = self.private / 'session.dpapi'
        self.enterContext(patch.object(session, 'PRIVATE', self.private))
        self.enterContext(patch.object(session, 'CACHE', self.cache))
        self.old = self.make_session('TEST_ACCOUNT')
        self.new = self.make_session('TEST_ACCOUNT', extra='fresh')
        self.cache.write_bytes(b'OLD_ENCRYPTED_ONLY')

        def crypt(data, decrypt=False):
            if decrypt:
                values = {b'OLD_ENCRYPTED_ONLY': self.old, b'NEW_ENCRYPTED_ONLY': self.new}
                return json.dumps(values[data]).encode()
            self.assertEqual(json.loads(data), self.new)
            return b'NEW_ENCRYPTED_ONLY'

        self.crypt = self.enterContext(patch.object(session, 'protect', side_effect=crypt))

    def make_session(self, identity, extra='', expired=False):
        claims = {'openid': identity, 'exp': datetime.now(timezone.utc).timestamp() + (-1 if expired else 3600),
                  'test': extra}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        return {'token': 'TEST.' + payload + '.TEST', 'openid': identity,
                'nickname': 'TEST_NICKNAME', 'phone': 'TEST_PHONE', 'code': 'TEST_CODE'}

    def test_explicit_logout_and_incomplete_new_source_never_use_old_cache(self):
        for records in ({'token': (9, None)}, {'token': (9, b'\x01NEW_TOKEN')},
                        {'openid': (9, b'\x01OTHER_ACCOUNT')}):
            with self.subTest(keys=list(records)), patch.object(session, 'origin_records', return_value=records):
                with self.assertRaises(session.SessionError):
                    session.load_session()
                self.crypt.assert_not_called()
                self.assertEqual(self.cache.read_bytes(), b'OLD_ENCRYPTED_ONLY')

    def test_invalid_or_expired_live_session_does_not_fall_back(self):
        for live in (self.make_session('TEST_ACCOUNT', expired=True),
                     {**self.new, 'token': 'INVALID'},
                     {**self.new, 'openid': 'OTHER_ACCOUNT'}):
            with self.subTest(live_kind=type(live).__name__), patch.object(session, 'read_live_session', return_value=live):
                with self.assertRaises(session.SessionError):
                    session.load_session()
                self.crypt.assert_not_called()

    def test_identity_switch_is_explicit_and_does_not_overwrite_cache(self):
        with patch.object(session, 'read_live_session', return_value=self.make_session('OTHER_ACCOUNT')):
            with self.assertRaisesRegex(session.SessionError, '账号'):
                session.load_session()
        self.assertEqual(self.cache.read_bytes(), b'OLD_ENCRYPTED_ONLY')

    def test_cache_fallback_only_for_unavailable_or_absent_source_and_checks_expiry(self):
        for cause in (SourceUnavailable('TEST_UNREADABLE'), session.SessionNotFound('TEST_ABSENT')):
            with self.subTest(cause=type(cause).__name__), patch.object(session, 'read_live_session', side_effect=cause):
                self.assertEqual(session.load_session()[0], self.old)
        self.old = self.make_session('TEST_ACCOUNT', expired=True)
        with patch.object(session, 'read_live_session', side_effect=SourceUnavailable('TEST_UNREADABLE')):
            with self.assertRaises(session.SessionError):
                session.load_session()
        with patch.object(session, 'read_live_session', side_effect=DataError('TEST_CRC')):
            with self.assertRaises(DataError):
                session.load_session()

    def test_incomplete_cache_cannot_supply_a_session_when_source_is_absent(self):
        self.old = {'token': self.old['token'], 'openid': self.old['openid']}
        with patch.object(session, 'read_live_session', side_effect=session.SessionNotFound('TEST_ABSENT')):
            with self.assertRaises(session.SessionError):
                session.load_session()

    def test_live_session_atomically_refreshes_only_encrypted_cache(self):
        replace = session.os.replace
        snapshots = []

        def inspect_replace(source, target):
            snapshots.append((Path(source).read_bytes(), self.cache.read_bytes()))
            replace(source, target)

        with patch.object(session, 'read_live_session', return_value=self.new), \
             patch.object(session.os, 'replace', side_effect=inspect_replace):
            result, _ = session.load_session()
        self.assertEqual(result, self.new)
        self.assertEqual(snapshots, [(b'NEW_ENCRYPTED_ONLY', b'OLD_ENCRYPTED_ONLY')])
        self.assertEqual(self.cache.read_bytes(), b'NEW_ENCRYPTED_ONLY')
        self.assertEqual(list(self.private.iterdir()), [self.cache])

    def test_cache_write_failure_is_not_masked_by_old_cache(self):
        with patch.object(session, 'read_live_session', return_value=self.new), \
             patch.object(session.os, 'replace', side_effect=OSError('TEST_WRITE_FAILURE')):
            with self.assertRaises(OSError):
                session.load_session()
        self.assertEqual(self.cache.read_bytes(), b'OLD_ENCRYPTED_ONLY')
        self.assertEqual(list(self.private.iterdir()), [self.cache])


if __name__ == '__main__':
    unittest.main(verbosity=2)
