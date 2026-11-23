"""Evidence checks for actual-event comparison reports."""
import base64
import copy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skill-ab-test/scripts'))
import trace_report as renderer


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def write_jsonl(path, values):
    path.write_text(''.join(json.dumps(value, ensure_ascii=False) + '\n' for value in values), encoding='utf-8')


def make_run(root):
    root.mkdir()
    (root / 'source').mkdir()
    (root / 'source/SKILL.md').write_text('---\nname: sample\n---\n수치를 보존합니다.\n', encoding='utf-8')
    target = str(root / 'workspace/.agents/skills/sample/SKILL.md')
    plan = {'schema_version': 2, 'title': '실제 실행 비교', 'source': {'name': 'sample', 'url': 'https://github.com/example/skill', 'commit': None, 'entrypoint': 'source/SKILL.md'}, 'model': 'model', 'reasoning_effort': 'medium', 'cases': [{'id': f'Q{i}', 'title': f'질문 {i}', 'prompt': '수치를 알려줘', 'input': '정답은 3이다.', 'criteria': [{'id': 'C1', 'label': '수치 보존', 'rule': '3을 보존한다.'}]} for i in range(1, 4)]}
    write_json(root / 'trace-plan.json', plan)
    record = {'schema_version': 2, 'created_at': '2026-11-17T06:00:00Z', 'cli_version': 'codex-cli fixture', 'control': {'method': 'per_process_skills_config', 'filesystem_isolation': False, 'global_config_unchanged': True, 'limitations': ['단일 실행']}, 'inputs_sha256': {name: renderer.digest(root / name) for name in ['trace-plan.json', 'source/SKILL.md']}, 'arms': {}}
    for name in renderer.ARMS:
        folder = root / 'arms' / name
        folder.mkdir(parents=True)
        answer = '수치는 3입니다. <script>literal</script>\n'
        events = [
            {'type': 'thread.started', 'thread_id': name},
            {'type': 'turn.started'},
            {'type': 'item.completed', 'item': {'id': 'r1', 'type': 'reasoning', 'text': 'DO_NOT_DISPLAY_OR_QUOTE'}},
            {'type': 'item.started', 'item': {'id': 'c1', 'type': 'command_execution', 'command': 'cat input.txt', 'status': 'in_progress'}},
            {'type': 'item.completed', 'item': {'id': 'c1', 'type': 'command_execution', 'command': 'cat input.txt', 'aggregated_output': '정답은 3이다.', 'exit_code': 0, 'status': 'completed'}},
            {'type': 'item.completed', 'item': {'id': 'a1', 'type': 'agent_message', 'text': answer.rstrip('\n')}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 20}},
        ]
        if name == 'treatment':
            events[4]['item']['aggregated_output'] += '\n' + (root / 'source/SKILL.md').read_text()
        write_jsonl(folder / 'events.jsonl', events)
        write_jsonl(folder / 'receipts.jsonl', [{'line': number, 'received_at': '2026-11-17T06:00:00Z'} for number in range(1, len(events) + 1)])
        write_json(folder / 'inventory.json', {'skills': [{'name': 'sample', 'path': target, 'enabled': name == 'treatment'}], 'errors': []})
        (folder / 'answer.txt').write_text(answer, encoding='utf-8')
        (folder / 'stderr.txt').write_text('', encoding='utf-8')
        (folder / 'prompt.txt').write_text('수치를 알려줘', encoding='utf-8')
        files = {key: f'arms/{name}/{filename}' for key, filename in [('events', 'events.jsonl'), ('receipts', 'receipts.jsonl'), ('inventory', 'inventory.json'), ('answer', 'answer.txt'), ('stderr', 'stderr.txt'), ('prompt', 'prompt.txt')]}
        record['arms'][name] = {'status': 'completed', 'exit_code': 0, 'started_at': '2026-11-17T06:00:00Z', 'finished_at': '2026-11-17T06:01:00Z', 'model': 'model', 'reasoning_effort': 'medium', 'command': ['codex', 'exec', '--json'], 'cwd': str(root / 'workspace'), 'files': files, 'sha256': {key: renderer.digest(root / relative) for key, relative in files.items()}, 'skill_state': {'target_enabled': name == 'treatment', 'enabled_skills': int(name == 'treatment'), 'target_path': target}, 'preflight_verified': True}
    write_json(root / 'trace-run.json', record)
    review = {'schema_version': 2, 'evaluated_by': {'role': 'main', 'agent_id': '/root'}, 'what': '텍스트에서 수치를 보존하는 스킬입니다.', 'takeaway': '두 실행에서 같은 수치를 반환했습니다.', 'workflow_summary': {'baseline': '입력 열람 후 응답했습니다.', 'treatment': '입력 열람 후 응답했습니다.'}, 'differences': [{'title': '입력 열람', 'verdict': 'unchanged', 'observed': '두 조건 모두 입력 파일을 읽었습니다.', 'reason': '동일 명령과 출력을 확인했습니다.', 'a': {'kind': 'event', 'line': 5, 'quote': '정답은 3이다.'}, 'b': {'kind': 'event', 'line': 5, 'quote': 'cat input.txt'}}], 'limits': ['내부 과정은 관찰하지 않았습니다.'], 'integrity': {'validity': 'verified', 'notes': ['기록 범위에서 조건을 확인했습니다.']}}
    write_json(root / 'trace-review.json', review)
    return root


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tags, self.text, self.scripts = [], [], []
        self.active_script = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == 'script':
            self.active_script = ''

    def handle_endtag(self, tag):
        if tag == 'script':
            self.scripts.append(self.active_script)
            self.active_script = None

    def handle_data(self, value):
        self.text.append(value)
        if self.active_script is not None:
            self.active_script += value


class TraceReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = make_run(Path(self.temp.name) / 'run')

    def read(self, relative):
        return json.loads((self.root / relative).read_text())

    def update_review(self, mutate):
        review = self.read('trace-review.json')
        mutate(review)
        write_json(self.root / 'trace-review.json', review)

    def reseal(self, arm, key):
        record = self.read('trace-run.json')
        state = record['arms'][arm]
        state['sha256'][key] = renderer.digest(self.root / state['files'][key])
        write_json(self.root / 'trace-run.json', record)

    def test_actual_events_answer_and_main_judgment_are_rendered(self):
        output = renderer.render_trace_report(self.root)
        html = output.read_text()
        self.assertEqual(output.name, 'trace-report.html')
        self.assertIn('cat input.txt', html)
        self.assertIn('정답은 3이다.', html)
        self.assertIn('수치는 3입니다.', html)
        self.assertIn('대상 스킬 OFF', html)
        self.assertIn('대상 스킬 ON', html)
        self.assertIn('차이 없음', html)
        self.assertIn('메인 에이전트가 작성', html)
        self.assertIn('수신 2026-11-17T06:00:00Z', html)
        self.assertNotIn('DO_NOT_DISPLAY_OR_QUOTE', html)

    def test_all_local_evidence_links_have_targets_and_script_hash_matches(self):
        document = Document(renderer.render_trace_report(self.root).read_text())
        ids = [attrs['id'] for _, attrs in document.tags if 'id' in attrs]
        self.assertEqual(len(ids), len(set(ids)))
        for _, attrs in document.tags:
            if attrs.get('href', '').startswith('#'):
                self.assertIn(attrs['href'][1:], ids)
        csp = next(attrs['content'] for tag, attrs in document.tags if tag == 'meta' and attrs.get('http-equiv') == 'Content-Security-Policy')
        for script in document.scripts:
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            self.assertIn(f"'sha256-{digest}'", csp)

    def test_untrusted_html_is_escaped_with_no_external_resources(self):
        self.update_review(lambda review: review.update(what='<img src="https://evil.test/a" onerror="go()">'))
        html = renderer.render_trace_report(self.root).read_text()
        document = Document(html)
        self.assertIn('&lt;script&gt;literal&lt;/script&gt;', html)
        for tag, attrs in document.tags:
            self.assertNotIn(tag, {'img', 'iframe', 'link', 'object', 'embed'})
            self.assertFalse(any(key.startswith('on') for key in attrs))
            if tag == 'script':
                self.assertNotIn('src', attrs)

    def test_template_tokens_in_evidence_are_not_replaced(self):
        self.update_review(lambda review: review.update(what='리터럴 {{SCRIPT}} {{BODY}} 보존'))
        html = renderer.render_trace_report(self.root).read_text()
        self.assertIn('리터럴 {{SCRIPT}} {{BODY}} 보존', html)

    def test_metrics_are_derived_from_completed_records_and_missing_is_not_zero(self):
        run = renderer.load_trace_run(self.root)
        a, b = (run['arms'][name]['metrics'] for name in renderer.ARMS)
        self.assertEqual(a['completed_tools'], 1)
        self.assertEqual(a['tool_lines'], [5])
        self.assertEqual(a['source_reads'], [])
        self.assertEqual(b['source_reads'], [{'path': 'source/SKILL.md', 'lines': [5]}])
        self.assertEqual(a['elapsed_seconds'], 60)
        self.assertEqual(a['usage'], {'input_tokens': 100, 'cached_input_tokens': None, 'output_tokens': 20})
        self.assertEqual(a['usage_lines'], [7])
        html = renderer.build_html(run)
        self.assertNotIn('<th scope="row">그중 캐시 입력</th>', html)
        self.assertNotIn('Quota', html)
        self.assertNotIn('TCO', html)
        self.assertNotIn('이번 비교를 이해하는 네 개념', html)
        self.assertIn('<th scope="row">입력 토큰 · 캐시 포함</th>', html)
        self.assertNotIn('total_tokens', html)

    def test_measured_zero_usage_is_kept_and_partial_usage_row_is_omitted(self):
        labels = {'input_tokens': '입력 토큰 · 캐시 포함', 'cached_input_tokens': '그중 캐시 입력', 'output_tokens': '출력 토큰'}
        source = {'source/SKILL.md': (self.root / 'source/SKILL.md').read_text()}
        for key, label in labels.items():
            with self.subTest(field=key):
                run = renderer.load_trace_run(self.root)
                for arm in run['arms'].values():
                    arm['events'][6]['event']['usage'][key] = 0
                    arm['metrics'] = renderer.observed_metrics(arm, source)
                html = renderer._metrics(run)
                row = html.split(f'<th scope="row">{label}</th>', 1)[1].split('</tr>', 1)[0]
                self.assertEqual(row.count('<td>0 <span'), 2)
                self.assertIn('<td>같음</td>', row)
                # A recorded zero is not enough to compare with B's absent field.
                arm = run['arms']['treatment']
                arm['events'][6]['event']['usage'].pop(key)
                arm['metrics'] = renderer.observed_metrics(arm, source)
                self.assertIsNone(arm['metrics']['usage'][key])
                self.assertNotIn(f'<th scope="row">{label}</th>', renderer._metrics(run))

    def test_usage_missing_from_one_completed_turn_is_not_a_partial_total(self):
        run = renderer.load_trace_run(self.root)
        for arm in run['arms'].values():
            arm['events'][6]['event']['usage']['cached_input_tokens'] = 0
        run['arms']['treatment']['events'].append({'line': 8, 'event': {'type': 'turn.completed', 'usage': {'input_tokens': 50, 'output_tokens': 5}}})
        for arm in run['arms'].values():
            arm['metrics'] = renderer.observed_metrics(arm, {})
        self.assertEqual(run['arms']['baseline']['metrics']['usage']['cached_input_tokens'], 0)
        self.assertIsNone(run['arms']['treatment']['metrics']['usage']['cached_input_tokens'])
        html = renderer._metrics(run)
        self.assertNotIn('<th scope="row">그중 캐시 입력</th>', html)
        self.assertIn('<th scope="row">입력 토큰 · 캐시 포함</th>', html)
        self.assertIn('href="#event-treatment-L8"', html)

    def test_metric_evidence_links_cover_events_files_and_process_records(self):
        run = renderer.load_trace_run(self.root)
        metrics_html = renderer._metrics(run)
        def row(label):
            return metrics_html.split(f'<th scope="row">{label}</th>', 1)[1].split('</tr>', 1)[0]
        self.assertIn('href="#event-baseline-L5"', row('완료 도구 호출'))
        self.assertIn('href="#timeline-baseline"', row('원문 확인 지침 파일'))
        self.assertIn('href="#event-treatment-L5"', row('원문 확인 지침 파일'))
        self.assertIn('href="#event-baseline-L7"', row('입력 토큰 · 캐시 포함'))
        for name in renderer.ARMS:
            self.assertIn(f'href="#process-{name}"', row('프로세스 경과 시간'))
            context = renderer._context(run)
            self.assertIn(f'id="process-{name}"', context)
            self.assertIn(f'trace-run.json · arms.{name}', context)
            self.assertIn(renderer._e(run['arms'][name]['state']['started_at']), context)
            self.assertIn(renderer._e(run['arms'][name]['state']['finished_at']), context)

    def test_metrics_keep_cache_separate_and_deduplicate_completed_tool_items(self):
        arm = copy.deepcopy(renderer.load_trace_run(self.root)['arms']['baseline'])
        arm['events'][6]['event']['usage']['cached_input_tokens'] = 40
        arm['events'].append({'line': 8, 'event': copy.deepcopy(arm['events'][4]['event'])})
        arm['events'].append({'line': 9, 'event': {'type': 'turn.completed', 'usage': {'input_tokens': 50, 'cached_input_tokens': 10, 'output_tokens': 5, 'reasoning_output_tokens': 3}}})
        metrics = renderer.observed_metrics(arm, {})
        self.assertEqual(metrics['completed_tools'], 1)
        self.assertEqual(metrics['usage'], {'input_tokens': 150, 'cached_input_tokens': 50, 'output_tokens': 25})
        arm['events'][-1]['event']['usage'].pop('cached_input_tokens')
        self.assertIsNone(renderer.observed_metrics(arm, {})['usage']['cached_input_tokens'])

    def test_metrics_reject_invalid_usage_and_backwards_process_time(self):
        arm = copy.deepcopy(renderer.load_trace_run(self.root)['arms']['baseline'])
        for value in (-1, True, 1.5, '100'):
            with self.subTest(value=value):
                arm['events'][6]['event']['usage']['input_tokens'] = value
                with self.assertRaisesRegex(renderer.ValidationError, 'nonnegative integer'):
                    renderer.observed_metrics(arm, {})
        arm['state']['finished_at'] = '2026-11-17T05:59:59Z'
        with self.assertRaisesRegex(renderer.ValidationError, 'finish precedes start'):
            renderer.observed_metrics(arm, {})

    def test_conditions_compare_recorded_questions_and_permissions(self):
        run = renderer.load_trace_run(self.root)
        common = '## Q1\n질문 하나\n## Q2\n질문 둘\n## Q3\n질문 셋'
        for name in renderer.ARMS:
            run['arms'][name]['prompt'] = ('B 로딩 지시\n' if name == 'treatment' else '') + common
            run['arms'][name]['state']['command'] = ['codex', 'exec', '-c', 'default_permissions="sample"', '-c', 'permissions={sample={extends=":read-only",filesystem={"/경로"="deny"}}}']
        html = renderer._condition_table(run)
        self.assertIn('<th scope="row">질문·입력</th><td>Q1~Q3 동일 전달</td><td>Q1~Q3 동일 전달</td><td>같음</td>', html)
        self.assertIn('<th scope="row">기본 파일 권한</th><td>읽기 전용</td><td>읽기 전용</td><td>같음</td>', html)
        run['arms']['treatment']['prompt'] += '\n추가 질문'
        self.assertNotIn('Q1~Q3 동일 전달', renderer._condition_table(run))
        self.assertIsNone(renderer._read_permission({'command': ['codex', '-c', 'permissions="invalid"']}))
        self.assertIsNone(renderer._read_permission({'command': ['codex', '-c', 'default_permissions="sample"', '-c', 'permissions={sample={extends=":read-only",filesystem={"/tmp"="write"}}}']}))

    def scope(self):
        return {'confirmed': [{'topic': '실제 입력 열람', 'a': '입력 파일 읽기', 'b': '입력과 지침 읽기', 'conclusion': '메인이 정한 결론 <literal>', 'evidence': [{'arm': 'baseline', 'kind': 'event', 'line': 5, 'quote': '정답은 3이다.'}, {'arm': 'treatment', 'kind': 'event', 'line': 5, 'quote': '수치를 보존합니다.'}]}], 'unknown': [{'topic': '반복 안정성', 'missing': '반복 데이터', 'why': '한 번씩 실행', 'next_check': '같은 입력으로 반복'}]}

    def test_scope_is_main_authored_two_columns_with_exact_evidence(self):
        scope = self.scope()
        self.update_review(lambda review: review.update(report_scope=scope))
        html = renderer.render_trace_report(self.root).read_text()
        self.assertIn('확인된 차이', html)
        self.assertIn('아직 판단할 수 없는 것', html)
        self.assertIn('메인이 정한 결론 &lt;literal&gt;', html)
        self.assertIn('href="#event-treatment-L5"', html)
        self.assertIn('반복 데이터', html)
        document = Document(html)
        ids = {attrs['id'] for _, attrs in document.tags if 'id' in attrs}
        self.assertTrue({'conditions', 'scope', 'observations', 'technical-details', 'log-captures'} <= ids)
        self.assertNotIn('concepts', ids)
        self.assertNotIn('limits', ids)

    def test_scope_cannot_replace_missing_arm_or_mismatched_evidence(self):
        for mutate in [lambda s: s['confirmed'][0]['evidence'].pop(), lambda s: s['confirmed'][0]['evidence'][0].update(quote='not in log'), lambda s: s['confirmed'][0]['evidence'][0].update(kind='unavailable', note='missing'), lambda s: s['confirmed'][0]['evidence'][0].update(line=3, quote='DO_NOT_DISPLAY_OR_QUOTE')]:
            scope = self.scope()
            mutate(scope)
            self.update_review(lambda review: review.update(report_scope=scope))
            with self.assertRaises(renderer.ValidationError):
                renderer.load_trace_run(self.root)

    def test_missing_scope_is_not_inferred_from_main_differences(self):
        html = renderer.render_trace_report(self.root).read_text()
        self.assertIn('메인이 확인된 차이와 판단 불가 항목을 아직 정리하지 않았습니다.', html)
        self.assertFalse(any('scope-card' in attrs.get('class', '').split() for _, attrs in Document(html).tags))

    def test_legacy_html_capture_manifest_is_neither_loaded_nor_displayed(self):
        (self.root / 'trace-captures.json').write_text('broken legacy manifest', encoding='utf-8')
        html = renderer.render_trace_report(self.root).read_text()
        self.assertIn('원본 로그 화면 미촬영', html)
        self.assertNotIn('<img ', html)

    def test_native_capture_provenance_is_visible_and_escaped(self):
        captures = [{'title': 'A <원본>', 'data_uri': 'data:image/png;base64,AAAA', 'width': 1280, 'height': 900, 'caption': '원본 JSONL 파일 직접 열람 · 실행 종료 후 촬영 · Codex 앱 화면 아님', 'basis': 'raw_log_browser', 'arm': 'baseline', 'source_file': 'arms/baseline/events.jsonl', 'visible_lines': {'start': 1, 'end': 7}, 'surface': 'Aside <Browser>', 'captured_at': '2026-11-17T07:00:00Z'}]
        captures.append({**captures[0], 'title': 'B 원본', 'arm': 'treatment', 'source_file': 'arms/treatment/events.jsonl', 'height': 1700})
        with patch('native_captures.load_native_captures', return_value=captures) as loader:
            html = renderer.render_trace_report(self.root).read_text()
        loader.assert_called_once_with(self.root.resolve())
        self.assertIn('A &lt;원본&gt;', html)
        self.assertIn('Aside &lt;Browser&gt;', html)
        self.assertIn('Codex 앱 화면 아님', html)
        self.assertIn('arms/baseline/events.jsonl · L1–L7', html)
        self.assertIn('src="data:image/png;base64,AAAA"', html)
        self.assertIn('원본 로그 캡처 2장', html)
        for arm, letter, index in [('baseline', 'A', 1), ('treatment', 'B', 2)]:
            marker = f'<details id="capture-{arm}-{index}"><summary>{letter} 캡처 펼치기</summary>'
            self.assertIn(marker, html)
            self.assertTrue(html.split(marker, 1)[1].startswith('<img '))
            figure = html.split(marker, 1)[0].rsplit('<figure class="trace-capture">', 1)[1]
            self.assertIn(f'arms/{arm}/events.jsonl', figure)
            self.assertTrue(figure.endswith('</figcaption>'))

    def test_rejects_nonmatching_or_reasoning_quotes(self):
        for evidence in [{'kind': 'event', 'line': 5, 'quote': 'invented'}, {'kind': 'event', 'line': 3, 'quote': 'DO_NOT_DISPLAY_OR_QUOTE'}, {'kind': 'answer', 'quote': 'invented'}, {'kind': 'event', 'line': 900, 'quote': 'missing'}]:
            with self.subTest(evidence=evidence):
                self.update_review(lambda review: review['differences'][0].update(a=evidence))
                with self.assertRaises(renderer.ValidationError):
                    renderer.load_trace_run(self.root)

    def test_hash_mismatch_prevents_overwriting_previous_report(self):
        target = renderer.render_trace_report(self.root)
        old = target.read_bytes()
        (self.root / 'arms/baseline/events.jsonl').write_text('{}\n')
        with self.assertRaisesRegex(renderer.ValidationError, 'hash mismatch'):
            renderer.render_trace_report(self.root)
        self.assertEqual(old, target.read_bytes())

    def test_rejects_source_and_plan_changes(self):
        (self.root / 'source/SKILL.md').write_text('replaced')
        with self.assertRaisesRegex(renderer.ValidationError, 'input hash mismatch'):
            renderer.load_trace_run(self.root)

    def test_inventory_off_on_cannot_be_claimed_without_matching_snapshot(self):
        inventory = self.read('arms/baseline/inventory.json')
        inventory['skills'][0]['enabled'] = True
        write_json(self.root / 'arms/baseline/inventory.json', inventory)
        self.reseal('baseline', 'inventory')
        with self.assertRaisesRegex(renderer.ValidationError, 'skill count differs'):
            renderer.load_trace_run(self.root)

    def test_failed_and_unverified_runs_require_undetermined(self):
        record = self.read('trace-run.json')
        record['arms']['baseline']['status'] = 'failed'
        record['arms']['baseline']['exit_code'] = 1
        write_json(self.root / 'trace-run.json', record)
        with self.assertRaisesRegex(renderer.ValidationError, 'requires undetermined'):
            renderer.load_trace_run(self.root)
        self.update_review(lambda review: review['differences'][0].update(verdict='undetermined'))
        self.assertIn('실행 실패', renderer.render_trace_report(self.root).read_text())
        record['arms']['baseline']['status'] = 'completed'
        record['arms']['baseline']['exit_code'] = 0
        record['arms']['baseline']['preflight_verified'] = False
        write_json(self.root / 'trace-run.json', record)
        self.assertIn('사전 검증 미완료', renderer.render_trace_report(self.root).read_text())

    def test_limited_or_missing_main_integrity_requires_undetermined(self):
        self.update_review(lambda review: review['integrity'].update(validity='limited'))
        with self.assertRaisesRegex(renderer.ValidationError, 'requires undetermined'):
            renderer.load_trace_run(self.root)
        self.update_review(lambda review: review['differences'][0].update(verdict='undetermined'))
        html = renderer.render_trace_report(self.root).read_text()
        self.assertIn('비교 조건 관찰 제한', html)
        self.assertIn('조회된 목록: 0개 활성', html)
        self.update_review(lambda review: review.pop('integrity'))
        renderer.load_trace_run(self.root)

    def test_changed_source_during_execution_invalidates_report(self):
        record = self.read('trace-run.json')
        record['control']['source_unchanged'] = False
        write_json(self.root / 'trace-run.json', record)
        with self.assertRaisesRegex(renderer.ValidationError, 'source changed'):
            renderer.load_trace_run(self.root)

    def test_additional_rpc_evidence_is_hash_validated(self):
        record = self.read('trace-run.json')
        path = self.root / 'arms/baseline/inventory-rpc.jsonl'
        path.write_text('{}\n')
        record['arms']['baseline']['files']['inventory_rpc'] = str(path.relative_to(self.root))
        record['arms']['baseline']['sha256']['inventory_rpc'] = renderer.digest(path)
        write_json(self.root / 'trace-run.json', record)
        renderer.load_trace_run(self.root)
        path.write_text('changed')
        with self.assertRaisesRegex(renderer.ValidationError, 'hash mismatch'):
            renderer.load_trace_run(self.root)

    def test_full_source_in_successful_baseline_output_invalidates_verified(self):
        path = self.root / 'arms/baseline/events.jsonl'
        events = [json.loads(line) for line in path.read_text().splitlines()]
        events[4]['item']['aggregated_output'] += '\n' + (self.root / 'source/SKILL.md').read_text()
        write_jsonl(path, events)
        self.reseal('baseline', 'events')
        with self.assertRaisesRegex(renderer.ValidationError, 'baseline contains pinned skill body'):
            renderer.load_trace_run(self.root)
        self.update_review(lambda review: review['integrity'].update(validity='contaminated'))
        self.update_review(lambda review: review['differences'][0].update(verdict='undetermined'))
        renderer.load_trace_run(self.root)

    def test_baseline_body_exposure_rejected_even_in_failed_or_unfinished_tools(self):
        path = self.root / 'arms/baseline/events.jsonl'
        original = [json.loads(line) for line in path.read_text().splitlines()]
        source = (self.root / 'source/SKILL.md').read_text()
        cases = [
            {'type': 'item.completed', 'item': {'type': 'command_execution', 'status': 'failed', 'exit_code': 1, 'command': 'cat SKILL.md missingfile', 'aggregated_output': source + 'missingfile: No such file'}},
            {'type': 'item.updated', 'item': {'type': 'command_execution', 'status': 'in_progress', 'command': 'cat SKILL.md', 'aggregated_output': source}},
            {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'status': 'failed', 'result': {'isError': True, 'content': [{'type': 'text', 'text': source}]}, 'error': 'tool failed'}},
            {'type': 'item.updated', 'item': {'type': 'mcp_tool_call', 'status': 'in_progress', 'result': {'content': [{'type': 'text', 'text': source}]}}},
        ]
        for exposed in cases:
            with self.subTest(event=exposed['type'], item=exposed['item']['type'], status=exposed['item']['status']):
                events = list(original)
                events[3] = exposed
                write_jsonl(path, events)
                self.reseal('baseline', 'events')
                with self.assertRaisesRegex(renderer.ValidationError, 'baseline contains pinned skill body'):
                    renderer.load_trace_run(self.root)

    def test_target_read_claim_requires_successful_full_source_output(self):
        path = self.root / 'arms/treatment/events.jsonl'
        events = [json.loads(line) for line in path.read_text().splitlines()]
        events[4]['item']['exit_code'] = 1
        write_jsonl(path, events)
        self.reseal('treatment', 'events')
        with self.assertRaisesRegex(renderer.ValidationError, 'treatment body read is unverified'):
            renderer.load_trace_run(self.root)

    def test_body_read_uses_mcp_result_not_command_or_failed_result(self):
        source = (self.root / 'source/SKILL.md').read_text()
        events = [
            {'line': 1, 'event': {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'status': 'completed', 'result': {'content': [{'type': 'text', 'text': source}]}}}},
            {'line': 2, 'event': {'type': 'item.completed', 'item': {'type': 'command_execution', 'status': 'completed', 'exit_code': 0, 'command': source, 'aggregated_output': ''}}},
            {'line': 3, 'event': {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'status': 'completed', 'result': {'isError': True, 'content': [{'type': 'text', 'text': source}]}}}},
        ]
        self.assertEqual(renderer.body_read_lines(events, source), [1])

    def test_not_installed_scan_requires_actual_empty_collisions(self):
        record = self.read('trace-run.json')
        evidence = {'status': 'not_installed', 'observed_at': '2026-11-17T06:00:00Z', 'checked_count': 1, 'roots': ['/skills'], 'skills': [{'name': 'sample', 'path': '/skills/sample/SKILL.md', 'sha256': 'f' * 64}], 'collisions': []}
        write_json(self.root / 'eligibility.json', evidence)
        record['inputs_sha256']['eligibility.json'] = renderer.digest(self.root / 'eligibility.json')
        record['control']['eligibility'] = {key: evidence[key] for key in ['status', 'observed_at', 'checked_count', 'roots']}
        write_json(self.root / 'trace-run.json', record)
        with self.assertRaisesRegex(renderer.ValidationError, 'target found in eligibility scan'):
            renderer.load_trace_run(self.root)

    def test_independent_action_selectors_keep_every_observable_event_and_anchor(self):
        html = renderer.render_trace_report(self.root).read_text()
        self.assertIn('완료된 도구 실행 1회', html)
        document = Document(html)
        for name in renderer.ARMS:
            buttons = [attrs for _, attrs in document.tags if attrs.get('class') == 'action-button' and attrs['data-event-target'].startswith(f'event-{name}-')]
            self.assertEqual([button['data-event-target'] for button in buttons], [f'event-{name}-L{i}' for i in (5, 6, 1, 2, 4, 7)])
            self.assertEqual([button['data-event-target'] for button in buttons if button['aria-pressed'] == 'true'], [f'event-{name}-L5'])
            details = [attrs for _, attrs in document.tags if attrs.get('class') == 'action-detail' and attrs['data-event-id'].startswith(f'event-{name}-')]
            self.assertEqual([detail['id'] for detail in details if 'hidden' not in detail], [f'event-{name}-L5'])
            for number in (1, 2, 4, 5, 6, 7):
                self.assertIn(f'id="event-{name}-L{number}"', html)
                self.assertIn(f'id="raw-{name}-L{number}"', html)
        self.assertNotIn('id="event-baseline-L3"', html)

    def test_auxiliary_events_are_folded_but_completion_and_final_answer_remain_visible(self):
        arm = renderer.load_trace_run(self.root)['arms']['baseline']
        html = renderer._timeline('baseline', arm)
        primary, rest = html.split('<details class="aux-events">', 1)
        auxiliary = rest.split('</details>', 1)[0]
        main_buttons = [attrs['data-event-target'] for _, attrs in Document(primary).tags if attrs.get('class') == 'action-button']
        hidden_buttons = [attrs['data-event-target'] for _, attrs in Document(auxiliary).tags if attrs.get('class') == 'action-button']
        self.assertEqual(main_buttons, ['event-baseline-L5', 'event-baseline-L6'])
        self.assertEqual(hidden_buttons, ['event-baseline-L1', 'event-baseline-L2', 'event-baseline-L4', 'event-baseline-L7'])
        self.assertIn('시작·중간 기록 4개', auxiliary)
        self.assertIn('input.txt 읽기', primary)
        self.assertIn('최종 응답', primary)
        self.assertNotIn('item.completed', primary)

    def test_long_output_preview_is_exact_and_command_and_full_text_are_folded(self):
        arm = copy.deepcopy(renderer.load_trace_run(self.root)['arms']['baseline'])
        text = '보존할 원문\n' * 100
        arm['events'][4]['event']['item']['aggregated_output'] = text
        html = renderer._timeline('baseline', arm)
        self.assertIn(renderer._pre(text[:180] + '\n…', class_name='event-preview'), html)
        self.assertIn(renderer._pre(text, class_name='event-output'), html)
        self.assertIn('<details><summary>실제 명령·요청 펼치기</summary><pre class="command">cat input.txt</pre></details>', html)

    def test_dashboard_tabs_and_verdict_controls_reference_real_panels(self):
        run = renderer.load_trace_run(self.root)
        document = Document(renderer.build_html(run))
        controls = [attrs for _, attrs in document.tags if attrs.get('role') == 'tab']
        self.assertEqual([item['data-panel'] for item in controls], ['comparison', 'behavior', 'answers', 'verdicts', 'context', 'captures'])
        self.assertEqual([item['data-panel'] for item in controls if item['aria-selected'] == 'true'], ['comparison'])
        panels = {attrs['id']: attrs for _, attrs in document.tags if attrs.get('role') == 'tabpanel'}
        self.assertEqual([name for name, attrs in panels.items() if 'hidden' not in attrs], ['panel-comparison'])
        for button in controls:
            self.assertEqual(panels[button['aria-controls']]['aria-labelledby'], button['id'])
        verdicts = [attrs for _, attrs in document.tags if attrs.get('class') == 'verdict-button']
        self.assertEqual(verdicts[0]['data-verdict-target'], 'difference-1')
        self.assertEqual(verdicts[0]['aria-pressed'], 'true')
        self.assertIn('차이 없음', ''.join(document.text))

    def test_action_panel_with_zero_tools_selects_actual_message(self):
        arm = copy.deepcopy(renderer.load_trace_run(self.root)['arms']['baseline'])
        arm['events'] = [row for row in arm['events'] if row['event'].get('item', {}).get('type') != 'command_execution']
        html = renderer._timeline('baseline', arm)
        self.assertIn('기록된 도구 호출 0회', html)
        buttons = [attrs for _, attrs in Document(html).tags if attrs.get('class') == 'action-button']
        self.assertEqual([item['data-event-target'] for item in buttons if item['aria-pressed'] == 'true'], ['event-baseline-L6'])
        self.assertNotIn('data-event-target="event-baseline-L5"', html)

    def test_unfinished_tool_is_not_described_as_zero_recorded_calls(self):
        arm = copy.deepcopy(renderer.load_trace_run(self.root)['arms']['baseline'])
        arm['events'] = arm['events'][:4]
        html = renderer._timeline('baseline', arm)
        self.assertIn('완료된 도구 실행 0회', html)
        self.assertIn('도구 관련 이벤트가 있으나 완료 이벤트는 없습니다.', html)
        self.assertNotIn('기록된 도구 호출 0회', html)
        buttons = [attrs for _, attrs in Document(html).tags if attrs.get('class') == 'action-button']
        self.assertEqual([item['data-event-target'] for item in buttons if item['aria-pressed'] == 'true'], ['event-baseline-L4'])

    def test_question_slices_preserve_exact_text_and_require_unambiguous_headings(self):
        answer = '\n## Q1\n원문 첫째\n\n## Q2\n원문 둘째\n\n## Q3\n원문 셋째\n'
        parts = renderer._answer_parts(answer)
        self.assertEqual(''.join(parts.values()), answer)
        self.assertEqual(parts['Q2'], '## Q2\n원문 둘째\n\n')
        for ambiguous in [answer.replace('## Q2', '### Q2'), '서문\n' + answer, answer + '\n## Q1\n중복', '```\n' + answer + '\n```']:
            self.assertIsNone(renderer._answer_parts(ambiguous))
        run = renderer.load_trace_run(self.root)
        run['arms']['baseline']['answer'] = answer
        self.assertNotIn('data-answer-select="Q1"', renderer._answers(run))
        run['arms']['treatment']['answer'] = answer
        html = renderer._answers(run)
        self.assertIn('data-answer-select="Q1"', html)
        self.assertIn(renderer._pre(answer, 'answer-baseline', 'answer'), html)
        self.assertIn(renderer._pre(parts['Q2'], 'answer-treatment-Q2', 'answer'), html)

    def test_dashboard_script_is_the_local_asset_used_for_csp(self):
        self.assertEqual(renderer.SCRIPT, (ROOT / 'skill-ab-test/assets/trace-dashboard.js').read_text())

    def test_target_registration_claim_matches_inventory(self):
        record = self.read('trace-run.json')
        record['arms']['baseline']['skill_state']['target_registered'] = False
        write_json(self.root / 'trace-run.json', record)
        with self.assertRaisesRegex(renderer.ValidationError, 'target registration differs'):
            renderer.load_trace_run(self.root)

    def test_read_probe_records_expected_permission_enforcement(self):
        record = self.read('trace-run.json')
        record['control']['filesystem_read_guard'] = True
        for name, state in record['arms'].items():
            target = state['skill_state']['target_path']
            probe = {'checks': [{'path': target, 'expected_readable': name == 'treatment', 'readable': name == 'treatment', 'errno': None if name == 'treatment' else 1}]}
            state['read_guard'] = {'verified': True, 'target_readable': name == 'treatment', 'checked_paths': 1}
            for key, filename in [('read_probe', 'read-probe.json'), ('read_probe_stdout', 'read-probe-stdout.txt'), ('read_probe_stderr', 'read-probe-stderr.txt')]:
                path = self.root / 'arms' / name / filename
                if key == 'read_probe_stderr':
                    path.write_text('')
                else:
                    write_json(path, probe)
                state['files'][key] = str(path.relative_to(self.root))
                state['sha256'][key] = renderer.digest(path)
        write_json(self.root / 'trace-run.json', record)
        run = renderer.load_trace_run(self.root)
        self.assertFalse(run['arms']['baseline']['read_probe']['checks'][0]['readable'])
        path = self.root / 'arms/baseline/read-probe-stdout.txt'
        changed = json.loads(path.read_text())
        changed['checks'][0]['readable'] = True
        write_json(path, changed)
        self.reseal('baseline', 'read_probe_stdout')
        with self.assertRaisesRegex(renderer.ValidationError, 'differs from actual stdout'):
            renderer.load_trace_run(self.root)

    def test_receipt_must_cover_each_recorded_line(self):
        write_jsonl(self.root / 'arms/baseline/receipts.jsonl', [{'line': 1, 'received_at': '2026-11-17T06:00:00Z'}])
        self.reseal('baseline', 'receipts')
        with self.assertRaisesRegex(renderer.ValidationError, 'coverage mismatch'):
            renderer.load_trace_run(self.root)

    def test_final_answer_must_match_actual_agent_message(self):
        (self.root / 'arms/baseline/answer.txt').write_text('manually rewritten answer')
        self.reseal('baseline', 'answer')
        with self.assertRaisesRegex(renderer.ValidationError, 'differs from emitted message'):
            renderer.load_trace_run(self.root)

    def test_absence_requires_scope_and_main_note(self):
        self.update_review(lambda review: review['differences'][0].update(a={'kind': 'absence', 'scope': 'recorded_events', 'note': '등록된 범위에서 대상 파일 읽기를 발견하지 못했습니다.'}))
        self.assertIn('검사 범위: 기록된 관측 이벤트 전체', renderer.render_trace_report(self.root).read_text())
        self.update_review(lambda review: review['differences'][0]['a'].update(scope='all_internal_behavior'))
        with self.assertRaisesRegex(renderer.ValidationError, 'entire observed scope'):
            renderer.load_trace_run(self.root)

    def test_only_main_agent_can_evaluate(self):
        self.update_review(lambda review: review['evaluated_by'].update(role='response_agent'))
        with self.assertRaisesRegex(renderer.ValidationError, 'only the main'):
            renderer.load_trace_run(self.root)

    def test_trace_path_escape_is_rejected(self):
        record = self.read('trace-run.json')
        record['arms']['baseline']['files']['events'] = '../outside.jsonl'
        write_json(self.root / 'trace-run.json', record)
        with self.assertRaisesRegex(renderer.ValidationError, 'path must stay inside'):
            renderer.load_trace_run(self.root)

    def test_unknown_jsonl_and_symlinks_fail_without_rendering(self):
        (self.root / 'arms/baseline/events.jsonl').write_text('{"type": "a", "type": "b"}\n')
        self.reseal('baseline', 'events')
        with self.assertRaisesRegex(renderer.ValidationError, 'invalid JSONL'):
            renderer.load_trace_run(self.root)


if __name__ == '__main__':
    unittest.main()
