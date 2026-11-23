"""Paired workflow exploration using already validated review evidence only."""
from html import escape
import json

ARMS = ('baseline', 'treatment')
LETTERS = {'baseline': 'A', 'treatment': 'B'}


def e(value):
    return escape(str(value), quote=True).replace('\r', '&#13;')


def highlighted(text, quote=''):
    """Escape first-class text segments without modifying or inferring a quote."""
    if not quote or quote not in text:
        return e(text)
    return '<mark>' + e(quote) + '</mark>' if text == quote else ('<mark>' + e(quote) + '</mark>').join(e(part) for part in text.split(quote))


def evidence_key(evidence, arm):
    if evidence['kind'] == 'event':
        return f'event-{arm}-L{evidence["line"]}'
    if evidence['kind'] == 'answer':
        return f'answer-{arm}'
    return None


def condition_label(arm):
    state = arm['state']
    if not state['preflight_verified']:
        return '스킬 상태 미검증'
    skill = state['skill_state']
    if skill.get('target_registered') is False:
        return '스킬 없음'
    return '스킬 활성' if skill['target_enabled'] else '대상 스킬 비활성'


def flow_lane(run, name):
    from trace_report import is_reasoning, _event_title, _command_label
    arm = run['arms'][name]
    refs = {}
    for index, diff in enumerate(run['review']['differences'], 1):
        evidence = diff['a' if name == 'baseline' else 'b']
        key = evidence_key(evidence, name)
        if key:
            refs.setdefault(key, []).append((index, diff))
    steps = []
    rows = [row for row in arm['events'] if not is_reasoning(row['event'])]
    # Coalesce lifecycle events by actual item ID; keep unfinished tool attempts.
    last_item_line = {row['event']['item']['id']: row['line'] for row in rows
                      if isinstance(row['event'].get('item'), dict) and row['event']['item'].get('id')}
    item_lines = {}
    for row in rows:
        item_id = row['event'].get('item', {}).get('id')
        if item_id:
            item_lines.setdefault(item_id, []).append(row['line'])
    last_message = next((row['line'] for row in reversed(rows) if row['event'].get('item', {}).get('type') == 'agent_message'), None)
    for row in rows:
        event, line = row['event'], row['line']
        item = event.get('item', {})
        kind = item.get('type')
        if item.get('id') and last_item_line[item['id']] != line:
            continue
        if kind not in {'command_execution', 'mcp_tool_call', 'web_search', 'file_change', 'agent_message', 'error', 'plan_update', 'todo_list'} and event['type'] not in {'turn.failed', 'error'}:
            continue
        key = f'event-{name}-L{line}'
        related = {}
        for linked_line in item_lines.get(item.get('id'), [line]):
            related.update(refs.get(f'event-{name}-L{linked_line}', []))
        if line == last_message:
            related.update(refs.get(f'answer-{name}', []))
        title = _event_title(event)
        if kind == 'command_execution':
            label, path = _command_label(item.get('command', ''), arm)
            title = f'{path.rsplit("/", 1)[-1]} 읽기' if label == '파일 읽기' else label
        elif kind == 'mcp_tool_call':
            title = item.get('tool', '도구 호출')
        elif kind == 'agent_message':
            title = '최종 응답' if line == last_message and arm['state']['status'] == 'completed' else '응답 메시지'
        extra = ''
        if related:
            index = min(related)
            targets = ' '.join(f'comparison-{i}' for i in sorted(related))
            attrs = f'href="#comparison-{index}" data-comparison-target="comparison-{index}" data-comparison-targets="{targets}"'
            state = 'changed' if any(d['verdict'] != 'unchanged' for d in related.values()) else 'reviewed'
            caption = related[index]['title'] if len(related) == 1 else f'연결된 비교 {len(related)}개'
            if len(related) > 1:
                extra = '<div class="related-comparisons">' + ''.join(f'<a href="#comparison-{i}" data-comparison-target="comparison-{i}">{e(d["title"])}</a>' for i, d in sorted(related.items())) + '</div>'
        else:
            attrs, state, caption = f'href="#{key}"', 'observed', '원문 열기'
        status = item.get('status', '')
        steps.append(f'<li class="flow-step {state}"><a {attrs}><span class="step-index">{len(steps)+1:02d}</span><span><strong>{e(title)}</strong><small>{e(caption)}</small></span><span class="step-line">L{line}{" · 실패" if status == "failed" else ""}</span></a>{extra}</li>')
    condition = condition_label(arm)
    status = {'completed': '완료', 'failed': '실패', 'timeout': '시간 초과'}[arm['state']['status']]
    return (f'<article class="flow-lane" data-arm="{name}"><header><span class="arm-letter">{LETTERS[name]}</span>'
            f'<div><h3>{condition}</h3><p>{e(run["review"]["workflow_summary"][name])}</p></div><span class="run-status">{status}</span></header>'
            f'<ol class="flow-steps">{"".join(steps)}</ol>'
            f'{"<p class=empty-evidence>표시할 관측 행동이 없습니다.</p>" if not steps else ""}</article>')


