"""Behavior checks for the offline, evidence-preserving HTML report."""

from html.parser import HTMLParser
from contextlib import contextmanager
import base64
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skill-ab-test" / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))


class HTMLDocument(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.all_text = []
        self.answers = {}
        self.answer_details = {}
        self._details = []
        self.scripts = []
        self._script = None
        self._answer = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == "details":
            self._details.append(attrs)
        if tag == "script":
            self._script = ""
        if attrs.get("data-role") == "answer-text":
            self._answer = (attrs["data-arm"], attrs["data-case"])
            self.answers[self._answer] = ""
            self.answer_details[self._answer] = list(self._details)

    def handle_endtag(self, tag):
        if tag == "pre":
            self._answer = None
        if tag == "details":
            self._details.pop()
        if tag == "script":
            self.scripts.append(self._script)
            self._script = None

    def handle_data(self, data):
        self.all_text.append(data)
        if self._script is not None:
            self._script += data
        if self._answer is not None:
            self.answers[self._answer] += data


class RenderReportTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec("render_report"),
            "render_report module must implement the approved report contract",
        )
        self.renderer = importlib.import_module("render_report")
        self.lib = importlib.import_module("report_lib")
        self.factory = importlib.import_module("fixture_factory")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = self.factory.make_run(Path(self.temp.name) / "run")

    def read_json(self, relative):
        return json.loads((self.run / relative).read_text(encoding="utf-8"))

    def write_json(self, relative, data):
        (self.run / relative).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def recapture(self):
        (self.run / "answers-lock.json").unlink()
        self.lib.capture_answers(self.run)

    def reseal(self):
        (self.run / "lock.json").unlink()
        (self.run / "prompts/baseline.txt").unlink()
        (self.run / "prompts/treatment.txt").unlink()
        self.lib.prepare_run(self.run)
        self.recapture()

    def render(self, output=None):
        path = self.renderer.render_report(self.run, output)
        html = path.read_text(encoding="utf-8")
        return path, html, HTMLDocument(html)

    def test_preserves_all_six_answer_texts_and_displays_main_judgments(self):
        path, html, document = self.render()
        self.assertEqual(path, self.run / "report.html")
        for arm in ("baseline", "treatment"):
            for answer in self.read_json(f"{arm}/answers.json")["answers"]:
                self.assertEqual(
                    document.answers[(arm, answer["case_id"])], answer["text"]
                )
        result = self.read_json("result.json")
        text = "".join(document.all_text)
        self.assertIn(result["summary"], text)
        self.assertIn("평가자: 메인 에이전트", text)
        self.assertIn("블라인드 평가 아님", text)
        self.assertIn("단일 실행", text)
        for comparison in result["comparisons"]:
            self.assertIn(comparison["observed"], text)
            self.assertIn(comparison["rationale"], text)
        self.assertIn('lang="ko"', html)

    def test_escapes_untrusted_text_and_loads_no_external_resources(self):
        payload = '<script>alert("x")</script><img src="https://evil.invalid/pixel" onerror="go()"> & 끝'
        answers = self.read_json("baseline/answers.json")
        answers["answers"][0]["text"] = payload
        self.write_json("baseline/answers.json", answers)
        result = self.read_json("result.json")
        comparison = result["comparisons"][0]
        comparison["a"] = {"kind": "quote", "start": 0, "end": len(payload), "text": payload}
        comparison["verification"]["sources"] = [
            {"title": '<b>자료</b>', "url": "https://example.org/evidence?a=1&b=2"}
        ]
        self.write_json("result.json", result)
        self.recapture()
        _, html, document = self.render()
        self.assertEqual(document.answers[("baseline", "Q1")], payload)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<b>자료</b>", html)
        for tag, attrs in document.tags:
            self.assertNotIn(tag, {"img", "iframe", "object", "embed", "link"})
            self.assertFalse(any(key.startswith("on") for key in attrs))
            if tag == "script":
                self.assertNotIn("src", attrs)
            if "href" in attrs:
                self.assertTrue(attrs["href"].startswith(("#", "https://", "http://")))
        self.assertIn("https://example.org/evidence?a=1&b=2", [attrs.get("href") for _, attrs in document.tags])
        csp = next(attrs["content"] for tag, attrs in document.tags if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy")
        for script in document.scripts:
            digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
            self.assertIn(f"'sha256-{digest}'", csp)
            self.assertNotIn(payload, script)

    def test_shows_absence_and_collection_failure_without_fabricating_quotes(self):
        answers = self.read_json("baseline/answers.json")
        answers["answers"] = [a for a in answers["answers"] if a["case_id"] != "Q3"]
        self.write_json("baseline/answers.json", answers)
        execution = self.read_json("execution.json")
        execution["arms"]["baseline"]["status"] = "partial"
        execution["arms"]["baseline"]["error"] = "Q3 제출 중단"
        self.write_json("execution.json", execution)
        result = self.read_json("result.json")
        q2 = next(c for c in result["comparisons"] if c["case_id"] == "Q2")
        q2["a"] = {"kind": "absence", "scope": "entire_answer", "note": "답변 전체에서 요청한 근거를 찾지 못함"}
        q3 = next(c for c in result["comparisons"] if c["case_id"] == "Q3")
        q3["a"] = {"kind": "unavailable", "reason": "Q3 제출 파일에 답변 없음"}
        q3["verdict"] = "undetermined"
        self.write_json("result.json", result)
        self.recapture()
        _, _, document = self.render()
        text = "".join(document.all_text)
        self.assertIn("답변 전체에서 요청한 근거를 찾지 못함", text)
        self.assertIn("검사 범위: 답변 전체", text)
        self.assertIn("Q3 제출 파일에 답변 없음", text)
        self.assertIn("수집 실패", text)
        self.assertNotIn(("baseline", "Q3"), document.answers)
        self.assertIn("판단 불가", text)

    def test_redacts_original_and_overlapping_quote_but_preserves_submission(self):
        raw = "시작 AUTH-TOKEN-EXAMPLE 끝"
        answers = self.read_json("baseline/answers.json")
        answers["answers"][0]["text"] = raw
        self.write_json("baseline/answers.json", answers)
        result = self.read_json("result.json")
        result["comparisons"][0]["a"] = {"kind": "quote", "start": 0, "end": len(raw), "text": raw}
        result["comparisons"][0]["verdict"] = "undetermined"
        result["redactions"] = [{"arm": "baseline", "case_id": "Q1", "start": 3, "end": 21, "reason": "인증정보"}]
        self.write_json("result.json", result)
        self.recapture()
        _, html, document = self.render()
        self.assertNotIn("AUTH-TOKEN-EXAMPLE", html)
        self.assertEqual(document.answers[("baseline", "Q1")], "시작 [인증정보 가림] 끝")
        self.assertGreaterEqual("".join(document.all_text).count("[인증정보 가림]"), 2)
        self.assertIn("표시 사본 · 일부 가림", "".join(document.all_text))
        self.assertEqual(self.read_json("baseline/answers.json")["answers"][0]["text"], raw)

    def test_source_evidence_has_line_locations_and_omits_unreferenced_source(self):
        experiment = self.read_json("experiment.json")
        source_path = experiment["source"]["files"][0]["path"]
        (self.run / source_path).write_text("첫 줄 지침\n관련 지침 <literal>\n미인용 소스 전체는 숨김\n", encoding="utf-8")
        result = self.read_json("result.json")
        for comparison in result["comparisons"]:
            comparison["skill_links"] = [{"file": source_path, "start_line": 2, "end_line": 2}]
        self.write_json("result.json", result)
        self.reseal()
        _, html, document = self.render()
        text = "".join(document.all_text)
        self.assertIn("관련 지침 <literal>", text)
        self.assertIn(f"{source_path}:2–2", text)
        self.assertIn("문자 위치", text)
        self.assertNotIn("미인용 소스 전체는 숨김", html)
        self.assertNotIn("첫 줄 지침", html)
        self.assertIn(hashlib.sha256((self.run / source_path).read_bytes()).hexdigest(), text)

    def test_title_with_template_marker_does_not_replace_document_content(self):
        experiment = self.read_json("experiment.json")
        experiment["source"]["title"] = "{{BODY}} 그대로인 제목"
        self.write_json("experiment.json", experiment)
        self.reseal()
        _, html, document = self.render()
        self.assertEqual(len(document.answers), 6)
        self.assertIn("{{BODY}} 그대로인 제목", "".join(document.all_text))
        self.assertIn("<main><header", html)

    def test_invalid_input_does_not_create_or_replace_output(self):
        output = self.run / "custom-report.html"
        output.write_text("기존 검증 보고서", encoding="utf-8")
        result = self.read_json("result.json")
        result["comparisons"][0]["a"]["text"] = "원문과 불일치"
        self.write_json("result.json", result)
        with self.assertRaises(self.lib.ValidationError):
            self.renderer.render_report(self.run, output)
        self.assertEqual(output.read_text(encoding="utf-8"), "기존 검증 보고서")
        self.assertFalse((self.run / "report.html").exists())

    def test_custom_output_cannot_overwrite_preserved_submission(self):
        output = self.run / "baseline/answers.json"
        original = output.read_bytes()
        with self.assertRaises(self.lib.ValidationError):
            self.renderer.render_report(self.run, output)
        self.assertEqual(output.read_bytes(), original)

    def test_replace_failure_preserves_existing_report_and_removes_temporary_file(self):
        output = self.run / "report.html"
        original = "이전에 검증한 보고서".encode("utf-8")
        output.write_bytes(original)
        files_before = set(self.run.iterdir())
        with patch("os.replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                self.renderer.render_report(self.run)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(set(self.run.iterdir()), files_before)

    def test_partial_write_failure_preserves_existing_report_and_removes_temporary_file(self):
        output = self.run / "report.html"
        original = "이전에 검증한 보고서".encode("utf-8")
        output.write_bytes(original)
        files_before = set(self.run.iterdir())
        real_fdopen = os.fdopen

        @contextmanager
        def fail_after_partial_write(fd, mode):
            with real_fdopen(fd, mode) as stream:
                class PartialWriter:
                    def write(self, payload):
                        stream.write(payload[:13])
                        stream.flush()
                        raise OSError("simulated partial write failure")

                yield PartialWriter()

        with patch("os.fdopen", side_effect=fail_after_partial_write):
            with self.assertRaises(OSError):
                self.renderer.render_report(self.run)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(set(self.run.iterdir()), files_before)

    def test_unencodable_rendered_text_preserves_existing_report(self):
        output = self.run / "report.html"
        original = "이전에 검증한 보고서".encode("utf-8")
        output.write_bytes(original)
        files_before = set(self.run.iterdir())
        run = self.lib.load_run(self.run)
        run["result"]["summary"] = "출력 오류 주입: \ud800"
        # Fault injection bypasses the loader's own Unicode validation to check
        # the renderer's guarantee before it opens any output file.
        with patch.object(self.renderer, "load_run", return_value=run):
            with self.assertRaises((self.lib.ValidationError, UnicodeError)):
                self.renderer.render_report(self.run)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(set(self.run.iterdir()), files_before)

    def test_output_symlink_cannot_overwrite_preserved_submission(self):
        original_path = self.run / "baseline/answers.json"
        original = original_path.read_bytes()
        output = self.run / "report.html"
        output.symlink_to(original_path)
        with self.assertRaises(self.lib.ValidationError):
            self.renderer.render_report(self.run)
        self.assertTrue(output.is_symlink())
        self.assertEqual(original_path.read_bytes(), original)

    def test_main_presentation_drives_short_intro_and_evidence_links(self):
        presentation = {
            "what": "제공된 수치를 보존하도록 답변을 검토하는 스킬입니다.",
            "input": "주어진 문장과 수치", "process": "사실 보존 지침 적용",
            "output": "근거에 맞춘 응답", "takeaway": "이 실행에서는 양쪽 모두 수치를 보존했습니다.",
            "case_labels": {"Q1": "수치 설명", "Q2": "복합 제약", "Q3": "누락 정보"},
            "highlights": [{"case_id": "Q1", "criterion_id": "C1", "title": "수치 보존은 동일", "detail": "두 응답의 같은 수치를 확인했습니다."}],
        }
        result = self.read_json("result.json")
        result["presentation"] = presentation
        self.write_json("result.json", result)
        _, _, document = self.render()
        text = "".join(document.all_text)
        for field in ("what", "input", "process", "output", "takeaway"):
            self.assertIn(presentation[field], text)
        self.assertIn("수치 보존은 동일", text)
        self.assertIn("#comparison-Q1-C1", [attrs.get("href") for _, attrs in document.tags])

    def test_disclosure_defaults_show_first_capture_and_keep_originals_available(self):
        _, _, document = self.render()
        details = {attrs["id"]: attrs for tag, attrs in document.tags if tag == "details" and "id" in attrs}
        self.assertIn("open", details.get("Q1", {}))
        self.assertNotIn("open", details.get("Q2", {}))
        self.assertNotIn("open", details.get("Q3", {}))
        self.assertIn("method", details)
        self.assertNotIn("open", details["method"])
        self.assertEqual(len(document.answers), 6)
        for ancestors in document.answer_details.values():
            self.assertTrue(any("open" not in detail for detail in ancestors))
        self.assertIn("화면 캡처 미수집", "".join(document.all_text))
        self.assertIn("평가 범위와 메인 요약", "".join(document.all_text))

    def test_embeds_only_capture_data_images_with_accessible_text(self):
        uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2ioAAAAASUVORK5CYII="
        shots = {("Q1", "baseline"): {"data_uri": uri, "caption": "브라우저 캡처 설명", "alt": "Q1 기본 응답 화면", "width": 1, "height": 1}}
        with patch.object(self.renderer, "load_screenshots", return_value=shots, create=True):
            _, _, document = self.render()
        images = [attrs for tag, attrs in document.tags if tag == "img"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["src"], uri)
        self.assertEqual(images[0]["alt"], "Q1 기본 응답 화면")
        self.assertIn("브라우저 캡처 설명", "".join(document.all_text))
        csp = next(attrs["content"] for tag, attrs in document.tags if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy")
        self.assertIn("img-src data:", csp)
        buttons = [attrs for tag, attrs in document.tags if tag == "button" and "data-capture-zoom" in attrs]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]["aria-pressed"], "false")
        self.assertEqual(buttons[0]["aria-expanded"], "false")
        frame = next(attrs for tag, attrs in document.tags if attrs.get("id") == buttons[0]["aria-controls"])
        self.assertEqual(frame["role"], "region")
        self.assertEqual(frame["tabindex"], "0")
        self.assertIn("크게 보기", "".join(document.all_text))

    def test_external_image_is_not_embedded_as_a_capture(self):
        shots = {("Q1", "baseline"): {"data_uri": "https://evil.invalid/pixel", "caption": "외부 이미지", "alt": "이미지", "width": 1, "height": 1}}
        with patch.object(self.renderer, "load_screenshots", return_value=shots, create=True):
            _, _, document = self.render()
        self.assertFalse([attrs for tag, attrs in document.tags if tag == "img"])
        self.assertFalse([attrs for tag, attrs in document.tags if tag == "button" and "data-capture-zoom" in attrs])
        self.assertIn("화면 캡처 미수집", "".join(document.all_text))


if __name__ == "__main__":
    unittest.main()
