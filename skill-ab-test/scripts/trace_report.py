#!/usr/bin/env python3
"""Render recorded Codex events and main-authored judgments, without grading."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import tomllib

from report_lib import (ValidationError, digest, local_path, need, obj, read_json,
                        read_text, safe_url, string, timestamp)
from skill_analysis import load_analysis
from analysis_views import files_panel, flow_panel, tools_panel
from comparison_views import comparison_panel

ARMS = ('baseline', 'treatment')
LETTERS = {'baseline': 'A', 'treatment': 'B'}
VERDICTS = {'improved': '개선', 'worsened': '악화', 'unchanged': '차이 없음', 'undetermined': '판단 불가'}
FILE_KEYS = {'events', 'stderr', 'answer', 'receipts', 'inventory', 'prompt'}
TEMPLATE = Path(__file__).resolve().parents[1] / 'assets/trace-template.html'
SCRIPT = read_text(TEMPLATE.with_name('trace-dashboard.js'))


def _e(value):
    return escape(str(value), quote=True).replace('\r', '&#13;')


def _pairs(pairs):
    data = {}
    for key, value in pairs:
        need(key not in data, f'duplicate JSON key: {key}')
        data[key] = value
    return data


def read_jsonl(path):
    """Keep original line numbers and literal line text for evidence anchors."""
    rows = []
    for number, raw in enumerate(read_text(path).splitlines(), 1):
        if not raw.strip():
            continue
        try:
            event = obj(json.loads(raw, object_pairs_hook=_pairs), f'{path.name}:{number}')
            json.dumps(event, ensure_ascii=False).encode('utf-8')
        except (ValueError, UnicodeError) as exc:
            raise ValidationError(f'invalid JSONL at {path.name}:{number}') from exc
        rows.append({'line': number, 'event': event, 'raw': raw})
    return rows


def is_reasoning(event):
    item = event.get('item')
    item_type = item.get('type', '') if isinstance(item, dict) else ''
    return 'reasoning' in str(event.get('type', '')).lower() or 'reasoning' in str(item_type).lower()


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def observed_fields(event):
    """Only emitted, publicly observable fields are eligible for quotations."""
    if is_reasoning(event):
        return {}
    item = event.get('item')
    if not isinstance(item, dict):
        return {key: event[key] for key in ('type', 'thread_id', 'usage', 'message', 'error') if key in event}
    kind = item.get('type')
    keys = {
        'command_execution': ('command', 'aggregated_output', 'exit_code', 'status'),
        'file_change': ('changes', 'status'),
        'mcp_tool_call': ('server', 'tool', 'arguments', 'result', 'error', 'status'),
        'web_search': ('query', 'action'),
        'agent_message': ('text',),
        'todo_list': ('items',),
        'plan_update': ('plan', 'explanation'),
        'error': ('message',),
    }.get(kind, ())
    return {key: item[key] for key in keys if key in item}


def event_quote_text(event):
    values = observed_fields(event)
    return '\n'.join(value if isinstance(value, str) else _json(value) for value in values.values())


def _result_texts(value):
    """Extract literal text values; never infer behavior from command names."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for part in value for text in _result_texts(part)]
    if isinstance(value, dict):
        return [text for part in value.values() for text in _result_texts(part)]
    return []


def body_read_lines(rows, source_text):
    """Find full pinned body in successful emitted tool output, not in prompts."""
    needle = source_text.rstrip()
    need(bool(needle), 'empty pinned source body')
    found = []
    for row in rows:
        event = row['event']
        if event.get('type') != 'item.completed':
            continue
        item = event.get('item', {})
        outputs = []
        if item.get('type') == 'command_execution' and item.get('exit_code') == 0 and item.get('status') == 'completed':
            outputs = [item.get('aggregated_output', '')]
        elif item.get('type') == 'mcp_tool_call' and item.get('status') == 'completed' and not item.get('error'):
            result = item.get('result')
            if not (isinstance(result, dict) and result.get('isError')):
                outputs = _result_texts(result)
        if any(isinstance(text, str) and needle in text for text in outputs):
            found.append(row['line'])
    return found


def body_exposure_lines(rows, source_text):
    """Exposure can occur before a tool fails or finishes; success is irrelevant."""
    needle = source_text.rstrip()
    need(bool(needle), 'empty pinned source body')
    found = []
    for row in rows:
        item = row['event'].get('item', {})
        outputs = []
        if item.get('type') == 'command_execution':
            outputs = [item.get('aggregated_output', '')]
        elif item.get('type') == 'mcp_tool_call':
            outputs = _result_texts(item.get('result')) + _result_texts(item.get('error'))
        if any(isinstance(text, str) and needle in text for text in outputs):
            found.append(row['line'])
    return found


def validate_read_probe(state, files, name):
    guard = obj(state.get('read_guard'), f'{name}.read_guard')
    need(guard.get('verified') is True, f'{name}: read guard must be verified')
    need({'read_probe', 'read_probe_stdout', 'read_probe_stderr'} <= set(files), f'{name}: raw read probe evidence required')
    probe = read_json(files['read_probe'])
    raw_probe = read_json(files['read_probe_stdout'])
    need(probe.get('checks') == raw_probe.get('checks') and probe.get('passed') == raw_probe.get('passed'), f'{name}: probe summary differs from actual stdout')
    need(probe.get('passed', True) is True, f'{name}: read probe failed')
    checks = probe.get('checks')
    need(isinstance(checks, list) and checks, f'{name}: read probe checks required')
    need(guard.get('checked_paths') == len(checks), f'{name}: read probe count mismatch')
    paths = set()
    for check in checks:
        obj(check, 'read probe check')
        path = string(check.get('path'), 'probe.path')
        need(Path(path).is_absolute() and path not in paths, f'{name}: unique absolute probe paths required')
        paths.add(path)
        for field in ('expected_readable', 'readable'):
            need(isinstance(check.get(field), bool), f'{name}: boolean {field} required')
        need(check['expected_readable'] == check['readable'], f'{name}: read probe failed expected access')
        if check['readable']:
            need(check.get('errno') is None, f'{name}: successful read cannot have errno')
        else:
            need(check.get('errno') in {1, 13}, f'{name}: unreadable file is not evidence of permission denial')
    target_checks = [check for check in checks if check['path'] == state['skill_state']['target_path']]
    need(len(target_checks) == 1, f'{name}: target read probe missing')
    actual = target_checks[0]['readable']
    need(guard.get('target_readable') == actual == (name == 'treatment'), f'{name}: target read guard mismatch')
    return probe


