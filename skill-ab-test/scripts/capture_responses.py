#!/usr/bin/env python3
"""Prepare response views and register real browser screenshots.

This helper never takes or fabricates screenshots. The main agent captures the
prepared views with its browser tool and registers the saved PNG/JPEGs. Hashes detect
changed inputs and files; they do not prove how an image was acquired.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
from html import escape
from io import BytesIO
import json
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import warnings
import zlib

from report_lib import (
    ARMS, CASE_IDS, ValidationError, choice, digest, load_run, local_path,
    need, obj, read_json, seq, timestamp,
)

TEMPLATE = Path(__file__).resolve().parents[1] / 'assets/response-template.html'
MANIFEST = 'screenshots/manifest.json'
BASIS = 'submitted_response_render'
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
ARM_LABELS = {
    'baseline': ('A', '대상 지침 추가 제공 없음'),
    'treatment': ('B', '대상 지침 명시 적용'),
}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _escape(text):
    return escape(text, quote=True).replace('\r', '&#13;')


def _display(run, case_id, arm):
    text = run['answers'][arm].get(case_id)
    need(text is not None, '수집된 답변이 없는 문항은 캡처할 수 없습니다.')
    redactions = sorted((item for item in run['result']['redactions']
                         if item['arm'] == arm and item['case_id'] == case_id),
                        key=lambda item: item['start'])
    pieces, cursor = [], 0
    for item in redactions:
        pieces.extend((text[cursor:item['start']], '[인증정보 가림]'))
        cursor = item['end']
    pieces.append(text[cursor:])
    return ''.join(pieces), bool(redactions)


def _view_path(case_id, arm):
    choice(case_id, set(CASE_IDS), 'capture.case_id')
    choice(arm, set(ARMS), 'capture.arm')
    return f'screenshots/views/{case_id}-{arm}.html'


def _view(run, case_id, arm):
    displayed, masked = _display(run, case_id, arm)
    letter, label = ARM_LABELS[arm]
    fields = {
        'TITLE': _escape(f'{case_id} · {letter} 제출 응답'),
        'HEADING': _escape(f'{case_id} · {letter} 응답'),
        'LABEL': _escape(label),
        'ANSWER': _escape(displayed),
        'NOTICE': '표시 사본 · 일부 가림' if masked else '정식 답변 그대로',
    }
    template = TEMPLATE.read_text(encoding='utf-8')
    html = re.sub(r'\{\{(TITLE|HEADING|LABEL|ANSWER|NOTICE)\}\}',
                  lambda match: fields[match[1]], template)
    return html.encode('utf-8'), displayed, masked


def _atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def prepare_views(run_dir):
    """Create only fixed derived paths; no source or submitted answer is changed."""
    root = Path(run_dir).resolve()
    run = load_run(root)
    captures = []
    prepared = []
    for case_id in CASE_IDS:
        for arm in ARMS:
            if case_id not in run['answers'][arm]:
                continue
            view = _view_path(case_id, arm)
            path = local_path(root, view)
            content, displayed, _ = _view(run, case_id, arm)
            prepared.append((path, content))
            captures.append({
                'case_id': case_id, 'arm': arm, 'view': view,
                'image': f'screenshots/{case_id}-{arm}.jpg',
                'view_sha256': _sha(content),
                'display_sha256': _sha(displayed.encode('utf-8')),
                'answer_sha256': run['answer_files'][arm]['sha256'],
            })
    plan_path = local_path(root, 'screenshots/capture-plan.json')
    plan = {'schema_version': 1, 'basis': BASIS, 'viewport_width': 640,
            'capture_instruction': '각 view를 같은 640px 폭에서 열고 전체 페이지를 실제 브라우저로 촬영합니다. 잘림 없이 원문 전체를 확인하고 원본 PNG 또는 JPEG를 등록합니다.',
            'captures': captures}
    # All paths are validated before writing any derived output.
    for path, content in prepared:
        _atomic(path, content)
    _atomic(plan_path, _json_bytes(plan))
    return plan


def _image_path(root, relative):
    need(isinstance(relative, str), 'capture image path must be text')
    rel = Path(relative)
    need(len(rel.parts) >= 2 and rel.parts[0] == 'screenshots' and rel.suffix.lower() in {'.png', '.jpg', '.jpeg'},
         '캡처 이미지는 screenshots/ 아래 PNG 또는 JPEG여야 합니다.')
    return local_path(root, relative)


def _png(raw):
    need(raw.startswith(b'\x89PNG\r\n\x1a\n'), '캡처 파일은 실제 PNG 형식이어야 합니다.')
    offset, width, height, saw_data = 8, None, None, False
    image_data, pixel_format = [], None
    while offset < len(raw):
        need(offset + 12 <= len(raw), '잘린 PNG 청크입니다.')
        length = struct.unpack('>I', raw[offset:offset + 4])[0]
        kind = raw[offset + 4:offset + 8]
        stop = offset + 8 + length
        need(stop + 4 <= len(raw), '잘린 PNG 데이터입니다.')
        data = raw[offset + 8:stop]
        crc = struct.unpack('>I', raw[stop:stop + 4])[0]
        need((zlib.crc32(kind + data) & 0xffffffff) == crc, 'PNG 체크섬이 일치하지 않습니다.')
        if offset == 8:
            need(kind == b'IHDR' and length == 13, 'PNG IHDR가 유효하지 않습니다.')
            width, height = struct.unpack('>II', data[:8])
            need(width > 0 and height > 0, 'PNG 크기가 유효하지 않습니다.')
            need(width * height <= MAX_IMAGE_PIXELS, '캡처 이미지는 2,500만 픽셀 이하여야 합니다.')
            pixel_format = tuple(data[8:])
        elif kind == b'IHDR':
            raise ValidationError('중복 PNG IHDR입니다.')
        if kind == b'IDAT':
            saw_data = True
            image_data.append(data)
        offset = stop + 4
        if kind == b'IEND':
            need(length == 0 and offset == len(raw) and saw_data, 'PNG 종료 청크가 유효하지 않습니다.')
            # Pillow decodes pixels below. For the common 8-bit noninterlaced
            # browser RGB/RGBA formats, also reject ignored trailing pixels or
            # bytes by checking the complete bounded zlib stream and row sizes.
            if pixel_format in {(8, 2, 0, 0, 0), (8, 6, 0, 0, 0)}:
                channels = 3 if pixel_format[1] == 2 else 4
                row_bytes = width * channels + 1
                expected = row_bytes * height
                need(expected <= 128 * 1024 * 1024, 'PNG 해제 데이터는 128 MiB 이하여야 합니다.')
                try:
                    decoder = zlib.decompressobj()
                    pixels = decoder.decompress(b''.join(image_data), expected + 1)
                except zlib.error as exc:
                    raise ValidationError('PNG 압축 픽셀 데이터가 손상되었습니다.') from exc
                need(len(pixels) == expected and decoder.eof and not decoder.unused_data
                     and not decoder.unconsumed_tail, 'PNG 스캔라인 길이 또는 압축 스트림이 유효하지 않습니다.')
                need(all(pixels[index] <= 4 for index in range(0, expected, row_bytes)),
                     'PNG 스캔라인 필터가 유효하지 않습니다.')
            return raw, width, height
    raise ValidationError('PNG 종료 청크가 없습니다.')


def _jpeg(raw):
    need(raw.startswith(b'\xff\xd8') and raw.endswith(b'\xff\xd9'), 'JPEG 시작 또는 종료 마커가 유효하지 않습니다.')
    offset, width, height = 2, None, None
    while offset < len(raw) - 2:
        need(raw[offset] == 0xff, 'JPEG 세그먼트 마커가 유효하지 않습니다.')
        while offset < len(raw) and raw[offset] == 0xff:
            offset += 1
        need(offset < len(raw), '잘린 JPEG 마커입니다.')
        kind = raw[offset]
        offset += 1
        need(kind not in {0, 0xd8, 0xd9}, '예상하지 못한 JPEG 마커입니다.')
        if kind == 0x01 or 0xd0 <= kind <= 0xd7:
            continue
        need(offset + 2 <= len(raw), '잘린 JPEG 세그먼트입니다.')
        length = struct.unpack('>H', raw[offset:offset + 2])[0]
        need(length >= 2 and offset + length <= len(raw) - 2, 'JPEG 세그먼트 길이가 유효하지 않습니다.')
        data = raw[offset + 2:offset + length]
        if kind in {0xc0, 0xc1, 0xc2}:
            need(width is None and len(data) >= 6, 'JPEG 프레임 헤더가 유효하지 않습니다.')
            height, width = struct.unpack('>HH', data[1:5])
            components = data[5]
            need(width > 0 and height > 0 and components > 0 and len(data) == 6 + components * 3,
                 'JPEG 프레임 크기가 유효하지 않습니다.')
        if kind == 0xda:
            need(width is not None and len(data) >= 4 and offset + length < len(raw) - 2,
                 'JPEG 스캔에 프레임 또는 영상 데이터가 없습니다.')
            return raw, width, height
        offset += length
    raise ValidationError('JPEG 스캔 데이터를 찾지 못했습니다.')


def _image(path):
    need(path.stat().st_size <= MAX_IMAGE_BYTES, '캡처 이미지 파일은 32 MiB 이하여야 합니다.')
    raw = path.read_bytes()
    if path.suffix.lower() == '.png':
        data, width, height = _png(raw)
        expected_format, mime = 'PNG', 'image/png'
    else:
        data, width, height = _jpeg(raw)
        expected_format, mime = 'JPEG', 'image/jpeg'
    need(width * height <= MAX_IMAGE_PIXELS, '캡처 이미지는 2,500만 픽셀 이하여야 합니다.')
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise ValidationError('캡처 이미지의 실제 디코드 검증에는 Pillow가 필요합니다. python3 -m pip install Pillow 로 설치하세요.') from exc
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                need(image.format == expected_format and image.size == (width, height), '이미지 디코드 정보가 캡처 헤더와 다릅니다.')
                image.verify()
            with Image.open(BytesIO(raw)) as decoded:
                decoded.load()
                need(decoded.format == expected_format and decoded.size == (width, height), '디코드된 이미지 크기 또는 형식이 다릅니다.')
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValidationError('캡처 이미지의 픽셀 데이터를 디코드할 수 없습니다. 실제 브라우저에서 다시 촬영하세요.') from exc
    return data, width, height, mime


def _manifest(root):
    path = local_path(root, MANIFEST)
    if not path.exists():
        return {'schema_version': 1, 'basis': BASIS, 'captures': []}
    manifest = read_json(path)
    need(manifest.get('schema_version') == 1 and manifest.get('basis') == BASIS, '지원하지 않는 캡처 기록 형식입니다.')
    seen = set()
    for item in seq(manifest.get('captures'), 'captures'):
        obj(item, 'capture')
        _view_path(item.get('case_id'), item.get('arm'))
        pair = item['case_id'], item['arm']
        need(pair not in seen, '같은 응답의 캡처 기록이 중복되었습니다.')
        seen.add(pair)
    return manifest


def register_capture(run_dir, case_id, arm, image, captured_at):
    """Record a browser-created image. Acquisition is a main-agent record."""
    root = Path(run_dir).resolve()
    run = load_run(root)
    view = _view_path(case_id, arm)
    timestamp(captured_at, 'capture.captured_at')
    content, displayed, masked = _view(run, case_id, arm)
    view_file = local_path(root, view)
    need(view_file.is_file() and view_file.read_bytes() == content,
         '캡처 화면이 현재 답변·가림 기준과 다릅니다. 화면을 준비하고 다시 촬영하세요.')
    image_file = _image_path(root, image)
    raw, width, height, mime = _image(image_file)
    record = {
        'case_id': case_id, 'arm': arm, 'path': image, 'view': view,
        'sha256': _sha(raw), 'answer_sha256': run['answer_files'][arm]['sha256'],
        'display_sha256': _sha(displayed.encode('utf-8')), 'view_sha256': _sha(content),
        'captured_at': captured_at, 'width': width, 'height': height,
        'capture_method': 'browser', 'basis': BASIS, 'redacted': masked, 'mime': mime,
    }
    manifest = _manifest(root)
    for previous in manifest['captures']:
        if previous.get('sha256') == record['sha256']:
            need(all(previous.get(key) == record[key] for key in
                     ('answer_sha256', 'display_sha256', 'view_sha256', 'redacted')),
                 '이미 알려진 이전 캡처를 변경된 답변·가림 화면에 재등록할 수 없습니다. 새 화면을 촬영하세요.')
    manifest['captures'] = [item for item in manifest['captures']
                            if (item['case_id'], item['arm']) != (case_id, arm)] + [record]
    manifest['captures'].sort(key=lambda item: (item['case_id'], item['arm']))
    _atomic(local_path(root, MANIFEST), _json_bytes(manifest))
    return record


def load_screenshots(run_dir, run):
    """Return validated embedded PNG/JPEGs; absent captures remain absent."""
    root = Path(run_dir).resolve()
    manifest = _manifest(root)
    loaded = {}
    for item in manifest['captures']:
        case_id, arm = item['case_id'], item['arm']
        view = _view_path(case_id, arm)
        need(item.get('view') == view, '캡처 화면 경로가 해당 응답과 일치하지 않습니다.')
        need(item.get('basis') == BASIS and item.get('capture_method') == 'browser', '캡처 출처 기록이 유효하지 않습니다.')
        timestamp(item.get('captured_at'), 'capture.captured_at')
        content, displayed, masked = _view(run, case_id, arm)
        need(item.get('answer_sha256') == run['answer_files'][arm]['sha256'], '캡처 후 제출 답변 파일이 변경되었습니다.')
        need(item.get('display_sha256') == _sha(displayed.encode('utf-8')) and item.get('redacted') is masked,
             '캡처 후 표시할 답변 또는 가림 기준이 변경되었습니다.')
        view_file = local_path(root, view)
        need(item.get('view_sha256') == _sha(content) and view_file.is_file()
             and digest(view_file) == item['view_sha256'], '캡처 화면이 변경되었습니다. 다시 촬영하세요.')
        image_file = _image_path(root, item.get('path'))
        raw, width, height, mime = _image(image_file)
        need(item.get('sha256') == _sha(raw), '캡처 이미지 파일이 변경되었습니다.')
        need(item.get('mime') == mime, '캡처 이미지 형식이 기록과 다릅니다.')
        need(type(item.get('width')) is int and type(item.get('height')) is int
             and (item['width'], item['height']) == (width, height), '캡처 이미지 크기가 기록과 다릅니다.')
        letter = ARM_LABELS[arm][0]
        loaded[(case_id, arm)] = {
            'data_uri': f'data:{mime};base64,' + base64.b64encode(raw).decode('ascii'),
            'caption': '제출 원문을 브라우저에 표시해 촬영한 화면' + (' · 일부 가림' if masked else ''),
            'alt': f'{case_id} · {letter} 실제 제출 응답을 표시한 화면. 같은 답변의 텍스트 원문을 아래에서 확인할 수 있습니다.',
            'width': width, 'height': height,
        }
    return loaded


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--register', nargs=2, metavar=('CASE_ID', 'ARM'))
    parser.add_argument('--image', help='실행 폴더 기준 screenshots/ 아래 PNG 또는 JPEG 경로')
    parser.add_argument('--captured-at', help='실제 촬영 시각 ISO8601, 시간대 포함')
    args = parser.parse_args(argv)
    try:
        if args.register:
            need(args.image is not None and args.captured_at is not None, '등록에는 --image와 --captured-at이 필요합니다.')
            record = register_capture(args.run_dir, *args.register, args.image, args.captured_at)
            print(json.dumps({'registered': record['path'], 'case_id': record['case_id'], 'arm': record['arm']}, ensure_ascii=False))
        else:
            need(args.image is None and args.captured_at is None, '--image와 --captured-at은 --register와 함께 사용합니다.')
            plan = prepare_views(args.run_dir)
            print(json.dumps({'prepared': len(plan['captures']), 'plan': str(args.run_dir / 'screenshots/capture-plan.json'), 'viewport_width': 640}, ensure_ascii=False))
    except (ValidationError, OSError) as exc:
        print(f'응답 캡처 처리 실패: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
