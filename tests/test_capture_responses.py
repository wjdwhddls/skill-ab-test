"""Checks screenshot provenance and response display without browser automation."""
import importlib
import base64
import hashlib
from io import BytesIO
import json
from html.parser import HTMLParser
from pathlib import Path
import struct
import tempfile
import unittest
import zlib
from unittest.mock import patch

from fixture_factory import make_run, write_json
from report_lib import ValidationError, capture_answers, load_run


def png_bytes(width=2, height=2, *, color_type=2, depth=8, interlace=0, pixels=None, compressed=None):
    # Synthetic test pixels only. Never included in a real comparison run.
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    header = struct.pack('>IIBBBBB', width, height, depth, color_type, 0, 0, interlace)
    if pixels is None:
        channels = 4 if color_type == 6 else 3
        pixels = b''.join(b'\0' + b'\xff' * channels * width for _ in range(height))
    if compressed is None:
        compressed = zlib.compress(pixels)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + chunk(b'IDAT', compressed) + chunk(b'IEND', b'')


def jpeg_bytes():
    # One test-only JPEG, unrelated to all real captured responses.
    from PIL import Image
    target = BytesIO()
    Image.new('RGB', (1, 1), 'white').save(target, format='JPEG')
    return target.getvalue()


class AnswerHTML(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.text = ''
        self.tags = []
        self.active = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if attrs.get('data-role') == 'answer-text':
            self.active = True

    def handle_endtag(self, tag):
        if tag == 'pre':
            self.active = False

    def handle_data(self, value):
        if self.active:
            self.text += value


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('capture_responses')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = make_run(Path(self.temp.name) / 'run')

    def recapture(self):
        (self.root / 'answers-lock.json').unlink()
        capture_answers(self.root)

    def prepare_and_register(self):
        self.module.prepare_views(self.root)
        image = self.root / 'screenshots/Q1-baseline.png'
        image.write_bytes(png_bytes())
        return self.module.register_capture(self.root, 'Q1', 'baseline', 'screenshots/Q1-baseline.png', '2026-11-17T07:00:00Z')

    def test_six_views_preserve_exact_escaped_answers(self):
        path = self.root / 'baseline/answers.json'
        answers = json.loads(path.read_text())
        text = '\n수치는 3이다.\r\n<script>bad()</script>{{TITLE}} & 끝'
        answers['answers'][0]['text'] = text
        write_json(path, answers)
        result = json.loads((self.root / 'result.json').read_text())
        result['comparisons'][0]['a'] = {'kind': 'quote', 'start': 1, 'end': 6, 'text': '수치는 3'}
        write_json(self.root / 'result.json', result)
        self.recapture()
        plan = self.module.prepare_views(self.root)
        self.assertEqual(len(plan['captures']), 6)
        html = (self.root / 'screenshots/views/Q1-baseline.html').read_text()
        document = AnswerHTML(html)
        self.assertEqual(document.text, text)
        self.assertFalse(any(tag in {'script', 'img', 'iframe', 'object'} for tag, _ in document.tags))
        self.assertIn('제출 원문 표시 화면', html)

    def test_registered_png_is_embedded_and_tracks_source_and_display(self):
        self.assertEqual(self.module.load_screenshots(self.root, load_run(self.root)), {})
        self.prepare_and_register()
        screenshots = self.module.load_screenshots(self.root, load_run(self.root))
        image = screenshots[('Q1', 'baseline')]
        self.assertEqual((image['width'], image['height']), (2, 2))
        self.assertTrue(image['data_uri'].startswith('data:image/png;base64,'))
        manifest = json.loads((self.root / 'screenshots/manifest.json').read_text())
        for key in ['sha256', 'answer_sha256', 'display_sha256', 'view_sha256']:
            self.assertEqual(len(manifest['captures'][0][key]), 64)

    def test_stale_redaction_rejected_and_new_view_applies_mask(self):
        self.prepare_and_register()
        result = json.loads((self.root / 'result.json').read_text())
        result['redactions'] = [{'arm': 'baseline', 'case_id': 'Q1', 'start': 0, 'end': 5, 'reason': '인증정보'}]
        write_json(self.root / 'result.json', result)
        with self.assertRaises(ValidationError):
            self.module.load_screenshots(self.root, load_run(self.root))
        self.module.prepare_views(self.root)
        html = (self.root / 'screenshots/views/Q1-baseline.html').read_text()
        self.assertTrue(AnswerHTML(html).text.startswith('[인증정보 가림]'))
        self.assertIn('표시 사본 · 일부 가림', html)

    def test_known_stale_image_cannot_be_reregistered_after_redaction(self):
        self.prepare_and_register()
        manifest = self.root / 'screenshots/manifest.json'
        before = manifest.read_bytes()
        result = json.loads((self.root / 'result.json').read_text())
        result['redactions'] = [{'arm': 'baseline', 'case_id': 'Q1', 'start': 0, 'end': 5, 'reason': '인증정보'}]
        write_json(self.root / 'result.json', result)
        self.module.prepare_views(self.root)
        with self.assertRaises(ValidationError):
            self.module.register_capture(self.root, 'Q1', 'baseline', 'screenshots/Q1-baseline.png', '2026-11-17T07:01:00Z')
        self.assertEqual(manifest.read_bytes(), before)
        with self.assertRaises(ValidationError):
            self.module.load_screenshots(self.root, load_run(self.root))

    def test_png_valid_crc_does_not_hide_invalid_pixels(self):
        self.module.prepare_views(self.root)
        path = self.root / 'screenshots/Q1-baseline.png'
        invalid = {
            'invalid-zlib': png_bytes(compressed=b'not-a-zlib-stream'),
            'too-short': png_bytes(pixels=b'\0' * 13),
            'too-long': png_bytes(pixels=b'\0' * 15),
            'invalid-filter': png_bytes(pixels=b'\x05' + b'\0' * 13),
            'trailing-zlib': png_bytes(compressed=zlib.compress(b'\0' * 14) + b'extra'),
            'oversized-decoded': png_bytes(width=100_000, height=100_000, pixels=b'\0'),
        }
        for reason, content in invalid.items():
            with self.subTest(reason=reason):
                path.write_bytes(content)
                with self.assertRaises(ValidationError):
                    self.module.register_capture(self.root, 'Q1', 'baseline', 'screenshots/Q1-baseline.png', '2026-11-17T07:00:00Z')

    def test_png_load_also_validates_pixels_and_rgba_is_supported(self):
        self.module.prepare_views(self.root)
        path = self.root / 'screenshots/Q1-baseline.png'
        path.write_bytes(png_bytes(color_type=6))
        self.module.register_capture(self.root, 'Q1', 'baseline', 'screenshots/Q1-baseline.png', '2026-11-17T07:00:00Z')
        self.assertIn(('Q1', 'baseline'), self.module.load_screenshots(self.root, load_run(self.root)))
        invalid = png_bytes(compressed=b'not-a-zlib-stream')
        path.write_bytes(invalid)
        manifest_path = self.root / 'screenshots/manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['captures'][0]['sha256'] = hashlib.sha256(invalid).hexdigest()
        write_json(manifest_path, manifest)
        with self.assertRaises(ValidationError):
            self.module.load_screenshots(self.root, load_run(self.root))

    def test_changed_png_and_view_rejected(self):
        self.prepare_and_register()
        image = self.root / 'screenshots/Q1-baseline.png'
        original = image.read_bytes()
        image.write_bytes(png_bytes(3, 3))
        with self.assertRaises(ValidationError):
            self.module.load_screenshots(self.root, load_run(self.root))
        image.write_bytes(original)
        view = self.root / 'screenshots/views/Q1-baseline.html'
        view.write_text('changed')
        with self.assertRaises(ValidationError):
            self.module.load_screenshots(self.root, load_run(self.root))
        with self.assertRaises(ValidationError):
            self.module.register_capture(self.root, 'Q1', 'baseline', 'screenshots/Q1-baseline.png', '2026-11-17T07:00:00Z')

    def test_unsafe_paths_symlinks_and_non_png_are_rejected(self):
        self.module.prepare_views(self.root)
        bad = self.root / 'screenshots/Q1-baseline.png'
        bad.write_text('<svg onload="evil()"/>')
        for path in ['screenshots/Q1-baseline.png', '../outside.png', 'source/SKILL.md']:
            with self.subTest(path=path), self.assertRaises(ValidationError):
                self.module.register_capture(self.root, 'Q1', 'baseline', path, '2026-11-17T07:00:00Z')
        bad.unlink()
        outside = self.root.parent / 'outside.png'
        outside.write_bytes(png_bytes())
        bad.symlink_to(outside)
        with self.assertRaises(ValidationError):
            self.module.register_capture(self.root, 'Q1', 'baseline', 'screenshots/Q1-baseline.png', '2026-11-17T07:00:00Z')

    def test_same_pair_reregistration_does_not_duplicate(self):
        self.prepare_and_register()
        self.module.register_capture(self.root, 'Q1', 'baseline', 'screenshots/Q1-baseline.png', '2026-11-17T07:01:00Z')
        manifest = json.loads((self.root / 'screenshots/manifest.json').read_text())
        self.assertEqual(len(manifest['captures']), 1)
        self.assertEqual(manifest['captures'][0]['captured_at'], '2026-11-17T07:01:00Z')

    def test_jpeg_is_retained_without_reencoding(self):
        self.module.prepare_views(self.root)
        path = self.root / 'screenshots/Q1-baseline.jpg'
        original = jpeg_bytes()
        path.write_bytes(original)
        self.module.register_capture(self.root, 'Q1', 'baseline', 'screenshots/Q1-baseline.jpg', '2026-11-17T07:00:00Z')
        capture = self.module.load_screenshots(self.root, load_run(self.root))[('Q1', 'baseline')]
        self.assertEqual(capture['data_uri'], 'data:image/jpeg;base64,' + base64.b64encode(original).decode('ascii'))
        self.assertEqual((capture['width'], capture['height']), (1, 1))
        path.write_bytes(original[:-2])
        with self.assertRaises(ValidationError):
            self.module.load_screenshots(self.root, load_run(self.root))

    def test_jpeg_header_without_decodable_entropy_is_rejected(self):
        self.module.prepare_views(self.root)
        path = self.root / 'screenshots/Q1-baseline.jpg'
        original = jpeg_bytes()
        marker = original.index(b'\xff\xda')
        scan_start = marker + 2 + struct.unpack('>H', original[marker + 2:marker + 4])[0]
        path.write_bytes(original[:scan_start] + b'\xff\xd8' + b'\xff\xd9')
        with self.assertRaises(ValidationError):
            self.module.register_capture(self.root, 'Q1', 'baseline', 'screenshots/Q1-baseline.jpg', '2026-11-17T07:00:00Z')

    def test_pillow_missing_only_blocks_image_validation_with_clear_message(self):
        real_import = __import__

        def without_pillow(name, *args, **kwargs):
            if name == 'PIL' or name.startswith('PIL.'):
                raise ImportError('test missing Pillow')
            return real_import(name, *args, **kwargs)

        self.module.prepare_views(self.root)
        path = self.root / 'screenshots/Q1-baseline.png'
        path.write_bytes(png_bytes())
        with patch('builtins.__import__', side_effect=without_pillow):
            self.assertEqual(self.module.load_screenshots(self.root, load_run(self.root)), {})
            with self.assertRaisesRegex(ValidationError, 'pip install Pillow'):
                self.module.register_capture(self.root, 'Q1', 'baseline', 'screenshots/Q1-baseline.png', '2026-11-17T07:00:00Z')


if __name__ == '__main__':
    unittest.main()
