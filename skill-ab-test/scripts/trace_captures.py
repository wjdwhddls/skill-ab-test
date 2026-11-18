#!/usr/bin/env python3
"""Register browser screenshots of the actual JSONL trace view.

Hashes bind saved images to source files and the preserved HTML snapshot. They
do not establish how a screenshot was acquired; the main agent records that UI
evidence. This helper neither interprets responses nor creates screenshots.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from report_lib import (
    ValidationError, digest, local_path, need, obj, read_json, safe_url, seq,
    string, timestamp,
)

MANIFEST = 'trace-captures.json'
BASIS = 'actual_jsonl_view'
CAPTION = '실행 로그 화면 · 촬영 당시 HTML 보존'


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _image_path(root, relative):
    string(relative, 'capture.path')
    rel = Path(relative)
    need(len(rel.parts) >= 2 and rel.parts[0] == 'screenshots'
         and rel.suffix.lower() in {'.png', '.jpg', '.jpeg'},
         '캡처 이미지는 screenshots/ 아래 PNG 또는 JPEG여야 합니다.')
    path = local_path(root, relative)
    need(relative == rel.as_posix(), '캡처 이미지 경로는 정규 상대 경로여야 합니다.')
    return path


def _view_name(relative):
    return f'screenshots/{Path(relative).stem}-view.html'


def _decode(path):
    # The decoder imports Pillow only when an image is present.
    from capture_responses import _image
    try:
        return _image(path)
    except OSError as exc:
        raise ValidationError(f'캡처 이미지를 읽을 수 없습니다: {path.name}') from exc


def _source_inputs(root):
    record = read_json(local_path(root, 'trace-run.json'))
    arms = obj(record.get('arms'), 'trace-run.arms')
    paths = ['trace-plan.json', 'trace-run.json', 'trace-review.json']
    for arm in ('baseline', 'treatment'):
        files = obj(obj(arms.get(arm), f'arms.{arm}').get('files'), f'{arm}.files')
        for kind in ('events', 'answer'):
            relative = string(files.get(kind), f'{arm}.files.{kind}')
            local_path(root, relative)
            paths.append(relative)
    need(len(set(paths)) == len(paths), '실행 입력 파일 경로가 중복되었습니다.')
    return {relative: digest(local_path(root, relative)) for relative in paths}


def _metadata(item):
    obj(item, 'capture')
    title = string(item.get('title'), 'capture.title')
    need(len(title) <= 80 and not any(ord(char) < 32 for char in title),
         '캡처 제목은 제어 문자가 없는 80자 이하의 텍스트여야 합니다.')
    try:
        title.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ValidationError('캡처 제목의 유니코드가 유효하지 않습니다.') from exc
    timestamp(item.get('captured_at'), 'capture.captured_at')
    safe_url(item.get('page_url'), 'capture.page_url')
    need(item.get('basis') == BASIS, '지원하지 않는 캡처 근거입니다.')


def _manifest(root):
    path = local_path(root, MANIFEST)
    if not path.exists():
        return {'schema_version': 1, 'basis': BASIS, 'captures': []}
    manifest = read_json(path)
    need(type(manifest.get('schema_version')) is int and manifest['schema_version'] == 1
         and manifest.get('basis') == BASIS, '지원하지 않는 로그 캡처 기록 형식입니다.')
    seq(manifest.get('captures'), 'captures')
    return manifest


def _load(root, manifest):
    captures = manifest['captures']
    if not captures:
        return []
    sources = _source_inputs(root)
    images, seen_images, seen_views = [], set(), set()
    for item in captures:
        _metadata(item)
        image = _image_path(root, item.get('path'))
        view = item.get('view')
        need(view == _view_name(item['path']), '캡처 HTML 보존 경로가 일치하지 않습니다.')
        need(item['path'] not in seen_images and view not in seen_views,
             '이미지 또는 HTML 보존 경로가 중복되었습니다.')
        seen_images.add(item['path'])
        seen_views.add(view)
        need(obj(item.get('source_inputs'), 'capture.source_inputs') == sources,
             '촬영 이후 실행 입력 또는 로그가 변경되었습니다. 기존 캡처를 갱신할 수 없습니다.')
        need(digest(local_path(root, view)) == item.get('view_sha256'),
             '촬영 당시 HTML 보존본이 변경되었습니다.')
        raw, width, height, mime = _decode(image)
        need(item.get('sha256') == _sha(raw), '등록된 캡처 이미지가 변경되었습니다.')
        need(type(item.get('width')) is int and type(item.get('height')) is int
             and (item['width'], item['height']) == (width, height)
             and item.get('mime') == mime, '캡처 이미지 크기 또는 형식이 기록과 다릅니다.')
        images.append({'title': item['title'], 'data_uri': f'data:{mime};base64,'
                       + base64.b64encode(raw).decode('ascii'), 'width': width,
                       'height': height, 'caption': CAPTION})
    return images


def load_trace_captures(root):
    """Validate immutable capture evidence and return offline image cards."""
    root = Path(root).resolve()
    return _load(root, _manifest(root))


def _atomic_json(path, value):
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
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


def register_capture(run_dir, image, title, captured_at, page_url):
    """Append a capture; never replace an existing image record or snapshot."""
    root = Path(run_dir).resolve()
    manifest = _manifest(root)
    _load(root, manifest)  # A stale prior record cannot be silently refreshed.
    image_path = _image_path(root, image)
    view = _view_name(image)
    view_path = local_path(root, view)
    need(not any(item['path'] == image or item['view'] == view for item in manifest['captures']),
         '이 이미지 또는 HTML 보존 경로는 이미 등록되었습니다.')
    need(not view_path.exists(), '기존 HTML 보존본을 덮어쓸 수 없습니다.')
    record = {'path': image, 'view': view, 'title': title, 'captured_at': captured_at,
              'page_url': page_url, 'basis': BASIS}
    _metadata(record)
    sources = _source_inputs(root)
    raw, width, height, mime = _decode(image_path)
    report = local_path(root, 'trace-report.html')
    try:
        html = report.read_bytes()
    except OSError as exc:
        raise ValidationError('촬영한 trace-report.html을 읽을 수 없습니다.') from exc
    record.update(sha256=_sha(raw), width=width, height=height, mime=mime,
                  view_sha256=_sha(html), source_inputs=sources)
    # Validate serialization before creating the preserved HTML.
    try:
        json.dumps(record, ensure_ascii=False).encode('utf-8')
    except (UnicodeError, ValueError) as exc:
        raise ValidationError('캡처 메타데이터를 UTF-8로 저장할 수 없습니다.') from exc
    created_view = False
    try:
        with view_path.open('xb') as stream:
            created_view = True
            stream.write(html)
            stream.flush()
            os.fsync(stream.fileno())
        manifest['captures'].append(record)
        _atomic_json(local_path(root, MANIFEST), manifest)
    except OSError as exc:
        if created_view:
            view_path.unlink(missing_ok=True)
        raise ValidationError('로그 캡처 기록을 저장하지 못했습니다. 기존 기록은 유지됩니다.') from exc
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--image', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--captured-at', required=True)
    parser.add_argument('--page-url', required=True)
    args = parser.parse_args()
    try:
        record = register_capture(args.run_dir, args.image, args.title, args.captured_at, args.page_url)
    except (ValidationError, OSError) as exc:
        print(f'오류: {exc}', file=sys.stderr)
        return 2
    print(json.dumps({'registered': record['path'], 'view': record['view']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
