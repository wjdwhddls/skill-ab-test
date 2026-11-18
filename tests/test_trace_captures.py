"""Synthetic screenshots verify immutable trace capture records, never real runs."""
import base64
import builtins
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fixture_factory import SCRIPTS, write_json
from report_lib import ValidationError
from test_capture_responses import png_bytes


class TraceCaptureTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('trace_captures')
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'run'
        (self.root / 'screenshots').mkdir(parents=True)
        write_json(self.root / 'trace-plan.json', {'fixture': 'plan'})
        write_json(self.root / 'trace-review.json', {'fixture': 'main-authored review'})
        arms = {}
        for arm in ('baseline', 'treatment'):
            files = {'events': f'arms/{arm}/events.jsonl', 'answer': f'arms/{arm}/answer.txt'}
            for key, name in files.items():
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((key + '\r\n').encode())
            arms[arm] = {'files': files}
        write_json(self.root / 'trace-run.json', {'arms': arms})
        self.html = b'<!doctype html><html><body>fixture log view</body></html>\r\n'
        (self.root / 'trace-report.html').write_bytes(self.html)
        self.raw = png_bytes()
        self.image = self.root / 'screenshots/log.png'
        self.image.write_bytes(self.raw)

    def register(self, image='screenshots/log.png'):
        return self.module.register_capture(self.root, image, '실행 로그',
                                            '2026-11-17T10:00:00+09:00',
                                            'http://127.0.0.1:8765/trace-report.html')

    def test_absent_capture_has_no_decoder_dependency_and_cli_preserves_bytes(self):
        original_import = builtins.__import__
        def guarded_import(name, *args, **kwargs):
            if name == 'capture_responses' or name.startswith('PIL'):
                raise AssertionError('decoder imported for absent screenshots')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=guarded_import):
            self.assertEqual(self.module.load_trace_captures(self.root), [])
        process = subprocess.run([sys.executable, str(SCRIPTS / 'trace_captures.py'), str(self.root),
                                  '--image', 'screenshots/log.png', '--title', '실행 로그',
                                  '--captured-at', '2026-11-17T10:00:00+09:00',
                                  '--page-url', 'http://127.0.0.1:8765/trace-report.html'],
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        capture, = self.module.load_trace_captures(self.root)
        self.assertEqual((capture['title'], capture['width'], capture['height']), ('실행 로그', 2, 2))
        self.assertEqual(base64.b64decode(capture['data_uri'].split(',')[1]), self.raw)
        snapshot = self.root / 'screenshots/log-view.html'
        self.assertEqual(snapshot.read_bytes(), self.html)
        manifest = json.loads((self.root / 'trace-captures.json').read_text())
        self.assertEqual(len(manifest['captures'][0]['source_inputs']), 7)
        self.assertEqual(manifest['basis'], 'actual_jsonl_view')
        (self.root / 'trace-report.html').write_text('later HTML includes the captured image')
        self.assertEqual(self.module.load_trace_captures(self.root), [capture])
        self.assertEqual(snapshot.read_bytes(), self.html)

    def test_paths_and_existing_snapshot_are_protected(self):
        for name in ('../log.png', 'screenshots/../../log.png', '/private/tmp/log.png',
                     'arms/log.png', 'screenshots/log.svg'):
            with self.subTest(path=name), self.assertRaises(ValidationError):
                self.register(name)
        outside = Path(self.temp.name) / 'outside.png'
        outside.write_bytes(self.raw)
        (self.root / 'screenshots/link.png').symlink_to(outside)
        with self.assertRaises(ValidationError):
            self.register('screenshots/link.png')
        snapshot = self.root / 'screenshots/log-view.html'
        snapshot.write_bytes(b'existing evidence')
        with self.assertRaises(ValidationError):
            self.register()
        self.assertEqual(snapshot.read_bytes(), b'existing evidence')
        self.assertFalse((self.root / 'trace-captures.json').exists())
        self.assertEqual((self.root / 'trace-report.html').read_bytes(), self.html)

    def test_source_image_snapshot_and_metadata_changes_reject_without_refresh(self):
        self.register()
        manifest_path = self.root / 'trace-captures.json'
        original_manifest = manifest_path.read_bytes()
        record = json.loads(original_manifest)['captures'][0]
        for relative in list(record['source_inputs']) + ['screenshots/log.png', 'screenshots/log-view.html']:
            with self.subTest(path=relative):
                path = self.root / relative
                original = path.read_bytes()
                path.write_bytes(original + b'changed')
                with self.assertRaises(ValidationError):
                    self.module.load_trace_captures(self.root)
                with self.assertRaises(ValidationError):
                    self.register()
                self.assertEqual(manifest_path.read_bytes(), original_manifest)
                path.write_bytes(original)
        for key, value in (('width', True), ('height', 3), ('captured_at', 'yesterday'),
                           ('page_url', 'javascript:alert(1)'), ('view', 'trace-report.html'),
                           ('source_inputs', {}), ('basis', 'submitted_response_render')):
            with self.subTest(field=key):
                manifest = json.loads(original_manifest)
                manifest['captures'][0][key] = value
                write_json(manifest_path, manifest)
                with self.assertRaises(ValidationError):
                    self.module.load_trace_captures(self.root)
        manifest_path.write_bytes(original_manifest)
        with self.assertRaises(ValidationError):
            self.register()
        self.assertEqual(manifest_path.read_bytes(), original_manifest)

    def test_corrupt_pixels_and_manifest_write_failure_leave_evidence_intact(self):
        self.image.write_bytes(png_bytes(compressed=b'not a zlib stream'))
        with self.assertRaises(ValidationError):
            self.register()
        self.assertFalse((self.root / 'trace-captures.json').exists())
        self.assertFalse((self.root / 'screenshots/log-view.html').exists())
        self.image.write_bytes(self.raw)
        self.register()
        manifest = (self.root / 'trace-captures.json').read_bytes()
        (self.root / 'screenshots/second.png').write_bytes(self.raw)
        with patch.object(self.module.os, 'replace', side_effect=OSError('fixture disk error')):
            with self.assertRaises(ValidationError):
                self.register('screenshots/second.png')
        self.assertEqual((self.root / 'trace-captures.json').read_bytes(), manifest)
        self.assertEqual((self.root / 'screenshots/log-view.html').read_bytes(), self.html)
        self.assertFalse((self.root / 'screenshots/second-view.html').exists())
        self.assertEqual(len(self.module.load_trace_captures(self.root)), 1)


if __name__ == '__main__':
    unittest.main()
