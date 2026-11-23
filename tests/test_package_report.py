"""Verify reports using only the shipped skill, from an unrelated working folder."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_trace_report import make_run


PACKAGE = Path(__file__).resolve().parents[1] / 'skill-ab-test'


class PackageReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.package = self.folder / 'installed' / 'skill-ab-test'
        shutil.copytree(PACKAGE, self.package, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        self.run = make_run(self.folder / 'run')
        self.cwd = self.folder / 'unrelated-project'
        self.cwd.mkdir()

    def invoke(self, *args, package=None, run=None):
        return subprocess.run(
            [sys.executable, '-E', '-s', '-B', str((package or self.package) / 'scripts/trace_report.py'), str(run or self.run), *args],
            cwd=self.cwd, capture_output=True, text=True, timeout=30,
        )

    def test_shipped_folder_renders_identically_after_relocation_without_modification(self):
        before = {p.relative_to(self.package): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.package.rglob('*') if p.is_file()}
        original = self.invoke(package=PACKAGE)
        self.assertEqual(original.returncode, 0, original.stderr)
        expected = (self.run / 'trace-report.html').read_bytes()
        relocated = self.folder / 'different-user-records'
        shutil.copytree(self.run, relocated)
        (relocated / 'trace-report.html').unlink()
        copied = self.invoke(run=relocated)
        self.assertEqual(copied.returncode, 0, copied.stderr)
        self.assertEqual((relocated / 'trace-report.html').read_bytes(), expected)
        after = {p.relative_to(self.package): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.package.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_check_accepts_exact_output_without_writing(self):
        self.assertEqual(self.invoke().returncode, 0)
        target = self.run / 'trace-report.html'
        original, modified = target.read_bytes(), target.stat().st_mtime_ns
        checked = self.invoke('--check')
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(target.stat().st_mtime_ns, modified)

    def test_check_rejects_manual_html_edit_without_replacing_it(self):
        self.assertEqual(self.invoke().returncode, 0)
        target = self.run / 'trace-report.html'
        target.write_bytes(target.read_bytes().replace(b'</style>', b'body { color: red; }</style>'))
        edited, modified = target.read_bytes(), target.stat().st_mtime_ns
        checked = self.invoke('--check')
        self.assertEqual(checked.returncode, 1, checked.stderr)
        self.assertIn('does not match', checked.stderr)
        self.assertEqual(target.read_bytes(), edited)
        self.assertEqual(target.stat().st_mtime_ns, modified)
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.invoke('--check').returncode, 0)

    def test_check_rejects_stale_review_data(self):
        self.assertEqual(self.invoke().returncode, 0)
        review_path = self.run / 'trace-review.json'
        review = json.loads(review_path.read_text())
        review['takeaway'] = '검토 내용 갱신'
        review_path.write_text(json.dumps(review, ensure_ascii=False), encoding='utf-8')
        checked = self.invoke('--check')
        self.assertEqual(checked.returncode, 1, checked.stderr)
        self.assertIn('does not match', checked.stderr)

    def test_check_missing_report_does_not_generate_it(self):
        checked = self.invoke('--check')
        self.assertEqual(checked.returncode, 1, checked.stderr)
        self.assertIn('does not exist', checked.stderr)
        self.assertFalse((self.run / 'trace-report.html').exists())


if __name__ == '__main__':
    unittest.main()
