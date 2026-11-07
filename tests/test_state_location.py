from pathlib import Path
import tempfile
import unittest
from state_location import resolve_state

class SharedStateTests(unittest.TestCase):
    def test_source_then_portable_keeps_original_uncertain_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);local=base/'local'
            source=base/'source';legacy=source/'.private';legacy.mkdir(parents=True)
            marker=legacy/'validation-20261108.json';marker.write_bytes(b'{"stage":"unknown"}')
            portable=base/'portable';portable.mkdir()
            self.assertEqual(resolve_state(source,local),legacy.resolve())
            self.assertEqual(resolve_state(portable,local),legacy.resolve())
            self.assertEqual(marker.read_bytes(),b'{"stage":"unknown"}')

    def test_portable_then_another_source_imports_legacy_guard_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);local=base/'local'
            portable=base/'portable';portable.mkdir()
            shared=resolve_state(portable,local)
            source=base/'other';legacy=source/'.private';legacy.mkdir(parents=True)
            (legacy/'submitted-20261108.json').write_bytes(b'old unknown request')
            self.assertEqual(resolve_state(source,local),shared)
            self.assertEqual((shared/'submitted-20261108.json').read_bytes(),b'old unknown request')
            (shared/'submitted-20261108.json').write_bytes(b'already authoritative')
            resolve_state(source,local)
            self.assertEqual((shared/'submitted-20261108.json').read_bytes(),b'already authoritative')
            self.assertTrue((legacy/'submitted-20261108.json').exists())

    def test_missing_or_corrupt_binding_never_creates_new_guard_store(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);local=base/'local'
            source=base/'source';source.mkdir()
            shared=resolve_state(source,local)
            shared.rmdir()
            with self.assertRaises(RuntimeError):resolve_state(base/'copy',local)
            self.assertFalse(shared.exists())
            binding=local/'HDUVenueAssistant/state-location.json'
            binding.write_text('{}')
            with self.assertRaises(RuntimeError):resolve_state(source,local)

if __name__=='__main__':unittest.main()