def _check_hashes(root, files, hashes, label):
    need(isinstance(files, dict) and isinstance(hashes, dict), f'{label}: files and sha256 objects required')
    need(FILE_KEYS <= set(files) and set(hashes) == set(files), f'{label}: all trace files require hashes; six core files required')
    paths = {}
    for key, relative in files.items():
        path = local_path(root, relative)
        need(digest(path) == hashes[key], f'{label}: hash mismatch for {key}')
        paths[key] = path
    need(len(set(path.resolve() for path in paths.values())) == len(files), f'{label}: trace files must be distinct')
    return paths


def _short(value, label, maximum):
    string(value, label)
    need(len(value) <= maximum, f'{label}: maximum {maximum} characters')


def validate_evidence(evidence, arm, label):
    obj(evidence, label)
    kind = evidence.get('kind')
    if kind == 'event':
        line = evidence.get('line')
        need(isinstance(line, int) and not isinstance(line, bool), f'{label}: event line required')
        row = arm['by_line'].get(line)
        need(row is not None and not is_reasoning(row['event']), f'{label}: observable event line required')
        quote = string(evidence.get('quote'), f'{label}.quote')
        need(quote in event_quote_text(row['event']), f'{label}: quote differs from observed event fields')
    elif kind == 'answer':
        quote = string(evidence.get('quote'), f'{label}.quote')
        need(quote in arm['answer'], f'{label}: quote differs from recorded answer')
    elif kind == 'absence':
        need(evidence.get('scope') in {'recorded_events', 'answer'}, f'{label}: entire observed scope required')
        string(evidence.get('note'), f'{label}.note')
    elif kind == 'unavailable':
        string(evidence.get('note'), f'{label}.note')
    else:
        raise ValidationError(f'{label}: unsupported evidence kind')


def validate_report_scope(scope, arms):
    """Only display the main agent's explicit known/unknown distinction."""
    if scope is None:
        return
    obj(scope, 'report_scope')
    for group in ('confirmed', 'unknown'):
        need(isinstance(scope.get(group), list) and len(scope[group]) <= 6, f'report_scope.{group}: at most six rows required')
    for row in scope['confirmed']:
        obj(row, 'confirmed row')
        for key, maximum in [('topic', 80), ('a', 180), ('b', 180), ('conclusion', 280)]:
            _short(row.get(key), f'confirmed.{key}', maximum)
        evidence = row.get('evidence')
        need(isinstance(evidence, list) and evidence, 'confirmed evidence required')
        need({item.get('arm') for item in evidence if isinstance(item, dict)} == set(ARMS), 'confirmed evidence must cover both arms')
        for item in evidence:
            obj(item, 'confirmed evidence')
            name = item.get('arm')
            need(name in ARMS and item.get('kind') != 'unavailable', 'confirmed evidence must be available')
            validate_evidence(item, arms[name], 'confirmed evidence')
    for row in scope['unknown']:
        obj(row, 'unknown row')
        for key, maximum in [('topic', 80), ('missing', 180), ('why', 280), ('next_check', 280)]:
            _short(row.get(key), f'unknown.{key}', maximum)


def observed_metrics(arm, source_texts):
    """Compute counts and durations from records, never a quality or cost score."""
    events = arm['events']
    tool_types = {'command_execution', 'mcp_tool_call', 'web_search', 'file_change'}
    completed = {}
    for row in events:
        event, item = row['event'], row['event'].get('item', {})
        if event.get('type') == 'item.completed' and item.get('type') in tool_types:
            key = (item['type'], item.get('id', f'line-{row["line"]}'))
            completed[key] = row['line']
    source_reads = []
    for path, source in source_texts.items():
        if source.strip():
            lines = body_read_lines(events, source)
            if lines:
                source_reads.append({'path': path, 'lines': lines})
    state = arm['state']
    start = datetime.fromisoformat(state['started_at'].replace('Z', '+00:00'))
    finish = datetime.fromisoformat(state['finished_at'].replace('Z', '+00:00'))
    elapsed = (finish - start).total_seconds()
    need(elapsed >= 0, 'execution finish precedes start')
    turns = [row for row in events if row['event'].get('type') == 'turn.completed']
    usage = {}
    for key in ('input_tokens', 'cached_input_tokens', 'output_tokens'):
        values = []
        complete = bool(turns)
        for row in turns:
            recorded = row['event'].get('usage')
            if not isinstance(recorded, dict) or key not in recorded:
                complete = False
                continue
            value = recorded[key]
            need(type(value) is int and value >= 0, f'usage.{key}: nonnegative integer required')
            values.append(value)
        usage[key] = sum(values) if complete else None
    return {'completed_tools': len(completed), 'tool_lines': list(completed.values()),
            'source_reads': source_reads, 'elapsed_seconds': elapsed,
            'usage': usage, 'usage_lines': [row['line'] for row in turns],
            'observable_events': sum(not is_reasoning(row['event']) for row in events)}


