"""Small complete run used to exercise preservation and evidence checks."""
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'skill-ab-test' / 'scripts'
sys.path.insert(0, str(SCRIPTS))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def experiment():
    return {
        'schema_version': 1, 'run_id': 'fixture', 'created_at': '2026-11-17T06:00:00Z',
        'source': {'title': 'Fixture skill', 'url': 'https://github.com/example/skill/blob/main/SKILL.md',
                   'commit': None, 'retrieved_at': '2026-11-17T06:00:00Z',
                   'files': [{'path': 'source/SKILL.md', 'role': 'skill',
                              'url': 'https://github.com/example/skill/blob/main/SKILL.md'}]},
        'scope': {'evaluated': '텍스트 지침', 'excluded': ['설치'], 'knowledge_effect': 'instructions'},
        'conditions': {'model_requested': None, 'reasoning_requested': None,
                       'baseline_exposure': 'unknown', 'language': 'ko', 'constraints': ['짧게 답변']},
        'cases': [{'id': f'Q{i}', 'kind': k, 'prompt': f'공통 요청 {i}', 'input': '주어진 수치는 3이다.',
                   'why': 'WHY_SECRET', 'criteria': [{'id': 'C1', 'label': '사실 보존',
                   'rule': 'RULE_SECRET', 'verification': 'input'}]}
                  for i, k in enumerate(['representative', 'constraints', 'insufficient_information'], 1)]}


def execution():
    return {'arms': {arm: {'agent_id': f'/root/{arm}', 'status': 'completed',
            'started_at': None, 'finished_at': None, 'error': None,
            'observations': [], 'usage': {'input_tokens': None, 'output_tokens': None}}
            for arm in ['baseline', 'treatment']},
            'application': {'status': 'provided', 'note': '고정 원문 제공'},
            'integrity': {'status': 'limited_visibility', 'notes': ['전역 주입 미확인']}}


def result():
    return {'schema_version': 1, 'evaluated_by': {'role': 'main', 'agent_id': '/root', 'blind': False},
            'summary': '이번 3문항에서는 사실 보존 차이가 없다.',
            'case_summaries': {f'Q{i}': '메인이 확인한 결과' for i in range(1, 4)},
            'comparisons': [{'case_id': f'Q{i}', 'criterion_id': 'C1', 'verdict': 'unchanged',
                'a': {'kind': 'quote', 'start': 0, 'end': 5, 'text': '수치는 3'},
                'b': {'kind': 'quote', 'start': 0, 'end': 5, 'text': '수치는 3'},
                'observed': '두 답변 모두 수치 보존', 'rationale': '공통 자료와 일치',
                'verification': {'kind': 'input', 'detail': '주어진 수치 3과 대조', 'sources': []},
                'skill_links': [{'file': 'source/SKILL.md', 'start_line': 1, 'end_line': 2}]}
                for i in range(1, 4)], 'redactions': []}


def make_unsealed_run(root):
    root = Path(root)
    (root / 'source').mkdir(parents=True, exist_ok=True)
    (root / 'source/SKILL.md').write_text('대상 전용 원문 TARGET_ONLY\n수치를 보존한다.\n', encoding='utf-8')
    write_json(root / 'experiment.json', experiment())
    return root


def make_run(root):
    from report_lib import prepare_run, capture_answers
    root = make_unsealed_run(root)
    prepare_run(root)
    answers = {'answers': [{'case_id': f'Q{i}', 'text': '수치는 3이다. <script>alert(1)</script>\n두 번째 줄.'} for i in range(1, 4)]}
    for arm in ['baseline', 'treatment']:
        write_json(root / arm / 'answers.json', answers)
    write_json(root / 'execution.json', execution())
    capture_answers(root)
    write_json(root / 'result.json', result())
    return root