def paired_evidence(run, name, evidence, captures, case_id=None):
    from trace_report import observed_fields, _answer_parts
    arm, kind = run['arms'][name], evidence['kind']
    key = evidence_key(evidence, name)
    content = f'<article class="paired-evidence" data-arm="{name}"' + (f' data-evidence-source="{key}"' if key else '') + '>'
    content += f'<header><span class="arm-letter">{LETTERS[name]}</span><h4>{condition_label(arm)}</h4></header>'
    if kind in {'absence', 'unavailable'}:
        content += f'<p class="empty-evidence">{e(evidence["note"])}</p>'
        if kind == 'absence':
            scope = '기록된 관측 이벤트 전체' if evidence['scope'] == 'recorded_events' else '수집된 최종 응답 전체'
            target = f'timeline-{name}' if evidence['scope'] == 'recorded_events' else f'answer-{name}'
            content += f'<p class="evidence-source">기록 부재 확인 범위 · <a href="#{target}">{scope}</a></p>'
        return content + '</article>'
    quote = evidence['quote']
    captured_line = evidence.get('line')
    if kind == 'answer':
        parts = _answer_parts(arm['answer'])
        part = parts.get(case_id) if parts else None
        scoped = part is not None and quote in part
        fields = [('응답 원문', part if scoped else arm['answer'])]
        source = f'answer.txt · {case_id} 원문' if scoped else 'answer.txt · 최종 응답'
        captured_line = next((row['line'] for row in reversed(arm['events']) if row['event'].get('type') == 'item.completed'
                              and row['event'].get('item', {}).get('type') == 'agent_message'
                              and row['event']['item'].get('text', '').rstrip('\n') == arm['answer'].rstrip('\n')), None)
    else:
        row = arm['by_line'][evidence['line']]
        observed = observed_fields(row['event'])
        labels = {'command': '실행한 명령', 'arguments': '도구에 전달한 입력', 'aggregated_output': '반환된 출력',
                  'result': '반환된 결과', 'text': '응답 원문', 'query': '검색 요청', 'tool': '도구', 'server': '서버',
                  'error': '오류', 'message': '메시지', 'status': '상태', 'exit_code': '종료 코드'}
        fields = [(labels.get(k, k), v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=2))
                  for k, v in observed.items() if k not in {'status', 'exit_code'}]
        state = ' · '.join(str(observed[k]) for k in ('status', 'exit_code') if k in observed)
        source = f'events.jsonl · L{evidence["line"]}' + (f' · {state}' if state else '')
    content += f'<p class="evidence-source"><a href="#{key}">{e(source)}</a></p>'
    content += f'<blockquote class="selected-quote">{highlighted(quote, quote)}</blockquote>'
    for label, text in fields:
        content += f'<section class="log-field"><h5>{e(label)}</h5><pre>{highlighted(text, quote)}</pre></section>'
    links = []
    for index, capture in enumerate(captures, 1):
        if capture['arm'] != name:
            continue
        visible = capture['visible_lines']
        # Only attach screenshots that actually include the cited event line.
        if captured_line is not None and capture['source_file'] == arm['state']['files']['events'] and visible['start'] <= captured_line <= visible['end']:
            links.append(f'<a href="#capture-{name}-{index}">{e(capture["title"])}</a>')
    content += '<div class="capture-links">참고 화면 · ' + ' · '.join(links) + '</div>' if links else ''
    return content + '</article>'


def comparison_panel(run, captures):
    body = ['<section class="comparison-workspace" id="comparison-workflow"><div class="section-heading"><h2>같은 작업, 달라진 흐름</h2><a href="#panel-behavior">전체 로그 탐색</a></div>'
            '<p class="section-note">각 실행의 실제 행동을 순서대로 표시합니다. 강조된 단계를 누르면 연결된 A/B 근거가 함께 열립니다.</p>']
    tasks = ''.join(f'<section><h4>{e(case["id"])} · {e(case["title"])}</h4><p>{e(case["prompt"])}</p></section>' for case in run['plan']['cases'])
    body.append(f'<details class="shared-task"><summary>두 실행에 준 같은 작업 확인</summary><div class="task-list">{tasks}</div></details>')
    body.append('<div class="flow-lanes">' + ''.join(flow_lane(run, name) for name in ARMS) + '</div>')
    body.append('<p class="flow-caption">두 열의 순서는 각 실행 안의 순서입니다. 같은 높이에 놓인 행동이 같은 작업이나 시점을 뜻하지는 않습니다.</p>')
    body.append('<section class="comparison-section"><div class="section-heading"><h2>차이가 생긴 지점</h2><span>항목 하나로 양쪽 로그 함께 보기</span></div><div class="comparison-layout"><nav class="comparison-index" aria-label="비교할 작업 선택">')
    for index, diff in enumerate(run['review']['differences'], 1):
        changed = diff['verdict'] != 'unchanged'
        body.append(f'<button type="button" class="comparison-button" data-comparison-target="comparison-{index}" aria-controls="comparison-{index}" aria-pressed="{str(index == 1).lower()}"><span class="comparison-status {"changed" if changed else "same"}">{"근거 비교" if changed else "차이 없음"}</span><strong>{e(diff["title"])}</strong><span>{e(diff["observed"])}</span></button>')
    body.append('</nav><div class="comparison-inspector" aria-label="선택한 작업의 양쪽 실제 로그">')
    for index, diff in enumerate(run['review']['differences'], 1):
        body.append(f'<section class="comparison-detail" id="comparison-{index}" tabindex="-1"{" hidden" if index != 1 else ""}><header class="comparison-heading"><h3>{e(diff["title"])}</h3><p>{e(diff["observed"])}</p></header><div class="paired-logs">')
        body.extend(paired_evidence(run, name, diff[key], captures, diff.get('case_id')) for name, key in [('baseline', 'a'), ('treatment', 'b')])
        body.append(f'</div><p class="comparison-note">하이라이트는 비교 근거로 선택한 원문 발췌입니다. <a href="#difference-{index}">판정과 설명 보기</a></p></section>')
    body.append('</div></div></section></section>')
    return ''.join(body)