def load_trace_run(root):
    root = Path(root).resolve()
    plan = read_json(root / 'trace-plan.json')
    record = read_json(root / 'trace-run.json')
    review = read_json(root / 'trace-review.json')
    need(all(data.get('schema_version') == 2 for data in (plan, record, review)), 'trace schema_version must be 2')
    source = obj(plan.get('source'), 'plan.source')
    string(plan.get('title'), 'plan.title')
    string(source.get('name'), 'source.name')
    safe_url(source.get('url'), 'source.url', github=True)
    local_path(root, source.get('entrypoint'))
    source_text = read_text(local_path(root, source['entrypoint']))
    inputs = obj(record.get('inputs_sha256'), 'inputs_sha256')
    need('trace-plan.json' in inputs and source['entrypoint'] in inputs, 'plan and source hashes required')
    for relative, expected in inputs.items():
        need(digest(local_path(root, relative)) == expected, f'input hash mismatch: {relative}')
    source_texts = {relative: read_text(local_path(root, relative)) for relative in inputs
                    if relative.startswith('source/') and Path(relative).suffix.lower() in {'.md', '.txt'}}
    control = obj(record.get('control'), 'control')
    need(control.get('method') == 'per_process_skills_config', 'unsupported skill control method')
    need(control.get('filesystem_isolation') is False, 'this renderer supports configuration control, not proven filesystem isolation')
    need(isinstance(control.get('global_config_unchanged'), bool), 'global config observation required')
    need(control.get('source_unchanged') is not False, 'source changed during execution')
    need(control.get('installed_copy_unchanged') is not False, 'installed source copy changed during execution')
    eligibility = control.get('eligibility')
    if eligibility is not None:
        obj(eligibility, 'eligibility')
        need(eligibility.get('status') == 'not_installed', 'target must be confirmed not installed for this mode')
        need(isinstance(eligibility.get('checked_count'), int) and not isinstance(eligibility['checked_count'], bool) and eligibility['checked_count'] > 0, 'eligibility checked_count required')
        timestamp(eligibility.get('observed_at'), 'eligibility.observed_at')
        need('eligibility.json' in inputs, 'eligibility evidence hash required')
        evidence = read_json(root / 'eligibility.json')
        need(all(evidence.get(key) == eligibility.get(key) for key in ('status', 'observed_at', 'checked_count', 'roots')), 'eligibility summary differs from recorded scan')
        checked = evidence.get('skills')
        need(isinstance(checked, list) and len(checked) == eligibility['checked_count'], 'eligibility scan count mismatch')
        need(evidence.get('collisions') == [], 'installed target collision found')
        scanned_paths = set()
        for skill in checked:
            obj(skill, 'eligibility skill')
            path = string(skill.get('path'), 'eligibility skill path')
            need(path not in scanned_paths and Path(path).is_absolute(), 'unique absolute eligibility paths required')
            scanned_paths.add(path)
            name = string(skill.get('name'), 'eligibility skill name')
            sha = skill.get('sha256')
            need(isinstance(sha, str) and re.fullmatch('[0-9a-f]{64}', sha), 'eligibility skill hash required')
            need(name.split(':')[-1].casefold() != source['name'].casefold() and sha != inputs[source['entrypoint']], 'target found in eligibility scan')
    need(isinstance(control.get('limitations'), list), 'control limitations required')
    for value in control['limitations']:
        string(value, 'control limitation')
    string(record.get('cli_version'), 'cli_version')
    timestamp(record.get('created_at'), 'created_at')
    cases = plan.get('cases')
    need(isinstance(cases, list) and [case.get('id') for case in cases] == ['Q1', 'Q2', 'Q3'], 'three ordered cases required')
    for case in cases:
        for key in ('title', 'prompt'):
            string(case.get(key), f'{case["id"]}.{key}')
        string(case.get('input'), 'case.input', empty=True)
        need(isinstance(case.get('criteria'), list) and case['criteria'], 'case criteria required')
    arms = {}
    integrity = review.get('integrity', {'validity': 'limited', 'notes': ['메인의 실행 오염 검토가 기록되지 않았습니다.']})
    obj(integrity, 'review.integrity')
    need(integrity.get('validity') in {'verified', 'limited', 'contaminated'}, 'unknown integrity validity')
    need(isinstance(integrity.get('notes'), list), 'integrity notes required')
    for note in integrity['notes']:
        string(note, 'integrity note')
    forced_undetermined = not control['global_config_unchanged'] or integrity['validity'] != 'verified'
    for name in ARMS:
        state = obj(record.get('arms', {}).get(name), f'arms.{name}')
        need(state.get('status') in {'completed', 'failed', 'timeout'}, 'unknown execution status')
        for key in ('started_at', 'finished_at'):
            timestamp(state.get(key), f'{name}.{key}')
        files = _check_hashes(root, state.get('files'), state.get('sha256'), name)
        rows = read_jsonl(files['events'])
        receipts = read_jsonl(files['receipts'])
        received = {}
        for receipt in receipts:
            entry = receipt['event']
            line = entry.get('line')
            need(isinstance(line, int) and not isinstance(line, bool) and line not in received, f'{name}: unique receipt line required')
            timestamp(entry.get('received_at'), 'receipt.received_at')
            received[line] = entry['received_at']
        need(set(received) == {row['line'] for row in rows}, f'{name}: event receipt coverage mismatch')
        inventory = read_json(files['inventory'])
        skills = inventory.get('skills')
        need(isinstance(skills, list) and isinstance(inventory.get('errors'), list), f'{name}: normalized skills inventory required')
        for skill in skills:
            obj(skill, 'skill')
            string(skill.get('path'), 'skill.path')
            string(skill.get('name'), 'skill.name')
            need(isinstance(skill.get('enabled'), bool), 'skill.enabled must be boolean')
        skill_state = obj(state.get('skill_state'), f'{name}.skill_state')
        need(isinstance(state.get('preflight_verified'), bool), 'preflight_verified must be boolean')
        need(isinstance(skill_state.get('target_enabled'), bool), 'target_enabled must be boolean')
        enabled_count = sum(skill['enabled'] for skill in skills)
        need(skill_state.get('enabled_skills') == enabled_count, f'{name}: enabled skill count differs from inventory')
        target_path = string(skill_state.get('target_path'), 'target_path')
        target_matches = [skill for skill in skills if skill['path'] == target_path]
        actual_target_enabled = any(skill['enabled'] for skill in target_matches)
        if 'target_registered' in skill_state:
            need(isinstance(skill_state['target_registered'], bool) and skill_state['target_registered'] == bool(target_matches), f'{name}: target registration differs from inventory')
        if eligibility is not None:
            need(skill_state.get('target_registered') == (name == 'treatment'), f'{name}: not-installed mode requires separate target registration')
        need(skill_state['target_enabled'] == actual_target_enabled, f'{name}: target flag differs from inventory')
        if state['preflight_verified']:
            need(not inventory['errors'], f'{name}: inventory errors invalidate preflight')
            need(actual_target_enabled == (name == 'treatment'), f'{name}: target OFF/ON preflight mismatch')
        answer = read_text(files['answer'])
        if state['status'] == 'completed':
            need(state.get('exit_code') == 0, f'{name}: completed execution requires exit 0')
            need(any(row['event'].get('type') == 'turn.completed' for row in rows), f'{name}: completion event missing')
            finals = [row['event']['item'].get('text', '') for row in rows
                      if row['event'].get('type') == 'item.completed' and row['event'].get('item', {}).get('type') == 'agent_message']
            need(bool(finals) and answer.rstrip('\n') == finals[-1].rstrip('\n'), f'{name}: final answer differs from emitted message')
        probe = None
        if control.get('filesystem_read_guard') is True:
            probe = validate_read_probe(state, files, name)
        elif eligibility is not None:
            raise ValidationError('not-installed comparison requires verified target read guard')
        exposure = body_read_lines(rows, source_text)
        if integrity['validity'] == 'verified':
            if name == 'baseline':
                need(not body_exposure_lines(rows, source_text), 'baseline contains pinned skill body in observed tool output; verified comparison is invalid')
            else:
                need(bool(exposure), 'treatment body read is unverified; verified comparison requires observed pinned body')
        forced_undetermined |= state['status'] != 'completed' or not state['preflight_verified']
        arms[name] = {'state': state, 'files': files, 'events': rows,
                      'by_line': {row['line']: row for row in rows}, 'received': received,
                      'inventory': inventory, 'answer': answer, 'prompt': read_text(files['prompt']),
                      'body_read_lines': exposure, 'read_probe': probe}
        arms[name]['metrics'] = observed_metrics(arms[name], source_texts)
    evaluator = obj(review.get('evaluated_by'), 'evaluated_by')
    need(evaluator.get('role') == 'main', 'only the main agent may evaluate')
    string(evaluator.get('agent_id'), 'evaluated_by.agent_id')
    for key, maximum in [('what', 180), ('takeaway', 240)]:
        _short(review.get(key), key, maximum)
    for name in ARMS:
        _short(review.get('workflow_summary', {}).get(name), f'workflow_summary.{name}', 320)
    differences = review.get('differences')
    need(isinstance(differences, list) and differences, 'main-authored differences required')
    for index, difference in enumerate(differences):
        obj(difference, 'difference')
        label = f'differences[{index}]'
        _short(difference.get('title'), f'{label}.title', 100)
        for key in ('observed', 'reason'):
            string(difference.get(key), f'{label}.{key}')
        need(difference.get('verdict') in VERDICTS, f'{label}: unknown verdict')
        for key, name in [('a', 'baseline'), ('b', 'treatment')]:
            validate_evidence(difference.get(key), arms[name], f'{label}.{key}')
        unavailable = any(difference[key]['kind'] == 'unavailable' for key in ('a', 'b'))
        need(not (forced_undetermined or unavailable) or difference['verdict'] == 'undetermined',
             f'{label}: failed/unverified comparison requires undetermined')
        if difference.get('case_id') is not None:
            need(difference['case_id'] in {'Q1', 'Q2', 'Q3'}, f'{label}: unknown case')
            if difference.get('criterion_id') is not None:
                case = next(case for case in cases if case['id'] == difference['case_id'])
                need(difference['criterion_id'] in {criterion['id'] for criterion in case['criteria']}, f'{label}: unknown criterion')
    need(isinstance(review.get('limits'), list), 'review limits required')
    for value in review['limits']:
        string(value, 'review limit')
    validate_report_scope(review.get('report_scope'), arms)
    run = {'root': root, 'plan': plan, 'record': record, 'review': review, 'arms': arms, 'integrity': integrity}
    run['analysis'] = load_analysis(run)
    return run


