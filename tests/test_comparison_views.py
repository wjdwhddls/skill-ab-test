"""Behavioral contracts for paired, evidence-backed workflow exploration."""
import copy
from html.parser import HTMLParser
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill-ab-test/scripts'))
from test_trace_report import make_run, Document
import trace_report as renderer


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = make_run(Path(self.temp.name) / 'run')
        self.run = renderer.load_trace_run(self.root)

    def test_comparison_is_initial_view_even_with_source_analysis(self):
        from test_skill_analysis import sample_analysis
        self.run['analysis'] = sample_analysis()
        html = renderer.build_html(self.run)
        tabs = [attrs for tag, attrs in Document(html).tags if attrs.get('role') == 'tab']
        self.assertEqual([a['data-panel'] for a in tabs if a['aria-selected'] == 'true'], ['comparison'])

    def test_one_comparison_exposes_both_original_events_and_exact_quote(self):
        html = renderer.build_html(self.run)
        doc = Document(html)
        self.assertIn('comparison-1', [attrs.get('id') for _, attrs in doc.tags])
        self.assertIn('data-comparison-target="comparison-1"', html)
        self.assertIn('data-evidence-source="event-baseline-L5"', html)
        self.assertIn('data-evidence-source="event-treatment-L5"', html)
        self.assertIn('<mark>정답은 3이다.</mark>', html)
        self.assertIn('cat input.txt', html)
        self.assertNotIn('DO_NOT_DISPLAY_OR_QUOTE', html)

    def test_absence_keeps_its_scope_and_does_not_invent_matching_event(self):
        diff = self.run['review']['differences'][0]
        diff['a'] = {'kind': 'absence', 'scope': 'recorded_events', 'note': '기록된 이벤트에서 파일 검증이 관찰되지 않음'}
        html = renderer.build_html(self.run)
        self.assertIn('기록된 이벤트에서 파일 검증이 관찰되지 않음', html)
        self.assertIn('기록된 관측 이벤트 전체', html)
        self.assertNotIn('data-evidence-source="event-baseline-L5"', html)

    def test_html_in_answer_quote_is_escaped_and_original_text_retained(self):
        diff = self.run['review']['differences'][0]
        diff['a'] = {'kind': 'answer', 'quote': '<script>literal</script>'}
        html = renderer.build_html(self.run)
        self.assertIn('<mark>&lt;script&gt;literal&lt;/script&gt;</mark>', html)
        self.assertNotIn('<script>literal</script>', html)

    def test_extra_action_does_not_create_baseline_peer(self):
        extra = copy.deepcopy(self.run['arms']['treatment']['events'][4])
        extra['line'] = 99
        extra['event']['item']['id'] = 'extra-action'
        extra['event']['item']['command'] = 'cat additional.txt'
        self.run['arms']['treatment']['events'].append(extra)
        self.run['arms']['treatment']['received'][99] = '2026-11-17T06:00:00Z'
        html = renderer.build_html(self.run)
        self.assertIn('href="#event-treatment-L99"', html)
        self.assertNotIn('event-baseline-L99', html)

    def test_all_comparisons_of_same_event_remain_reachable_from_flow(self):
        from comparison_views import flow_lane
        second = copy.deepcopy(self.run['review']['differences'][0])
        second['title'] = '같은 이벤트의 다른 비교'
        self.run['review']['differences'].append(second)
        html = flow_lane(self.run, 'baseline')
        self.assertIn('href="#comparison-1"', html)
        self.assertIn('href="#comparison-2"', html)

    def test_lifecycle_start_evidence_links_to_the_coalesced_action(self):
        from comparison_views import flow_lane
        self.run['review']['differences'][0]['a'] = {'kind': 'event', 'line': 4, 'quote': 'cat input.txt'}
        html = flow_lane(self.run, 'baseline')
        self.assertIn('href="#comparison-1"', html)
        self.assertEqual(html.count('class="step-index"'), 2)

    def test_observed_plan_and_todo_actions_stay_in_workflow(self):
        from comparison_views import flow_lane
        for number, kind in enumerate(['plan_update', 'todo_list'], 90):
            self.run['arms']['baseline']['events'].append({'line': number, 'event': {'type': 'item.completed', 'item': {'id': str(number), 'type': kind, 'items': []}}})
        html = flow_lane(self.run, 'baseline')
        self.assertIn('href="#event-baseline-L90"', html)
        self.assertIn('href="#event-baseline-L91"', html)

    def test_answer_screenshot_links_to_matching_final_message(self):
        from comparison_views import paired_evidence
        capture = {'arm': 'baseline', 'title': '원본 응답 화면', 'source_file': 'arms/baseline/events.jsonl', 'visible_lines': {'start': 6, 'end': 6}}
        html = paired_evidence(self.run, 'baseline', {'kind': 'answer', 'quote': '수치는 3'}, [capture])
        self.assertIn('href="#capture-baseline-1"', html)

    def test_condition_labels_follow_verified_registration_state(self):
        from comparison_views import flow_lane
        state = self.run['arms']['baseline']['state']
        state['skill_state']['target_registered'] = True
        html = flow_lane(self.run, 'baseline')
        self.assertIn('대상 스킬 비활성', html)
        self.assertNotIn('스킬 없음', html)
        state['preflight_verified'] = False
        self.assertIn('스킬 상태 미검증', flow_lane(self.run, 'baseline'))

    def test_case_evidence_shows_exact_case_without_other_question_text(self):
        from comparison_views import paired_evidence
        self.run['arms']['baseline']['answer'] = '## Q1\n첫 문항 원문\n\n## Q2\n둘째 문항 원문\n\n## Q3\n셋째 문항 원문\n'
        html = paired_evidence(self.run, 'baseline', {'kind': 'answer', 'quote': '둘째'}, [], case_id='Q2')
        self.assertIn('문항 원문', html)
        self.assertNotIn('첫 문항', html)
        self.assertNotIn('셋째 문항', html)


if __name__ == '__main__':
    unittest.main()
