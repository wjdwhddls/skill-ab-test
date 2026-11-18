"""Render main-authored skill anatomy; never infer instructions or grade results."""
from html import escape
from pathlib import Path

from report_lib import read_text, local_path, ValidationError


def e(value):
    return escape(str(value), quote=True)


def file_id(analysis, path):
    return 'skill-file-' + str(next(i for i, item in enumerate(analysis['files']) if item['path'] == path))


def source_refs(analysis, refs):
    return '<div class="source-refs">' + ''.join(
        f'<a href="#{file_id(analysis, ref["path"])}-L{ref["start"]}">{e(ref["path"].removeprefix("source/"))} · L{ref["start"]}–{ref["end"]}</a>'
        for ref in refs) + '</div>'


def observations(items):
    result = []
    for item in items:
        arm = item['arm']
        letter = 'A' if arm == 'baseline' else 'B'
        target = f'event-{arm}-L{item["line"]}' if item['kind'] == 'event' else f'answer-{arm}'
        label = f'{letter} 로그 L{item["line"]}' if item['kind'] == 'event' else f'{letter} 응답 원문'
        result.append(f'<a href="#{target}">{label}</a>')
    return '<div class="source-refs">' + ''.join(result) + '</div>'


def files_panel(run, analysis):
    from trace_report import body_read_lines
    summary = analysis['summary']
    intro = '<dl class="skill-facts">' + ''.join(
        f'<div><dt>{label}</dt><dd>{e(summary[key])}</dd></div>'
        for key, label in [('activation', '언제 쓰나'), ('input', '무엇을 주나'), ('output', '무엇이 나오나')]) + '</dl>'
    tree, details, seen_folders = [], [], set()
    for i, item in enumerate(analysis['files']):
        identifier = file_id(analysis, item['path'])
        relative = item['path'].removeprefix('source/')
        try:
            text = read_text(local_path(run['root'], item['path']))
        except ValidationError:
            text = None
        reads = {arm: body_read_lines(run['arms'][arm]['events'], text) if text and text.strip() else [] for arm in ('baseline', 'treatment')}
        label = 'B 열람 확인' if reads['treatment'] else 'B 열람 기록 없음'
        depth = len(Path(relative).parts) - 1
        for level in range(1, depth + 1):
            folder = '/'.join(Path(relative).parts[:level])
            if folder not in seen_folders:
                tree.append(f'<p class="folder-label" style="--depth:{level - 1}">{e(folder)}/</p>')
                seen_folders.add(folder)
        tree.append(f'<button type="button" class="file-button" data-file-target="{identifier}" aria-controls="{identifier}" aria-pressed="{str(i == 0).lower()}" style="--depth:{depth}"><span class="file-name">{e(Path(relative).name)}</span><span class="file-role">{e(item["role"])}</span><span class="read-state">{label}</span></button>')
        links = ''.join(f'<a href="#{file_id(analysis, path)}">{e(path.removeprefix("source/"))}</a>' for path in item['links'])
        lines = ''.join(f'<span class="source-code-line" id="{identifier}-L{number}"><span class="line-number">{number}</span>{e(line)}\n</span>' for number, line in enumerate((text or '').splitlines(), 1))
        preview = f'<details class="source-code"><summary>원문 펼치기 · {len(text.splitlines())}행</summary><pre><code>{lines}</code></pre></details>' if text is not None else '<p class="section-note">바이너리 파일 · 텍스트 미리보기 없음</p>'
        read_links = ''.join(f'<p>{"A" if arm == "baseline" else "B"} · ' + (', '.join(f'<a href="#event-{arm}-L{line}">로그 L{line}</a>' for line in values) if values else '기록된 도구 출력에서 원문 열람 확인 없음') + '</p>' for arm, values in reads.items())
        details.append(f'<article class="file-detail" id="{identifier}"{" hidden" if i else ""}><h3>{e(relative)}</h3><p class="file-description">{e(item["role"])}</p><dl class="skill-facts"><div><dt>쓰이는 시점</dt><dd>{e(item["when"])}</dd></div></dl>' + (f'<h4>연결된 파일</h4><div class="source-refs">{links}</div>' if links else '') + f'<div class="file-observed"><h4>이번 실행의 열람 기록</h4>{read_links}</div>{preview}</article>')
    return '<section><h2>구조와 파일 역할</h2><p class="section-note">' + e(analysis['inventory']['scope']) + '</p>' + intro + '<div class="file-layout"><nav class="file-tree" aria-label="스킬 파일 선택"><p class="tree-root">' + e(run['plan']['source']['name']) + '/</p>' + ''.join(tree) + '</nav><div class="file-inspector">' + ''.join(details) + '</div></div><details><summary>분석 범위와 확인 근거</summary><p>' + e(analysis['inventory']['basis']) + '</p><ul>' + ''.join(f'<li>{e(note)}</li>' for note in analysis['scope_notes']) + '</ul></details></section>'


def flow_panel(run, analysis):
    buttons, details = [], []
    for i, item in enumerate(analysis['flow']):
        identifier = 'skill-flow-' + item['id']
        buttons.append(f'<button type="button" class="flow-button" data-flow-target="{identifier}" aria-controls="{identifier}" aria-pressed="{str(i == 0).lower()}"><strong>{e(item["label"])}</strong><span>{e(item["condition"])}</span></button>')
        next_links = ''.join(f'<a href="#skill-flow-{target}">{e(next(step["label"] for step in analysis["flow"] if step["id"] == target))}</a>' for target in item['next'])
        stages = '<ol class="procedure-list">' + ''.join(f'<li>{e(line)}</li>' for line in item['instruction'].splitlines() if line.strip()) + '</ol>'
        details.append(f'<article class="flow-detail" id="{identifier}"{" hidden" if i else ""}><h3>{e(item["label"])}</h3><div class="procedure-columns"><section><h4>원문이 지시하는 절차</h4>{stages}{source_refs(analysis, item["source"])}</section><section><h4>이번 실행에서 확인한 것</h4><p>{e(item["observation_note"])}</p>{observations(item["observations"])}</section></div>' + (f'<div class="flow-next"><h4>요청에 따른 다음 분기</h4><div class="source-refs">{next_links}</div></div>' if next_links else '') + '</article>')
    return '<section><h2>작동 흐름과 분기</h2><p class="section-note">문서에서 정리한 작업 흐름입니다. 실제 이벤트 순서는 행동 비교에서 확인하세요.</p><div class="flow-map" aria-label="작업 흐름 선택">' + ''.join(buttons) + '</div><div class="flow-inspector">' + ''.join(details) + '</div></section>'


def tools_panel(run, analysis):
    body = ['<section><h2>도구와 의존성</h2><p class="section-note">원문에 언급된 서비스와 이번 실행의 실제 호출을 구분합니다.</p>']
    for kinds, title in [({'service', 'dependency'}, '스킬이 활용하도록 안내하는 것'), ({'runtime'}, '이번 실행에 기록된 도구')]:
        items = [item for item in analysis['tools'] if item['kind'] in kinds]
        if not items:
            continue
        body.append(f'<h3 class="tools-heading">{title}</h3><div class="tool-rows">')
        for item in items:
            body.append(f'<article class="tool-row"><div><h4>{e(item["name"])}</h4><p class="tool-availability">{e(item["availability"])}</p></div><div><p>{e(item["purpose"])}</p>{source_refs(analysis, item["source"])}</div><div><p>{e(item["observation_note"])}</p>{observations(item["observations"])}</div></article>')
        body.append('</div>')
    return ''.join(body) + '</section>'
