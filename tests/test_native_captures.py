"""Synthetic screenshots exercise provenance checks, never real-run semantics."""
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
from report_lib import ValidationError, digest
from test_capture_responses import png_bytes


class NativeCaptureTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('native_captures')
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'run'
        (self.root / 'screenshots').mkdir(parents=True)
        write_json(self.root / 'trace-plan.json', {'fixture': 'plan'})
        write_json(self.root / 'trace-review.json', {'fixture': 'main analysis'})
        (self.root / 'trace-report.html').write_text('report HTML is not screenshot provenance')
        arms = {}
        for arm in ('baseline', 'treatment'):
            folder = self.root / 'arms' / arm
            folder.mkdir(parents=True)
            events = [{'type': 'thread.started', 'thread_id': arm},
                      {'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'cat input.txt', 'aggregated_output': 'input', 'exit_code': 0}},
                      {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 5}}]
            (folder / 'events.jsonl').write_bytes(('\r\n'.join(json.dumps(x) for x in events) + '\r\n').encode())
            (folder / 'answer.txt').write_bytes(b'answer\r\n')
            (folder / 'stderr.txt').write_bytes(b'fixture warning\r\n')
            files = {key: f'arms/{arm}/{filename}' for key, filename in [('events', 'events.jsonl'), ('answer', 'answer.txt'), ('stderr', 'stderr.txt')]}
            arms[arm] = {'files': files, 'sha256': {key: digest(self.root / path) for key, path in files.items()}}
        write_json(self.root / 'trace-run.json', {'arms': arms})
        self.raw = png_bytes()
        (self.root / 'screenshots/log.png').write_bytes(self.raw)

    def register(self, **overrides):
        args = {'run_dir': self.root, 'image': 'screenshots/log.png', 'title': 'B 원본 로그',
                'captured_at': '2026-11-17T14:00:00+09:00', 'surface': 'Codex app terminal',
                'basis': 'saved_log_terminal', 'arm': 'treatment', 'source_file': 'arms/treatment/events.jsonl',
                'display_command': "sed -n '1,3p' arms/treatment/events.jsonl", 'visible_lines': {'start': 1, 'end': 3}}
        args.update(overrides)
        return self.module.register_capture(**args)

    def test_absent_capture_is_lazy_and_cli_retains_image_and_log_bytes(self):
        original_import = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name == 'capture_responses' or name.startswith('PIL'):
                raise AssertionError('decoder imported without any capture')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=guarded):
            self.assertEqual(self.module.load_native_captures(self.root), [])
        source = self.root / 'arms/treatment/events.jsonl'
        raw_log = source.read_bytes()
        result = subprocess.run([sys.executable, str(SCRIPTS / 'native_captures.py'), str(self.root),
                                 '--image', 'screenshots/log.png', '--title', 'B 원본 로그',
                                 '--captured-at', '2026-11-17T14:00:00+09:00', '--surface', 'Codex app terminal',
                                 '--basis', 'saved_log_terminal', '--arm', 'treatment',
                                 '--source-file', 'arms/treatment/events.jsonl', '--display-command', 'cat events.jsonl',
                                 '--visible-lines', '1:3'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        image, = self.module.load_native_captures(self.root)
        self.assertEqual(base64.b64decode(image['data_uri'].split(',')[1]), self.raw)
        self.assertEqual(source.read_bytes(), raw_log)
        self.assertEqual(image['visible_lines'], {'start': 1, 'end': 3})
        self.assertIn('실행 종료 후 촬영', image['caption'])
        self.assertFalse((self.root / 'screenshots/log-view.html').exists())

    def test_analysis_and_report_can_change_but_execution_and_image_cannot(self):
        record = self.register()
        before, = self.module.load_native_captures(self.root)
        (self.root / 'trace-review.json').write_text('new main analysis')
        (self.root / 'trace-report.html').unlink()
        self.assertEqual(self.module.load_native_captures(self.root), [before])
        self.assertNotIn('trace-review.json', record['source_inputs'])
        self.assertEqual(len(record['source_inputs']), 6)
        manifest = (self.root / self.module.MANIFEST).read_bytes()
        for relative in list(record['source_inputs']) + ['screenshots/log.png']:
            with self.subTest(path=relative):
                path = self.root / relative
                original = path.read_bytes()
                path.write_bytes(original + b'changed')
                with self.assertRaises(ValidationError):
                    self.module.load_native_captures(self.root)
                with self.assertRaises(ValidationError):
                    self.register()
                self.assertEqual((self.root / self.module.MANIFEST).read_bytes(), manifest)
                path.write_bytes(original)

    def test_paths_arm_and_declared_log_source_are_enforced(self):
        for name in ('../log.png', '/private/tmp/log.png', 'arms/log.png', 'screenshots/log.svg', 'screenshots/../../log.png'):
            with self.subTest(image=name), self.assertRaises(ValidationError):
                self.register(image=name)
        outside = Path(self.temp.name) / 'outside.png'
        outside.write_bytes(self.raw)
        (self.root / 'screenshots/link.png').symlink_to(outside)
        with self.assertRaises(ValidationError):
            self.register(image='screenshots/link.png')
        for source in ('../events.jsonl', 'arms/baseline/events.jsonl', 'arms/treatment/answer.txt', 'trace-report.html'):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                self.register(source_file=source)
        with self.assertRaises(ValidationError):
            self.register(arm='both')
        record = self.register(source_file='arms/treatment/stderr.txt', visible_lines={'start': 1, 'end': 1})
        self.assertEqual(record['source_kind'], 'stderr')
        self.assertEqual(len(record['source_inputs']), 7)

    def test_capture_kind_line_range_and_reasoning_boundaries(self):
        for basis in ('actual_jsonl_view', 'report_screenshot', 'submitted_response_render'):
            with self.subTest(basis=basis), self.assertRaises(ValidationError):
                self.register(basis=basis)
        for scope in ({'start': 0, 'end': 1}, {'start': True, 'end': 2}, {'start': 3, 'end': 2}, {'start': 1, 'end': 4}):
            with self.subTest(lines=scope), self.assertRaises(ValidationError):
                self.register(visible_lines=scope)
        path = self.root / 'arms/treatment/events.jsonl'
        path.write_text(json.dumps({'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'not public evidence'}}) + '\n')
        record = json.loads((self.root / 'trace-run.json').read_text())
        record['arms']['treatment']['sha256']['events'] = digest(path)
        write_json(self.root / 'trace-run.json', record)
        with self.assertRaises(ValidationError):
            self.register(visible_lines={'start': 1, 'end': 1})

    def test_raw_log_browser_is_labeled_distinctly_and_requires_safe_url(self):
        for url in (None, 'file:///tmp/report.html', 'javascript:alert(1)'):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                self.register(basis='raw_log_browser', page_url=url)
        self.register(basis='raw_log_browser', surface='Aside · 원본 JSONL',
                      page_url='http://127.0.0.1:8765/arms/treatment/events.jsonl')
        capture, = self.module.load_native_captures(self.root)
        self.assertIn('Codex 앱 화면 아님', capture['caption'])
        self.assertIn('실행 종료 후 촬영', capture['caption'])
        manifest = json.loads((self.root / self.module.MANIFEST).read_text())
        manifest['captures'][0]['basis'] = 'actual_jsonl_view'
        write_json(self.root / self.module.MANIFEST, manifest)
        with self.assertRaises(ValidationError):
            self.module.load_native_captures(self.root)

    def test_re_registration_corrupt_image_and_write_failure_preserve_records(self):
        (self.root / 'screenshots/log.png').write_bytes(png_bytes(compressed=b'invalid pixels'))
        with self.assertRaises(ValidationError):
            self.register()
        self.assertFalse((self.root / self.module.MANIFEST).exists())
        (self.root / 'screenshots/log.png').write_bytes(self.raw)
        self.register()
        manifest = (self.root / self.module.MANIFEST).read_bytes()
        with self.assertRaises(ValidationError):
            self.register()
        (self.root / 'screenshots/other.png').write_bytes(self.raw)
        with patch.object(self.module.os, 'replace', side_effect=OSError('fixture disk error')):
            with self.assertRaises(ValidationError):
                self.register(image='screenshots/other.png')
        self.assertEqual((self.root / self.module.MANIFEST).read_bytes(), manifest)
        self.assertEqual(len(self.module.load_native_captures(self.root)), 1)


if __name__ == '__main__':
    unittest.main()