def _details(label, body, identifier='', opened=False):
    return f'<details{f" id={chr(34)}{identifier}{chr(34)}" if identifier else ""}{" open" if opened else ""}><summary>{_e(label)}</summary>{body}</details>'


def _pre(text, identifier='', class_name=''):
    return f'<pre{f" id={chr(34)}{identifier}{chr(34)}" if identifier else ""} class="{class_name}">{_e(text)}</pre>'


def _event_title(event):
    item = event.get('item', {})
    kind = item.get('type')
    title = {'command_execution': '명령 실행', 'file_change': '파일 변경', 'mcp_tool_call': '도구 호출',
             'web_search': '웹 검색', 'agent_message': '응답 메시지', 'todo_list': '작업 목록', 'plan_update': '계획 갱신', 'error': '실행 오류'}.get(kind)
    if title:
        phase = {'item.started': '시작', 'item.updated': '갱신', 'item.completed': '완료'}.get(event.get('type'), '')
        return f'{title} · {phase}' if phase else title
    return {'thread.started': '새 실행 시작', 'turn.started': '과제 시작', 'turn.completed': '과제 완료', 'turn.failed': '과제 실패', 'error': '실행 오류'}.get(event.get('type'), f'이벤트 · {event.get("type", "unknown")}')


def _command_label(command, arm):
    try:
        outer = shlex.split(command)
        parts = shlex.split(outer[-1]) if len(outer) >= 3 and outer[1] in {'-lc', '-c'} else outer
        if len(parts) == 2 and parts[0] == 'cat' and not parts[1].startswith('-'):
            path = Path(parts[1])
            base = Path(arm['state']['skill_state']['target_path']).parent
            short = str(path.relative_to(base)) if path.is_relative_to(base) else path.name
            return '파일 읽기', short
    except (ValueError, TypeError):
        pass
    return '명령 실행', '실제 명령과 출력 펼치기'


def _timeline(name, arm):
    """Keep one selectable detail per observed event, with original evidence IDs."""
    tools = {'command_execution', 'mcp_tool_call', 'web_search', 'file_change'}
    rows = [row for row in arm['events'] if not is_reasoning(row['event'])]
    observed_tools = [row for row in rows if row['event'].get('item', {}).get('type') in tools]
    tool_rows = [row for row in observed_tools if row['event'].get('type') == 'item.completed']
    messages = [row for row in rows if row['event'].get('item', {}).get('type') == 'agent_message']
    selected = (tool_rows or observed_tools or messages[-1:] or rows)[0]['line'] if rows else None
    buttons, auxiliary, details = [], [], []
    auxiliary_selected = False
    for number, row in enumerate(rows, 1):
        event, line = row['event'], row['line']
        fields = observed_fields(event)
        item = event.get('item', {})
        primary = fields.get('command') or fields.get('query')
        title = _event_title(event)
        is_action = event.get('type') == 'item.completed' and item.get('type') in tools
        is_final = bool(messages) and line == messages[-1]['line']
        is_error = event.get('type') in {'error', 'turn.failed'} or item.get('type') == 'error' or item.get('status') == 'failed'
        short = ''
        if is_action and item.get('type') == 'command_execution':
            title, short = _command_label(fields.get('command', ''), arm)
            if title == '파일 읽기':
                title = f'{Path(short).name} 읽기'
        if is_final:
            title = '최종 응답' if arm['state']['status'] == 'completed' else '마지막 응답 메시지'
        if item.get('type') == 'mcp_tool_call':
            primary = f'{fields.get("server", "")} / {fields.get("tool", "")}'
        identifier = f'event-{name}-L{line}'
        received = arm['received'][line]
        kind = '오류' if is_error else ('도구 완료' if is_action else ('응답' if is_final else '기록'))
        button = f'<button type="button" class="action-button" data-event-target="{identifier}" aria-controls="{identifier}" aria-pressed="{str(line == selected).lower()}"><span class="action-number">{number:02d}</span><span class="action-title">{_e(title)}</span><span class="action-meta">원본 L{line}</span><span class="action-kind">{kind}</span></button>'
        if is_action or is_final or is_error:
            buttons.append(button)
        else:
            auxiliary.append(button)
            auxiliary_selected = auxiliary_selected or line == selected
        content = f'<div class="event-top"><h4>{_e(title)}</h4><a href="#raw-{name}-L{line}">원본 L{line}</a></div>'
        event_kind = ' · '.join(str(value) for value in (event.get('type'), item.get('type')) if value)
        content += f'<p class="source-line">{_e(arm["state"]["files"]["events"])} · L{line}</p><p class="action-meta">{_e(event_kind)} · 수신 {_e(received)}</p>'
        if short:
            content += f'<p class="file-label">{_e(short)}</p>'
        if primary:
            content += _details('실제 명령·요청 펼치기', _pre(primary, class_name='command'))
        displayed = {'command', 'query', 'server', 'tool'}
        labels = {'aggregated_output': '도구 출력', 'text': '응답 메시지', 'result': '도구 결과', 'arguments': '도구 입력', 'error': '오류', 'message': '메시지', 'usage': '사용량', 'status': '상태', 'exit_code': '종료 코드', 'changes': '파일 변경', 'items': '작업 목록', 'plan': '계획', 'explanation': '설명', 'type': '이벤트 종류', 'thread_id': '실행 ID', 'action': '검색 동작'}
        for key, value in fields.items():
            if key in displayed:
                continue
            text = value if isinstance(value, str) else _json(value)
            full = _pre(text, class_name='event-output')
            label = labels.get(key, key)
            if len(text) > 200:
                content += f'<h4>{_e(label)}</h4>' + _pre(text[:180] + '\n…', class_name='event-preview')
                content += _details(f'{label} 원문 펼치기 · {len(text):,}자', full)
            else:
                content += f'<h4>{_e(label)}</h4>' + full
        content += _details('JSONL 원본 행', _pre(row['raw'], f'raw-{name}-L{line}', 'raw'))
        details.append(f'<section id="{identifier}" class="action-detail" data-event-id="{identifier}" aria-label="{_e(title)} L{line}"{" hidden" if line != selected else ""}>{content}</section>')
    empty = ''
    if not observed_tools:
        empty = '<p class="no-tools"><strong>기록된 도구 호출 0회</strong> · 응답 등 관측된 이벤트만 표시합니다.</p>'
    elif not tool_rows:
        empty = '<p class="no-tools"><strong>완료된 도구 실행 0회</strong> · 도구 관련 이벤트가 있으나 완료 이벤트는 없습니다.</p>'
    hidden = sum(is_reasoning(row['event']) for row in arm['events'])
    auxiliary_html = f'<details class="aux-events"{" open" if auxiliary_selected else ""}><summary>시작·중간 기록 {len(auxiliary)}개</summary>{"".join(auxiliary)}</details>' if auxiliary else ''
    return (empty + f'<div class="agent-body" id="timeline-{name}"><div class="action-list" aria-label="{LETTERS[name]} 관측 이벤트 선택">{"".join(buttons)}{auxiliary_html}</div>'
            f'<div class="action-inspector" aria-label="{LETTERS[name]} 선택한 이벤트 원문">{"".join(details)}</div></div>'
            f'<p class="trace-footnote">원본 행 번호로 순서 확인 · 추론 항목 {hidden}개 제외 · <a href="#answer-{name}">최종 응답 전체</a></p>')


