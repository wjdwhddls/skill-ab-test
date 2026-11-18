#!/usr/bin/env python3
"""Bind native terminal/agent-UI screenshots to preserved execution logs.

This does not create screenshots or prove how they were acquired. The main agent
must observe the native UI. Report HTML and analysis text are not capture sources.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from report_lib import ValidationError, digest, local_path, need, obj, read_json, read_text, safe_url, seq, string, timestamp

MANIFEST = 'native-captures.json'
KIND = 'native_log_captures'
CAPTIONS = {
    'saved_log_terminal': '원본 로그를 터미널에서 연 화면 · 실행 종료 후 촬영',
    'live_runner_terminal': '실행 중인 러너의 원본 출력 화면 · 실시간 촬영',
    'native_agent_ui': '에이전트의 실제 실행 기록 UI 화면',
    'raw_log_browser': '원본 JSONL 파일 직접 열람 · 실행 종료 후 촬영 · Codex 앱 화면 아님',
}
SOURCE_KINDS = {'events', 'stderr'}


def _text(value, label, maximum):
    string(value, label)
    need(len(value) <= maximum and not any(ord(c) < 32 for c in value), f'{label}: invalid length or control character')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValidationError(f'{label}: invalid Unicode') from exc


def _image_path(root, relative):
    string(relative, 'capture.path')
    rel = Path(relative)
    need(len(rel.parts) >= 2 and rel.parts[0] == 'screenshots' and rel.suffix.lower() in {'.png', '.jpg', '.jpeg'},
         'native screenshots must be PNG/JPEG files under screenshots/')
    need(relative == rel.as_posix(), 'capture path must be normalized')
    return local_path(root, relative)


def _decode(path):
    from capture_responses import _image
    try:
        return _image(path)
    except OSError as exc:
        raise ValidationError(f'cannot read native screenshot: {path.name}') from exc


def _sources(root, arm, source_file):
    need(arm in {'baseline', 'treatment'}, 'capture.arm must be baseline or treatment')
    local_path(root, source_file)
    record = read_json(local_path(root, 'trace-run.json'))
    arms = obj(record.get('arms'), 'trace-run.arms')
    selected = obj(obj(arms.get(arm), f'arms.{arm}').get('files'), f'{arm}.files')
    matches = [kind for kind in SOURCE_KINDS if selected.get(kind) == source_file]
    need(len(matches) == 1, 'source_file must be the selected arm\'s declared raw events or stderr')
    paths = {'trace-plan.json', 'trace-run.json', source_file}
    for name in ('baseline', 'treatment'):
        state = obj(arms.get(name), f'arms.{name}')
        files, hashes = obj(state.get('files'), f'{name}.files'), obj(state.get('sha256'), f'{name}.sha256')
        required = {'events', 'answer'} | ({matches[0]} if name == arm else set())
        for key in required:
            relative = string(files.get(key), f'{name}.{key}')
            need(digest(local_path(root, relative)) == hashes.get(key), f'{name}.{key}: original log hash mismatch')
            paths.add(relative)
    return matches[0], {path: digest(local_path(root, path)) for path in sorted(paths)}


def _lines(root, source_file, kind, visible_lines):
    obj(visible_lines, 'visible_lines')
    start, end = visible_lines.get('start'), visible_lines.get('end')
    need(type(start) is int and type(end) is int and 1 <= start <= end, 'visible_lines must be a positive inclusive range')
    # JSONL uses physical LF-delimited records. A U+2028 inside a JSON string
    # must not shift event line numbers.
    lines = read_text(local_path(root, source_file)).split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    need(end <= len(lines), 'visible_lines exceeds the source file')
    if kind == 'events':
        for line in lines[start - 1:end]:
            try:
                event = obj(json.loads(line), 'selected event')
            except ValueError as exc:
                raise ValidationError('selected events must be valid JSONL') from exc
            item = event.get('item', {})
            item_type = item.get('type', '') if isinstance(item, dict) else ''
            need('reasoning' not in str(event.get('type', '')).lower() and 'reasoning' not in str(item_type).lower(),
                 'reasoning events cannot be selected as observable screenshot evidence')


def _metadata(root, item):
    obj(item, 'capture')
    _text(item.get('title'), 'capture.title', 80)
    _text(item.get('surface'), 'capture.surface', 160)
    _text(item.get('display_command'), 'capture.display_command', 4000)
    timestamp(item.get('captured_at'), 'capture.captured_at')
    need(item.get('basis') in CAPTIONS, 'capture basis must describe a native terminal or agent UI')
    if item.get('page_url') is not None:
        safe_url(item['page_url'], 'capture.page_url')
    if item['basis'] == 'raw_log_browser':
        safe_url(item.get('page_url'), 'capture.page_url')
    kind, sources = _sources(root, item.get('arm'), item.get('source_file'))
    need(item['basis'] != 'raw_log_browser' or kind == 'events', 'raw_log_browser requires the original JSONL event file')
    _lines(root, item['source_file'], kind, item.get('visible_lines'))
    return kind, sources


def _manifest(root):
    path = local_path(root, MANIFEST)
    if not path.exists():
        return {'schema_version': 1, 'kind': KIND, 'captures': []}
    value = read_json(path)
    need(type(value.get('schema_version')) is int and value['schema_version'] == 1 and value.get('kind') == KIND,
         'unsupported native capture manifest')
    seq(value.get('captures'), 'captures')
    return value


def _load(root, manifest):
    images, seen = [], set()
    for item in manifest['captures']:
        kind, sources = _metadata(root, item)
        path = _image_path(root, item.get('path'))
        need(item['path'] not in seen, 'duplicate native screenshot')
        seen.add(item['path'])
        need(item.get('source_kind') == kind and obj(item.get('source_inputs'), 'source_inputs') == sources,
             'capture source files changed; existing captures cannot be refreshed')
        raw, width, height, mime = _decode(path)
        need(hashlib.sha256(raw).hexdigest() == item.get('sha256'), 'native screenshot changed after registration')
        need(type(item.get('width')) is int and type(item.get('height')) is int and
             (item['width'], item['height'], item.get('mime')) == (width, height, mime), 'native screenshot metadata mismatch')
        images.append({key: item[key] for key in ('title', 'basis', 'arm', 'source_file', 'visible_lines', 'surface', 'captured_at')} |
                      {'data_uri': f'data:{mime};base64,' + base64.b64encode(raw).decode('ascii'),
                       'width': width, 'height': height, 'caption': CAPTIONS[item['basis']]})
    return images


def load_native_captures(root):
    """Return validated native screenshots; absent captures need no image decoder."""
    root = Path(root).resolve()
    return _load(root, _manifest(root))


def _atomic_json(path, value):
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def register_capture(run_dir, image, title, captured_at, surface, basis, arm, source_file, display_command, visible_lines, page_url=None):
    """Append an observed screenshot without changing source logs or old records."""
    root = Path(run_dir).resolve()
    manifest = _manifest(root)
    _load(root, manifest)
    need(not any(item['path'] == image for item in manifest['captures']), 'native screenshot is already registered')
    item = {'path': image, 'title': title, 'captured_at': captured_at, 'surface': surface, 'basis': basis,
            'arm': arm, 'source_file': source_file, 'display_command': display_command, 'visible_lines': visible_lines, 'page_url': page_url}
    kind, sources = _metadata(root, item)
    raw, width, height, mime = _decode(_image_path(root, image))
    item.update(source_kind=kind, source_inputs=sources, sha256=hashlib.sha256(raw).hexdigest(), width=width, height=height, mime=mime)
    manifest['captures'].append(item)
    try:
        _atomic_json(local_path(root, MANIFEST), manifest)
    except OSError as exc:
        raise ValidationError('could not save native capture metadata; existing records remain unchanged') from exc
    return item


def _range(value):
    match = re.fullmatch(r'([1-9][0-9]*):([1-9][0-9]*)', value)
    if not match:
        raise argparse.ArgumentTypeError('use an inclusive start:end line range, for example 1:5')
    start, end = map(int, match.groups())
    if end < start:
        raise argparse.ArgumentTypeError('end must be at least start')
    return {'start': start, 'end': end}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    for key in ('image', 'title', 'captured-at', 'surface', 'arm', 'source-file', 'display-command'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--basis', required=True, choices=sorted(CAPTIONS))
    parser.add_argument('--visible-lines', type=_range, required=True)
    parser.add_argument('--page-url')
    args = parser.parse_args()
    try:
        record = register_capture(args.run_dir, args.image, args.title, args.captured_at, args.surface, args.basis,
                                  args.arm, args.source_file, args.display_command, args.visible_lines, args.page_url)
    except (ValidationError, OSError) as exc:
        print(f'오류: {exc}', file=sys.stderr)
        return 2
    print(json.dumps({'registered': record['path'], 'basis': record['basis']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
