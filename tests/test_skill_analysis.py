"""Integrity checks for source anatomy and its links to real public events."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skill-ab-test/scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
from report_lib import ValidationError, digest
from skill_analysis import load_analysis
from test_trace_report import make_run, write_json
from trace_report import build_html, load_trace_run


def sample_analysis():
    source = {'path': 'source/SKILL.md', 'start': 4, 'end': 4, 'quote': '수치를 보존합니다.'}
    event = {'arm': 'treatment', 'kind': 'event', 'line': 5, 'quote': 'cat input.txt'}
    return {
        'schema_version': 1,
        'analyzed_by': {'role': 'main', 'agent_id': '/root'},
        'summary': {'purpose': '수치 보존', 'activation': '수치 질문', 'input': '원문',
                    'output': '답변', 'mechanism': '입력의 수치를 유지하도록 지시'},
        'inventory': {'scope': '고정한 스킬 폴더', 'basis': '고정 입력 목록'},
        'files': [{'path': 'source/SKILL.md', 'role': '진입 지침', 'when': '적용 시',
                   'kind': 'instruction', 'links': []}],
        'flow': [{'id': 'preserve', 'label': '수치 보존', 'condition': '수치가 포함된 입력',
                  'instruction': '입력의 수치를 보존', 'source': [source],
                  'observations': [event], 'observation_note': '입력 읽기 호출이 기록됨', 'next': []}],
        'tools': [{'name': 'command_execution', 'kind': 'runtime', 'purpose': '파일 읽기',
                   'availability': '실행 환경 제공', 'source': [], 'observations': [copy.deepcopy(event)],
                   'observation_note': '실제 읽기 명령'}],
        'scope_notes': ['한 번의 실행 기록'],
    }


class SkillAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = make_run(Path(self.temp.name) / 'run')
        self.run = load_trace_run(self.root)
        self.analysis = sample_analysis()

    def load(self):
        write_json(self.root / 'skill-analysis.json', self.analysis)
        return load_analysis(self.run)

    def test_optional_analysis_absence_preserves_old_reports(self):
        self.assertIsNone(load_analysis(self.run))

    def test_valid_analysis_is_returned_without_rewriting_or_inference(self):
        self.assertEqual(self.load(), self.analysis)

    def test_main_authorship_required(self):
        self.analysis['analyzed_by']['role'] = 'worker'
        with self.assertRaisesRegex(ValidationError, 'main agent'):
            self.load()

    def test_source_quote_must_be_in_cited_range(self):
        self.analysis['flow'][0]['source'][0]['start'] = 1
        self.analysis['flow'][0]['source'][0]['end'] = 3
        with self.assertRaisesRegex(ValidationError, 'quote differs'):
            self.load()

    def test_tampered_quote_rejected(self):
        self.analysis['flow'][0]['source'][0]['quote'] = '확인되지 않은 지시'
        with self.assertRaisesRegex(ValidationError, 'quote differs'):
            self.load()

    def test_line_ranges_reject_bool_zero_reversal_and_overflow(self):
        for start, end in [(True, 4), (4, True), (0, 4), (4, 3), (4, 5)]:
            with self.subTest(start=start, end=end):
                self.analysis['flow'][0]['source'][0].update(start=start, end=end)
                with self.assertRaisesRegex(ValidationError, 'line range'):
                    self.load()

    def test_changed_source_bytes_rejected_even_with_matching_quote(self):
        with (self.root / 'source/SKILL.md').open('a') as stream:
            stream.write('추가된 지시\n')
        with self.assertRaisesRegex(ValidationError, 'source hash mismatch'):
            self.load()

    def test_missing_file_rejected(self):
        (self.root / 'source/SKILL.md').unlink()
        with self.assertRaisesRegex(ValidationError, 'cannot hash'):
            self.load()

    def test_omitted_pinned_file_rejected(self):
        (self.root / 'source/extra.txt').write_text('추가 원문\n')
        self.run['record']['inputs_sha256']['source/extra.txt'] = digest(self.root / 'source/extra.txt')
        with self.assertRaisesRegex(ValidationError, 'cover every pinned'):
            self.load()

    def test_duplicate_file_rejected(self):
        self.analysis['files'].append(copy.deepcopy(self.analysis['files'][0]))
        with self.assertRaisesRegex(ValidationError, 'duplicate inventory'):
            self.load()

    def test_unpinned_file_and_link_rejected(self):
        for place in ('path', 'links'):
            with self.subTest(place=place):
                self.analysis = sample_analysis()
                self.analysis['files'][0][place] = 'source/unpinned.md' if place == 'path' else ['source/unpinned.md']
                with self.assertRaisesRegex(ValidationError, 'outside pinned'):
                    self.load()

    def test_escaping_and_symlink_paths_rejected(self):
        self.analysis['files'][0]['path'] = '../outside.md'
        with self.assertRaisesRegex(ValidationError, 'inside run directory'):
            self.load()
        self.analysis = sample_analysis()
        target = self.root / 'source/SKILL.md'
        target.unlink()
        target.symlink_to(self.root / 'trace-plan.json')
        with self.assertRaisesRegex(ValidationError, 'symlinks are not allowed'):
            self.load()

    def test_reasoning_event_cannot_be_observed_evidence(self):
        self.analysis['flow'][0]['observations'][0].update(line=3, quote='DO_NOT_DISPLAY_OR_QUOTE')
        with self.assertRaisesRegex(ValidationError, 'observable event'):
            self.load()

    def test_answer_evidence_must_match_the_selected_arm(self):
        self.analysis['flow'][0]['observations'] = [{'arm': 'baseline', 'kind': 'answer', 'quote': '수치는 3입니다.'}]
        self.assertEqual(self.load(), self.analysis)
        self.analysis['flow'][0]['observations'][0]['quote'] = '수치는 4입니다.'
        with self.assertRaisesRegex(ValidationError, 'recorded answer'):
            self.load()

    def test_unknown_graph_target_and_duplicate_step_rejected(self):
        self.analysis['flow'][0]['next'] = ['missing']
        with self.assertRaisesRegex(ValidationError, 'next step does not exist'):
            self.load()
        self.analysis = sample_analysis()
        self.analysis['flow'].append(copy.deepcopy(self.analysis['flow'][0]))
        with self.assertRaisesRegex(ValidationError, 'duplicate step'):
            self.load()

    def test_flow_requires_source_but_runtime_tool_can_have_only_event(self):
        self.assertEqual(self.load()['tools'][0]['source'], [])
        self.analysis['flow'][0]['source'] = []
        with self.assertRaisesRegex(ValidationError, 'source evidence required'):
            self.load()

    def test_tool_without_source_or_observation_rejected(self):
        self.analysis['tools'][0]['observations'] = []
        with self.assertRaisesRegex(ValidationError, 'source or observed evidence'):
            self.load()

    def test_runtime_tool_cannot_rely_on_source_or_answer_only(self):
        for observations in ([], [{'arm': 'treatment', 'kind': 'answer', 'quote': '수치는 3입니다.'}]):
            with self.subTest(observations=observations):
                self.analysis['tools'][0]['source'] = copy.deepcopy(self.analysis['flow'][0]['source'])
                self.analysis['tools'][0]['observations'] = observations
                with self.assertRaisesRegex(ValidationError, 'runtime tool requires an observed tool event'):
                    self.load()

    def test_runtime_tool_rejects_message_and_turn_events(self):
        for line, quote in [(6, '수치는 3입니다.'), (7, '100')]:
            with self.subTest(line=line):
                self.analysis['tools'][0]['observations'] = [
                    {'arm': 'treatment', 'kind': 'event', 'line': line, 'quote': quote}]
                with self.assertRaisesRegex(ValidationError, 'runtime tool requires an observed tool event'):
                    self.load()

    def test_started_and_failed_tool_events_are_valid_observations(self):
        self.analysis['tools'][0]['observations'][0]['line'] = 4
        self.assertEqual(self.load(), self.analysis)
        self.analysis['tools'][0]['observations'][0]['line'] = 5
        self.run['arms']['treatment']['by_line'][5]['event']['item'].update(
            exit_code=1, status='failed', aggregated_output='read denied')
        self.assertEqual(self.load(), self.analysis)

    def test_declared_service_and_dependency_require_source(self):
        for kind in ('service', 'dependency'):
            with self.subTest(kind=kind):
                self.analysis['tools'][0]['kind'] = kind
                self.analysis['tools'][0]['source'] = []
                with self.assertRaisesRegex(ValidationError, 'requires source evidence'):
                    self.load()
                self.analysis['tools'][0]['source'] = copy.deepcopy(self.analysis['flow'][0]['source'])
                self.assertEqual(self.load(), self.analysis)

    def test_whitespace_only_source_file_keeps_report_renderable(self):
        source = self.root / 'source/blank.py'
        source.write_text(' \n\n')
        self.run['record']['inputs_sha256']['source/blank.py'] = digest(source)
        self.analysis['files'].append({'path': 'source/blank.py', 'role': '공백 파일',
                                       'when': '실행 단계 없음', 'kind': 'script', 'links': []})
        self.run['analysis'] = self.load()
        html = build_html(self.run)
        self.assertIn('blank.py', html)
        self.assertIn('id="skill-file-1-L2"', html)

    def test_binary_asset_inventory_does_not_require_utf8(self):
        asset = self.root / 'source/logo.png'
        asset.write_bytes(b'\x89PNG\r\n\x1a\n\xff')
        self.run['record']['inputs_sha256']['source/logo.png'] = digest(asset)
        self.analysis['files'].append({'path': 'source/logo.png', 'role': '로고', 'when': '출력 시',
                                       'kind': 'asset', 'links': []})
        self.assertEqual(self.load(), self.analysis)


if __name__ == '__main__':
    unittest.main()