def _evidence(evidence, name):
    kind, letter = evidence['kind'], LETTERS[name]
    if kind == 'event':
        label = f'{letter} 실제 이벤트 L{evidence["line"]}'
        body = f'<blockquote>{_e(evidence["quote"])}</blockquote><a href="#event-{name}-L{evidence["line"]}">{label} 확인 →</a>'
    elif kind == 'answer':
        body = f'<blockquote>{_e(evidence["quote"])}</blockquote><a href="#answer-{name}">{letter} 최종 응답 원문 확인 →</a>'
    elif kind == 'absence':
        scope = '기록된 관측 이벤트 전체' if evidence['scope'] == 'recorded_events' else '수집된 최종 응답 전체'
        anchor = f'timeline-{name}' if evidence['scope'] == 'recorded_events' else f'answer-{name}'
        body = f'<p>{_e(evidence["note"])}</p><p class="muted">부재 판단: 메인 · 검사 범위: {scope}</p><a href="#{anchor}">검사 범위 확인 →</a>'
    else:
        body = f'<p class="missing">근거 수집 불가 · {_e(evidence["note"])}</p>'
    return f'<section class="evidence"><h4>{letter} 근거</h4>{body}</section>'


def _table(headers, rows, caption):
    head = ''.join(f'<th scope="col">{_e(value)}</th>' for value in headers)
    body = ''.join('<tr>' + ''.join(f'<th scope="row">{cell}</th>' if i == 0 else f'<td>{cell}</td>' for i, cell in enumerate(row)) + '</tr>' for row in rows)
    return f'<div class="table-wrap"><table><caption>{_e(caption)}</caption><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _read_permission(state):
    command = state.get('command', [])
    overrides = {}
    for index, arg in enumerate(command[:-1]):
        if arg in {'-s', '--sandbox'}:
            return '읽기 전용' if command[index + 1] == 'read-only' else str(command[index + 1])
        if arg in {'-c', '--config'}:
            try:
                overrides.update(tomllib.loads(command[index + 1]))
            except (tomllib.TOMLDecodeError, TypeError):
                continue
    if overrides.get('sandbox_mode') == 'read-only':
        return '읽기 전용'
    permissions = overrides.get('permissions')
    profile = permissions.get(overrides.get('default_permissions'), {}) if isinstance(permissions, dict) else {}
    if not isinstance(profile, dict):
        return None
    if profile.get('extends') == ':read-only':
        rules = profile.get('filesystem', {})
        if isinstance(rules, dict) and all(isinstance(value, str) and value in {'read', 'deny'} for value in rules.values()):
            return '읽기 전용'
    return None


def _condition_table(run):
    arms = run['arms']
    left, right = (arms[name] for name in ARMS)
    rows = []
    def add(label, a, b):
        relation = '미확인' if a is None or b is None else ('같음' if a == b else '다름')
        rows.append([_e(label), _e(a if a is not None else '기록에서 확인 못함'), _e(b if b is not None else '기록에서 확인 못함'), _e(relation)])
    payloads = [arm['prompt'].split('## Q1\n', 1)[1] if '## Q1\n' in arm['prompt'] else None for arm in (left, right)]
    shared_questions = (payloads[0] is not None and payloads[0] == payloads[1]
                        and all(f'## {case["id"]}\n' in left['prompt'] for case in run['plan']['cases']))
    add('질문·입력', 'Q1~Q3 동일 전달' if shared_questions else None, 'Q1~Q3 동일 전달' if shared_questions else None)
    add('요청 모델', left['state']['model'], right['state']['model'])
    add('요청 추론 설정', left['state']['reasoning_effort'], right['state']['reasoning_effort'])
    add('기본 파일 권한', _read_permission(left['state']), _read_permission(right['state']))
    def target(arm):
        state = arm['state']['skill_state']
        if state.get('target_registered') is False:
            return '미등록'
        return '등록·활성' if state['target_enabled'] else '등록·비활성'
    add('대상 스킬', target(left), target(right))
    add('대상 파일 읽기', '허용' if left['state'].get('read_guard', {}).get('target_readable') is True else ('차단' if left['read_probe'] else None),
        '허용' if right['state'].get('read_guard', {}).get('target_readable') is True else ('차단' if right['read_probe'] else None))
    return _table(['항목', 'A · 기본 실행', 'B · 스킬 적용', '조건'], rows, '같게 둔 조건과 의도적으로 바꾼 조건')


