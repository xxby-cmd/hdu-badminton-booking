"""Run the existing tests with outbound networking disabled before one live test."""
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from booking import BookingError, TZ


ROOT = Path(__file__).resolve().parent
REPORT = ROOT / '制作记录/网球单次验证_20261103/仿真验收.json'
REQUIRED_GROUPS = ('test_timing.', 'test_session.', 'test_leveldb_reader.',
                   'test_once_journal.', 'test_single_test.', 'test_resilient_trial.',
                   'test_event_log.')


def source_hashes(root=ROOT):
    paths = set(root.glob('*.py')) | set(root.glob('*.pyw')) | set(root.glob('*.cmd'))
    paths.update((root / 'tests').glob('test_*.py'))
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def require_current_simulation():
    try:
        report = json.loads(REPORT.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise BookingError('尚未通过本版本的离线仿真，禁止真实测试') from exc
    if (report.get('passed') is not True or report.get('network_blocked') is not True
            or report.get('sources') != source_hashes()):
        raise BookingError('代码变化或仿真未通过，禁止真实测试')
    return report


def _test_ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _test_ids(test)
        else:
            yield test.id()


def run():
    before = source_hashes()
    output = io.StringIO()
    def reject_network(*args, **kwargs):
        raise AssertionError('离线仿真禁止真实网络请求')

    with patch.object(socket.socket, 'connect', reject_network), \
         patch.object(socket.socket, 'connect_ex', reject_network), \
         patch.object(socket, 'create_connection', reject_network), \
         patch.object(socket, 'getaddrinfo', reject_network):
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
        identifiers = list(_test_ids(suite))
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    after = source_hashes()
    missing = [group for group in REQUIRED_GROUPS
               if not any(identifier.startswith(group) for identifier in identifiers)]
    report = {
        'measured_at': datetime.now(TZ).isoformat(),
        'passed': result.wasSuccessful() and not missing and before == after and not result.skipped,
        'network_blocked': True, 'tests_run': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'missing_groups': missing,
        'sources_unchanged_during_run': before == after,
        'sources': after, 'test_ids': identifiers,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.with_name('仿真输出.txt').write_text(output.getvalue(), encoding='utf-8')
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(output.getvalue())
    print(json.dumps({key: value for key, value in report.items()
                      if key not in ('sources', 'test_ids')}, ensure_ascii=False))
    return report['passed']


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
