import copy
import json
import tempfile
import unittest
from pathlib import Path
from fixture_factory import make_run, make_unsealed_run, write_json, execution, result
from report_lib import ValidationError, prepare_run, capture_answers, load_run


class ReportValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'run'

    def tearDown(self):
        self.temp.cleanup()

    def edit(self, filename, fn):
        p = self.root / filename
        data = json.loads(p.read_text())
        fn(data)
        write_json(p, data)

    def test_prepare_excludes_rubric_and_target_from_baseline(self):
        make_unsealed_run(self.root)
        prepare_run(self.root)
        a = (self.root / 'prompts/baseline.txt').read_text()
        b = (self.root / 'prompts/treatment.txt').read_text()
        self.assertNotIn('TARGET_ONLY', a)
        self.assertIn('TARGET_ONLY', b)
        for text in [a, b]:
            self.assertNotIn('RULE_SECRET', text)
            self.assertNotIn('WHY_SECRET', text)
            self.assertIn('공통 요청 3', text)
            self.assertIn('주어진 수치는 3이다.', text)

    def test_prepare_refuses_overwrite(self):
        make_unsealed_run(self.root)
        prepare_run(self.root)
        with self.assertRaises(ValidationError):
            prepare_run(self.root)

    def test_case_kind_must_match_question_before_sealing(self):
        for kind in ['representive', 'constraints']:
            with self.subTest(kind=kind):
                root = self.root / kind
                make_unsealed_run(root)
                data = json.loads((root / 'experiment.json').read_text())
                data['cases'][0]['kind'] = kind
                write_json(root / 'experiment.json', data)
                with self.assertRaises(ValidationError):
                    prepare_run(root)
                self.assertFalse((root / 'lock.json').exists())
                self.assertFalse((root / 'prompts/baseline.txt').exists())

    def test_unverified_criterion_requires_main_to_leave_undetermined(self):
        make_run(self.root)
        for verdict in ['improved', 'worsened', 'unchanged']:
            with self.subTest(verdict=verdict):
                res = result()
                res['comparisons'][0]['verification']['kind'] = 'unverified'
                res['comparisons'][0]['verdict'] = verdict
                write_json(self.root / 'result.json', res)
                with self.assertRaises(ValidationError):
                    load_run(self.root)
        res['comparisons'][0]['verdict'] = 'undetermined'
        write_json(self.root / 'result.json', res)
        self.assertEqual(load_run(self.root)['result']['comparisons'][0]['verdict'], 'undetermined')

    def test_partial_blank_answer_preserves_other_answers_and_raw_file(self):
        make_unsealed_run(self.root)
        prepare_run(self.root)
        for arm in ['baseline', 'treatment']:
            write_json(self.root / arm / 'answers.json', {'answers': [
                {'case_id': f'Q{i}', 'text': '  \n' if arm == 'baseline' and i == 3 else '수치는 3이다.'}
                for i in range(1, 4)]})
        ex = execution()
        ex['arms']['baseline']['status'] = 'partial'
        write_json(self.root / 'execution.json', ex)
        original = (self.root / 'baseline/answers.json').read_bytes()
        capture_answers(self.root)
        res = result()
        res['comparisons'][2].update(a={'kind': 'unavailable', 'reason': '빈 답변'}, verdict='undetermined')
        write_json(self.root / 'result.json', res)
        data = load_run(self.root)
        self.assertEqual(set(data['answers']['baseline']), {'Q1', 'Q2'})
        self.assertEqual(data['result']['comparisons'][0]['verdict'], 'unchanged')
        self.assertEqual((self.root / 'baseline/answers.json').read_bytes(), original)

    def test_invalid_unicode_rejected_without_touching_existing_report(self):
        make_run(self.root)
        target = self.root / 'report.html'
        target.write_text('기존 보고서', encoding='utf-8')
        res = result()
        res['summary'] = '\ud800'
        (self.root / 'result.json').write_text(json.dumps(res, ensure_ascii=True), encoding='utf-8')
        with self.assertRaises(ValidationError):
            load_run(self.root)
        self.assertEqual(target.read_text(encoding='utf-8'), '기존 보고서')

    def test_path_escape_rejected(self):
        make_unsealed_run(self.root)
        self.edit('experiment.json', lambda x: x['source']['files'][0].update(path='../secret'))
        with self.assertRaises(ValidationError):
            prepare_run(self.root)

    def test_source_symlink_escape_rejected(self):
        make_unsealed_run(self.root)
        secret = self.root.parent / 'outside'
        secret.write_text('outside')
        (self.root / 'source/SKILL.md').unlink()
        (self.root / 'source/SKILL.md').symlink_to(secret)
        with self.assertRaises(ValidationError):
            prepare_run(self.root)

    def test_github_blob_hash_catches_source_copy_changes(self):
        make_unsealed_run(self.root)
        self.edit('experiment.json', lambda x: x['source']['files'][0].update(blob_sha='0' * 40))
        with self.assertRaises(ValidationError):
            prepare_run(self.root)

    def test_valid_run_preserves_answer_bytes(self):
        make_run(self.root)
        before = (self.root / 'baseline/answers.json').read_bytes()
        data = load_run(self.root)
        self.assertIn('<script>', data['answers']['baseline']['Q1'])
        self.assertEqual(before, (self.root / 'baseline/answers.json').read_bytes())

    def test_presentation_highlights_must_reference_real_unique_comparisons(self):
        make_run(self.root)
        brief = {
            'what': '스킬의 목적', 'input': '입력', 'process': '처리',
            'output': '결과', 'takeaway': '이번 실행의 결론',
            'case_labels': {'Q1': '대표 과제', 'Q2': '조건 과제', 'Q3': '정보 부족'},
            'highlights': [{'case_id': 'Q1', 'criterion_id': 'C1', 'title': '변화', 'detail': '관찰 설명'}],
        }
        self.edit('result.json', lambda x: x.update(presentation=brief))
        self.assertEqual(load_run(self.root)['result']['presentation'], brief)
        for mutate in [
            lambda x: x['highlights'][0].update(criterion_id='absent'),
            lambda x: x['highlights'].append(copy.deepcopy(x['highlights'][0])),
            lambda x: x.update(what='너무 긴 문장' * 100),
            lambda x: x['case_labels'].pop('Q3'),
        ]:
            value = copy.deepcopy(brief)
            mutate(value)
            self.edit('result.json', lambda x: x.update(presentation=value))
            with self.assertRaises(ValidationError):
                load_run(self.root)

    def test_sealed_source_tamper_rejected(self):
        make_run(self.root)
        (self.root / 'source/SKILL.md').write_text('changed')
        with self.assertRaises(ValidationError):
            load_run(self.root)

    def test_captured_answer_tamper_rejected(self):
        make_run(self.root)
        self.edit('baseline/answers.json', lambda x: x['answers'][0].update(text='수정'))
        with self.assertRaises(ValidationError):
            load_run(self.root)

    def test_main_only_evaluator(self):
        make_run(self.root)
        self.edit('result.json', lambda x: x['evaluated_by'].update(role='subagent'))
        with self.assertRaises(ValidationError):
            load_run(self.root)

    def test_worker_cannot_be_declared_main_evaluator(self):
        make_run(self.root)
        self.edit('result.json', lambda x: x['evaluated_by'].update(agent_id='/root/baseline'))
        with self.assertRaises(ValidationError):
            load_run(self.root)

    def test_fake_quote_rejected(self):
        make_run(self.root)
        self.edit('result.json', lambda x: x['comparisons'][0]['a'].update(text='없는 발췌'))
        with self.assertRaises(ValidationError):
            load_run(self.root)

    def test_missing_planned_criterion_rejected(self):
        make_run(self.root)
        self.edit('result.json', lambda x: x['comparisons'].pop())
        with self.assertRaises(ValidationError):
            load_run(self.root)

    def test_absence_requires_whole_answer_scope(self):
        make_run(self.root)
        self.edit('result.json', lambda x: x['comparisons'][0].update(a={'kind': 'absence', 'scope': 'first_line', 'note': '없음'}))
        with self.assertRaises(ValidationError):
            load_run(self.root)

    def test_unavailable_rejected_when_answer_exists(self):
        make_run(self.root)
        self.edit('result.json', lambda x: x['comparisons'][0].update(a={'kind': 'unavailable', 'reason': '실패'}, verdict='undetermined'))
        with self.assertRaises(ValidationError):
            load_run(self.root)

    def test_partial_missing_answer_remains_undetermined(self):
        make_unsealed_run(self.root)
        prepare_run(self.root)
        for arm in ['baseline', 'treatment']:
            ids = [1, 2] if arm == 'baseline' else [1, 2, 3]
            write_json(self.root / arm / 'answers.json', {'answers': [{'case_id': f'Q{i}', 'text': '수치는 3이다.'} for i in ids]})
        ex = execution()
        ex['arms']['baseline']['status'] = 'partial'
        write_json(self.root / 'execution.json', ex)
        capture_answers(self.root)
        res = result()
        res['comparisons'][2].update(a={'kind': 'unavailable', 'reason': '답변 누락'}, verdict='undetermined')
        write_json(self.root / 'result.json', res)
        self.assertNotIn('Q3', load_run(self.root)['answers']['baseline'])

    def test_contaminated_run_must_not_claim_improvement(self):
        make_run(self.root)
        (self.root / 'answers-lock.json').unlink()
        self.edit('execution.json', lambda x: x['integrity'].update(status='contaminated'))
        capture_answers(self.root)
        with self.assertRaises(ValidationError):
            load_run(self.root)

    def test_redaction_range_and_source_line_bounds(self):
        make_run(self.root)
        self.edit('result.json', lambda x: x['redactions'].append({'arm':'baseline','case_id':'Q1','start':0,'end':9999,'reason':'인증정보'}))
        with self.assertRaises(ValidationError):
            load_run(self.root)
        write_json(self.root / 'result.json', result())
        self.edit('result.json', lambda x: x['comparisons'][0]['skill_links'][0].update(end_line=999))
        with self.assertRaises(ValidationError):
            load_run(self.root)


if __name__ == '__main__':
    unittest.main()