def _line_links(name, lines):
    return ' '.join(f'<a href="#event-{name}-L{line}">{LETTERS[name]} L{line}</a>' for line in lines)


def _metrics(run):
    left, right = (run['arms'][name]['metrics'] for name in ARMS)
    rows = []
    def numeric(label, a, b, unit='', precision=0, references=None):
        if a is None or b is None:
            return
        def fmt(value):
            return f'{value:,.{precision}f}{unit}'
        cells = [_e(fmt(value)) for value in (a, b)]
        if references:
            cells = [f'{cell} <span class="metric-ref">{references[name]}</span>' for cell, name in zip(cells, ARMS)]
        difference = f'{b - a:+,.{precision}f}{unit}' if a != b else '같음'
        rows.append([_e(label), *cells, _e(difference)])
    def event_references(lines_by_arm):
        return {name: (_line_links(name, lines_by_arm[name]) or f'<a href="#timeline-{name}">{LETTERS[name]} 관측 범위</a>') for name in ARMS}
    numeric('완료 도구 호출', left['completed_tools'], right['completed_tools'], '회', references=event_references({name: run['arms'][name]['metrics']['tool_lines'] for name in ARMS}))
    source_lines = {name: sorted({line for item in run['arms'][name]['metrics']['source_reads'] for line in item['lines']}) for name in ARMS}
    numeric('원문 확인 지침 파일', len(left['source_reads']), len(right['source_reads']), '개', references=event_references(source_lines))
    numeric('프로세스 경과 시간', left['elapsed_seconds'], right['elapsed_seconds'], '초', 1,
            references={name: f'<a href="#process-{name}">{LETTERS[name]} 시작·종료 기록</a>' for name in ARMS})
    for key, label in [('input_tokens', '입력 토큰 · 캐시 포함'), ('cached_input_tokens', '그중 캐시 입력'), ('output_tokens', '출력 토큰')]:
        numeric(label, left['usage'][key], right['usage'][key], references=event_references({name: run['arms'][name]['metrics']['usage_lines'] for name in ARMS}))
    content = '<section id="observations"><h2>기록에서 확인한 수치</h2><p class="section-note">조건별 한 번의 실행에서 관찰한 값입니다.</p>'
    content += _table(['관찰 항목', 'A', 'B', 'B − A'], rows, '실제 이벤트와 프로세스 시각에서 계산')
    evidence = ''
    for name in ARMS:
        read_files = run['arms'][name]['metrics']['source_reads']
        evidence += f'<h4>{LETTERS[name]} · 성공 출력에서 원문이 확인된 파일</h4>'
        evidence += ('<ul>' + ''.join(f'<li>{_e(item["path"])} · {_line_links(name, item["lines"])}</li>' for item in read_files) + '</ul>') if read_files else '<p>확인된 지침 원문 없음</p>'
    content += _details('계산 근거와 지침 파일', evidence + '<p>파일 수는 고정한 source/의 Markdown·텍스트 파일 전체가 성공한 도구 출력에 나타난 경우만 셉니다. 토큰은 turn.completed 기록의 합이며, 캐시 입력을 입력 토큰에 다시 더하지 않습니다. 경과 시간은 프로세스 시작·종료 시각의 차이입니다.</p>')
    return content + '</section>'


def _report_scope(run):
    scope = run['review'].get('report_scope')
    if scope is None:
        return '<section id="scope"><h2>확인 범위</h2><p class="missing">메인이 확인된 차이와 판단 불가 항목을 아직 정리하지 않았습니다.</p></section>'
    content = '<section id="scope"><h2>어디까지 확인했나</h2><p class="section-note">메인이 양쪽 원문을 확인해 정리한 내용입니다.</p><div class="columns scope-columns"><section><h3>확인된 차이</h3>'
    for index, row in enumerate(scope['confirmed'], 1):
        evidence = ''.join(_evidence(item, item['arm']) for item in row['evidence'])
        content += f'<article class="scope-card"><h4>{_e(row["topic"])}</h4><dl><dt>A</dt><dd>{_e(row["a"])}</dd><dt>B</dt><dd>{_e(row["b"])}</dd></dl><p class="scope-conclusion">{_e(row["conclusion"])}</p>{_details("양쪽 근거", evidence)}</article>'
    content += '</section><section><h3>아직 판단할 수 없는 것</h3>'
    for row in scope['unknown']:
        content += f'<article class="scope-card unknown"><h4>{_e(row["topic"])}</h4><p><strong>없는 자료</strong> · {_e(row["missing"])}</p><p>{_e(row["why"])}</p><p class="scope-next"><strong>다음 확인</strong> · {_e(row["next_check"])}</p></article>'
    return content + '</section></div></section>'


def _captures(root, captures=None):
    if captures is None:
        from native_captures import load_native_captures
        captures = load_native_captures(root)
    if not captures:
        return '<section id="log-captures"><h2>원본 로그 화면</h2><p class="missing">원본 로그 화면 미촬영. 비교 HTML의 스크린샷으로 대체하지 않았습니다.</p></section>'
    figures = []
    for index, capture in enumerate(captures, 1):
        uri = capture.get('data_uri')
        need(isinstance(uri, str) and re.fullmatch(r'data:image/(?:png|jpeg);base64,[A-Za-z0-9+/=]+', uri), 'unsupported capture data URI')
        for dimension in ('width', 'height'):
            need(isinstance(capture.get(dimension), int) and capture[dimension] > 0, 'capture dimensions required')
        visible = capture['visible_lines']
        source_label = f'{capture["source_file"]} · L{visible["start"]}–L{visible["end"]}'
        meta = f'{capture["surface"]} · {capture["captured_at"]}'
        image = f'<img src="{uri}" width="{capture["width"]}" height="{capture["height"]}" alt="{_e(capture["title"])}" loading="lazy">'
        image_details = _details(f'{LETTERS[capture["arm"]]} 캡처 펼치기', image, f'capture-{capture["arm"]}-{index}')
        figures.append(f'<figure class="trace-capture"><figcaption><h3>{_e(capture["title"])}</h3><p>{_e(capture["caption"])}<br>{_e(source_label)}<br>{_e(meta)}</p></figcaption>{image_details}</figure>')
    return f'<section id="log-captures"><h2>원본 로그 화면</h2><p class="section-note">원본 로그 캡처 {len(captures)}장입니다. 촬영 정보는 아래에 표시했으며, 이미지는 각각 펼쳐 볼 수 있습니다. 원본 파일과 캡처의 해시를 함께 검증합니다.</p>' + ''.join(figures) + '</section>'


