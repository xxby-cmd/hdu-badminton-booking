from pathlib import Path
import hashlib,re,unittest,zipfile
ROOT=Path(__file__).resolve().parents[1]
class BootstrapBundleTests(unittest.TestCase):
 def test_runtime_matches_pinned_checksum(self):
  digest=hashlib.sha256((ROOT/'runtime/hdu-runtime-win64.zip').read_bytes()).hexdigest()
  script=(ROOT/'bootstrap.ps1').read_text(encoding='utf-8-sig')
  self.assertIn(digest,script)
  self.assertEqual(set(re.findall(r"'[a-f0-9]{64}'",script)),{"'"+digest+"'"})
 def test_runtime_contains_tk_licenses_and_no_user_state(self):
  with zipfile.ZipFile(ROOT/'runtime/hdu-runtime-win64.zip') as z:
   names=set(z.namelist())
   self.assertIn('.runtime/python.exe',names)
   self.assertIn('.runtime/pythonw.exe',names)
   self.assertIn('.runtime/LICENSE.txt',names)
   self.assertTrue(any(n.startswith('.runtime/tcl/tk8.6/') for n in names))
   self.assertTrue(any(n.endswith('license.terms') for n in names))
   for n in names:
    self.assertTrue(n.startswith('.runtime/'))
    self.assertNotIn('..',Path(n).parts)
    self.assertFalse(any(part in {'.private','.git','site-packages','__pycache__'} for part in Path(n).parts))
   self.assertIsNone(z.testzip())