def _context(run):
    plan, record, review = run['plan'], run['record'], run['review']
    source = plan['source']
    body = []
    eligibility = record['control'].get('eligibility')
    eligibility_note = f'미설치 확인 · 알려진 경로의 {eligibility["checked_count"]}개 스킬 기록 대조' if eligibility else '각 실행에 별도 스킬 설정 전달'
    body.append(f'<section class="control" id="conditions"><h2>실험 조건</h2><p>{_e(eligibility_note)}</p><div class="columns">')
    for name in ARMS:
        arm = run['arms'][name]
        state = arm['state']
        flag = 'ON' if state['skill_state']['target_enabled'] else 'OFF'
        condition = ('스킬 없음' if name == 'baseline' else '임시 적용') if eligibility else f'대상 스킬 {flag}'
        verified = '사전 목록 검증됨' if state['preflight_verified'] else '사전 검증 미완료'
        status = {'completed': '실행 완료', 'failed': '실행 실패', 'timeout': '시간 초과'}[state['status']]
        read_state = '본문 열람 확인' if arm['body_read_lines'] else '본문 열람 미확인'
        if name == 'baseline' and arm['read_probe']:
            read_state = '대상 읽기 차단 확인'
        body.append(f'<section class="arm-control"><div class="control-title"><h3>{LETTERS[name]} · {condition}</h3><span class="state">{_e(status)}</span></div><p>{_e(read_state)}</p><p class="muted">{_e(verified)} · 조회된 목록: {state["skill_state"]["enabled_skills"]}개 활성</p></section>')
    body.append('</div>' + _condition_table(run) + '</section>')
    validity = {'verified': '기록 범위에서 비교 조건 확인', 'limited': '비교 조건 관찰 제한', 'contaminated': '비교 조건 오염 확인'}[run['integrity']['validity']]
    plugin_state = record['control'].get('plugins_disabled')
    plugins = '실행별 비활성화 설정' if plugin_state is True else '차단 미확인'
    body.append(f'<p class="integrity-note"><strong>{validity}</strong> · <a href="#technical-details">검증 기록 보기</a></p>')
    body.append(_report_scope(run))
    body.append(_metrics(run))
    questions = ''
    for case in plan['cases']:
        questions += f'<section class="case"><h3>{_e(case["id"])} · {_e(case["title"])}</h3>{_pre(case["prompt"])}{_pre(case["input"])}<ul>'
        questions += ''.join(f'<li><strong>{_e(criterion["label"])}</strong> · {_e(criterion["rule"])}</li>' for criterion in case['criteria']) + '</ul></section>'
    body.append(_details('공통 과제와 사전 판단 기준', questions, 'tasks'))
    global_state = '변경 없음 확인' if record['control']['global_config_unchanged'] else '변경 없음 미확인'
    appendix = f'<p>전역 설정: {global_state} · 플러그인: {plugins}.</p><ul>'
    appendix += ''.join(f'<li>{_e(value)}</li>' for value in run['integrity']['notes'] + record['control']['limitations'] + review['limits']) + '</ul>'
    appendix += f'<p>대상 출처: <a href="{_e(source["url"])}" rel="noreferrer noopener">{_e(source["name"])}</a> · commit {_e(source.get("commit") or "미확인")}</p><p>CLI {_e(record["cli_version"])} · 수집 {_e(record["created_at"])} · 평가 {_e(review["evaluated_by"]["agent_id"])}</p>'
    for name in ARMS:
        arm = run['arms'][name]
        state = arm['state']
        hashes = ''.join(f'<li>{_e(state["files"][key])}<code class="hash">{_e(state["sha256"][key])}</code></li>' for key in sorted(state['files']))
        probe = '<h4>실제 읽기 검사 결과</h4>' + _pre(_json(arm['read_probe'])) if arm['read_probe'] else ''
        times = f'<p class="source-line">trace-run.json · arms.{name}</p>' + _pre(_json({key: state[key] for key in ('started_at', 'finished_at', 'status', 'exit_code')}), f'process-{name}', 'process-record')
        appendix += _details(f'{LETTERS[name]} 실행 기록·스킬 목록·해시', '<h4>프로세스 시각·종료 상태</h4>' + times + '<h4>실제 호출 인자</h4>' + _pre(_json(state['command'])) + '<h4>작업 폴더</h4>' + _pre(state['cwd']) + '<h4>사전 스킬 목록</h4>' + _pre(_json(arm['inventory'])) + probe + '<h4>전달 프롬프트</h4>' + _pre(arm['prompt']) + '<h4>보존 파일 SHA256</h4><ul>' + hashes + '</ul>')
    body.append(_details('실행 기록과 검증 세부사항', appendix, 'technical-details'))
    return ''.join(body)


def _behavior(run):
    body = ['<section id="workflow"><div class="section-heading"><h2>행동 비교</h2><span>각 열에서 독립적으로 선택</span></div><p class="section-note">실제 JSONL에 기록된 순서입니다. 양쪽 단계는 같은 시점이나 같은 작업을 뜻하지 않습니다.</p><div class="agent-grid">']
    for name in ARMS:
        arm = run['arms'][name]
        metrics = arm['metrics']
        state = arm['state']
        registered = state['skill_state'].get('target_registered')
        condition = '스킬 없음' if registered is False else ('스킬 적용' if state['skill_state']['target_enabled'] else '대상 스킬 비활성')
        status = {'completed': '실행 완료', 'failed': '실행 실패', 'timeout': '시간 초과'}[state['status']]
        body.append(f'<article class="agent-panel" data-arm="{name}"><header class="agent-header"><div class="agent-label"><h3>{LETTERS[name]} · {condition}</h3><span class="agent-state">{status}</span></div><p class="agent-stats">완료된 도구 실행 {metrics["completed_tools"]}회 · {metrics["elapsed_seconds"]:.1f}초</p><p class="flow-summary">{_e(run["review"]["workflow_summary"][name])}</p></header>{_timeline(name, arm)}</article>')
    return ''.join(body) + '</div></section>'


def _answer_parts(answer):
    """Split only exact, ordered Q1–Q3 Markdown headings; retain original bytes."""
    if re.search(r'^\s*(?:`{3,}|~{3,})', answer, re.MULTILINE):
        return None
    markers = list(re.finditer(r'^## (Q[123])[ \t]*\r?$', answer, re.MULTILINE))
    if [match.group(1) for match in markers] != ['Q1', 'Q2', 'Q3'] or answer[:markers[0].start()].strip():
        return None
    starts = [0, markers[1].start(), markers[2].start(), len(answer)]
    return {f'Q{index + 1}': answer[starts[index]:starts[index + 1]] for index in range(3)}


def _answers(run):
    parts = {name: _answer_parts(run['arms'][name]['answer']) for name in ARMS}
    case_names = ['all', 'Q1', 'Q2', 'Q3'] if all(parts.values()) else ['all']
    body = ['<section id="answers"><h2>최종 응답 비교</h2><p class="section-note">실제 마지막 메시지와 대조한 원문입니다. 질문별 보기는 정확한 Q1·Q2·Q3 경계가 양쪽에 있을 때만 표시합니다.</p><div class="answer-toolbar" aria-label="응답 범위 선택">']
    for name in case_names:
        body.append(f'<button type="button" data-answer-select="{name}" aria-pressed="{str(name == "all").lower()}">{"전체 원문" if name == "all" else name}</button>')
    body.append('</div><div class="answer-grid">')
    for name in ARMS:
        body.append(f'<article class="answer-panel"><h3>{LETTERS[name]} · 최종 응답</h3>')
        for case in case_names:
            text = run['arms'][name]['answer'] if case == 'all' else parts[name][case]
            identifier = f'answer-{name}' + (f'-{case}' if case != 'all' else '')
            body.append(f'<section class="answer-case" data-answer-case="{case}"{" hidden" if case != "all" else ""}>{_pre(text, identifier, "answer")}</section>')
        body.append('</article>')
    return ''.join(body) + '</div></section>'


def _verdicts(run):
    buttons, details = [], []
    for index, difference in enumerate(run['review']['differences'], 1):
        verdict = difference['verdict']
        identifier = f'difference-{index}'
        badge = f'<span class="verdict {verdict}">{VERDICTS[verdict]}</span>'
        buttons.append(f'<button type="button" class="verdict-button" data-verdict-target="{identifier}" aria-controls="{identifier}" aria-pressed="{str(index == 1).lower()}"><span class="action-number">{index:02d}</span><span class="action-title">{_e(difference["title"])}</span>{badge}</button>')
        evidence = f'<div class="columns evidence-columns">{_evidence(difference["a"], "baseline")}{_evidence(difference["b"], "treatment")}</div>'
        details.append(f'<article class="verdict-detail difference" id="{identifier}"{" hidden" if index != 1 else ""}><div class="difference-heading"><h3>{_e(difference["title"])}</h3>{badge}</div><p>{_e(difference["observed"])}</p>{evidence}<p class="reason"><strong>판정 이유</strong> {_e(difference["reason"])}</p></article>')
    return '<section id="differences"><h2>메인 에이전트의 최종 정리</h2><p class="section-note">판정과 발췌는 메인 에이전트가 작성했습니다. 항목을 선택하면 양쪽 근거와 판정 이유가 보입니다.</p><div class="verdict-layout"><div class="verdict-list" aria-label="판정 항목 선택">' + ''.join(buttons) + '</div><div class="verdict-inspector">' + ''.join(details) + '</div></div></section>'


def build_html(run):
    from native_captures import load_native_captures
    captures = load_native_captures(run['root'])
    plan, review = run['plan'], run['review']
    analysis = run.get('analysis')
    title = f'{plan["source"]["name"]} · 실행 비교'
    what = analysis['summary']['purpose'] if analysis else review['what']
    body = [f'<header class="appbar"><div><h1>{_e(title)}</h1><p class="what">{_e(what)}</p></div><a class="source-link" href="{_e(plan["source"]["url"])}" rel="noreferrer noopener">스킬 출처 ↗</a></header>']
    body.append(f'<section class="summary-strip" aria-label="이번 실행에서 확인한 차이"><div class="summary-copy"><p class="takeaway">{_e(review["takeaway"])}</p></div><div class="summary-metrics">')
    for name in ARMS:
        metrics = run['arms'][name]['metrics']
        body.append(f'<div class="metric-chip"><strong>{LETTERS[name]}</strong><span>완료 도구 {metrics["completed_tools"]}회</span><span>{metrics["elapsed_seconds"]:.1f}초</span></div>')
    body.append('</div></section>')
    body.append('<nav class="dashboard-tabs" role="tablist" aria-label="비교 보고서 탐색">')
    panels = [('comparison', '흐름과 차이', comparison_panel(run, captures)), ('behavior', '전체 로그', _behavior(run)), ('answers', '최종 응답', _answers(run)), ('verdicts', '최종 정리', _verdicts(run))]
    if analysis:
        panels += [('files', '스킬 구조', files_panel(run, analysis)), ('flow', '선언된 절차', flow_panel(run, analysis)), ('tools', '도구·의존성', tools_panel(run, analysis))]
    panels += [('context', '조건·한계', _context(run)), ('captures', '참고 화면', _captures(run['root'], captures))]
    initial = panels[0][0]
    for name, label, _ in panels:
        selected = name == initial
        body.append(f'<button type="button" role="tab" id="tab-{name}" data-panel="{name}" aria-selected="{str(selected).lower()}" aria-controls="panel-{name}" tabindex="{0 if selected else -1}">{label}</button>')
    body.append('</nav>')
    for name, _, content in panels:
        body.append(f'<section id="panel-{name}" class="dashboard-panel" role="tabpanel" aria-labelledby="tab-{name}"{" hidden" if name != initial else ""}>{content}</section>')
    body.append('<footer>구조 설명은 고정한 소스에, 실행 설명은 실제 로그에 근거합니다. A/B 결과는 한 번의 관측이며 품질 판정은 메인 에이전트가 작성했습니다.</footer>')
    csp = "'sha256-" + base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode() + "'"
    template = read_text(TEMPLATE)
    replacements = {'TITLE': _e(title), 'SCRIPT_CSP': csp, 'BODY': ''.join(body), 'SCRIPT': SCRIPT}
    return re.sub(r'\{\{(TITLE|SCRIPT_CSP|BODY|SCRIPT)\}\}', lambda match: replacements[match[1]], template)


def render_trace_report(root, output=None, *, check=False):
    run = load_trace_run(root)
    target = Path(output) if output else run['root'] / 'trace-report.html'
    target = target.resolve()
    need(target.is_relative_to(run['root']) and target.suffix == '.html', 'HTML output must stay inside the run folder')
    need(target not in {path for arm in run['arms'].values() for path in arm['files'].values()}, 'output cannot overwrite trace evidence')
    html = build_html(run)
    if check:
        need(target.is_file(), 'report does not exist; generate it with this renderer first')
        need(target.read_bytes() == html.encode('utf-8'),
             'report does not match the bundled renderer and current evidence; regenerate it instead of editing HTML')
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.trace-report-', suffix='.html', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(html)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check', action='store_true', help='Verify existing HTML matches this renderer and current evidence without writing files')
    args = parser.parse_args(argv)
    try:
        target = render_trace_report(args.run_dir, args.output, check=args.check)
        print(f'Report matches bundled renderer: {target}' if args.check else target)
    except (ValidationError, OSError) as exc:
        print(f'trace report validation failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
