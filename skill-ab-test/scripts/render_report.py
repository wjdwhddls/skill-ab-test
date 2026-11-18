#!/usr/bin/env python3
"""Render a validated, main-authored comparison as one offline HTML file.

This module only presents evidence and judgments returned by ``load_run``.
It does not assess answer quality, calculate scores, or call external services.
"""

import argparse
import base64
import binascii
import hashlib
from html import escape
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit

from report_lib import ValidationError, load_run


TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "report-template.html"
ARM_LABELS = {
    "baseline": ("A", "기본 응답", "대상 지침 추가 제공 없음"),
    "treatment": ("B", "스킬 적용 응답", "대상 스킬 지침을 추가한 조건"),
}
VERDICTS = {
    "improved": "개선", "worsened": "악화",
    "unchanged": "차이 없음", "undetermined": "판단 불가",
}
CASE_KINDS = {
    "representative": "대표 상황", "constraints": "복합 조건",
    "insufficient_information": "정보 부족·경계 상황",
}
ARM_STATES = {"completed": "수집 완료", "partial": "일부 수집", "failed": "수집 실패"}
APPLICATION_STATES = {
    "provided": "원문 패킷 제공 기록 있음",
    "partially_provided": "일부 자료만 제공",
    "not_verified": "대상 자료 제공 미확인",
}
INTEGRITY_STATES = {
    "observed_no_contamination": "관찰 기록에서 조건 오염 미발견",
    "limited_visibility": "관찰 범위 제한",
    "contaminated": "조건 오염 확인 · 유효한 미적용 비교 아님",
}
VERIFICATION_KINDS = {
    "input": "공통 자료 대조", "interpretation": "평가자의 해석",
    "unverified": "미검증",
}
REDACTION = "[인증정보 가림]"
DISCLOSURE_SCRIPT = """(() => {
  const reveal = () => {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
    const target = document.getElementById(id);
    if (!target) return;
    for (let node = target; node; node = node.parentElement) {
      if (node.tagName === 'DETAILS') node.open = true;
    }
    target.scrollIntoView({block: 'start'});
  };
  addEventListener('hashchange', reveal);
  document.querySelectorAll('[data-capture-zoom]').forEach(button => { button.hidden = false; });
  document.addEventListener('click', event => {
    if (!(event.target instanceof Element)) return;
    const button = event.target.closest('button[data-capture-zoom]');
    if (button) {
      const frame = document.getElementById(button.dataset.captureZoom);
      if (!frame) return;
      const zoomed = frame.classList.toggle('is-zoomed');
      button.setAttribute('aria-pressed', String(zoomed));
      button.setAttribute('aria-expanded', String(zoomed));
      button.textContent = zoomed ? '맞춰 보기' : '크게 보기';
      if (!zoomed) frame.scrollLeft = 0;
      return;
    }
    const link = event.target.closest('a[href^="#"]');
    if (link) requestAnimationFrame(reveal);
  });
  let printState = [];
  addEventListener('beforeprint', () => {
    printState = Array.from(document.querySelectorAll('details'), node => [node, node.open]);
    printState.forEach(([node]) => { node.open = true; });
  });
  addEventListener('afterprint', () => {
    printState.forEach(([node, open]) => { node.open = open; });
  });
  reveal();
})();"""


def load_screenshots(run_dir, run):
    """Use the optional capture module; older packages still render text reports."""
    try:
        from capture_responses import load_screenshots as read_screenshots
    except ModuleNotFoundError as exc:
        if exc.name != "capture_responses":
            raise
        return {}
    return read_screenshots(run_dir, run)


def _escape(value):
    # A character reference keeps CR characters from HTML newline normalization.
    return escape(str(value), quote=True).replace("\r", "&#13;")


def _value(value):
    return "미확인" if value is None else _escape(value)


def _link(url, label):
    """Only http(s) URLs become links; all labels remain untrusted text."""
    try:
        parts = urlsplit(url)
        allowed = (
            parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)
            and not any(ord(char) < 32 or ord(char) == 127 for char in url)
        )
    except (ValueError, TypeError):
        allowed = False
    if not allowed:
        return _escape(label)
    return f'<a href="{_escape(url)}" rel="noreferrer noopener">{_escape(label)}</a>'


def _paragraph(value, class_name=""):
    cls = f' class="{class_name}"' if class_name else ""
    return f"<p{cls}>{_escape(value)}</p>"


def _list(values, empty="없음"):
    if not values:
        return f'<p class="muted">{_escape(empty)}</p>'
    return "<ul>" + "".join(f"<li>{_escape(value)}</li>" for value in values) + "</ul>"


def _facts(items):
    return '<dl class="facts">' + "".join(
        f"<div><dt>{_escape(label)}</dt><dd>{value}</dd></div>"
        for label, value in items
    ) + "</dl>"


def _redactions(run, arm, case_id):
    return sorted(
        (item for item in run["result"]["redactions"]
         if item["arm"] == arm and item["case_id"] == case_id),
        key=lambda item: item["start"],
    )


def _display_text(text, redactions, offset=0):
    """Mask only the overlapping original-code-point intervals in a display copy."""
    pieces = []
    cursor = 0
    end = offset + len(text)
    for item in redactions:
        start = max(item["start"], offset) - offset
        stop = min(item["end"], end) - offset
        if start >= stop:
            continue
        pieces.extend((text[cursor:start], REDACTION))
        cursor = stop
    pieces.append(text[cursor:])
    return "".join(pieces)


def _answer(run, arm, case_id):
    letter, title, description = ARM_LABELS[arm]
    text = run["answers"][arm].get(case_id)
    redactions = _redactions(run, arm, case_id)
    heading = (
        f'<section class="answer" aria-labelledby="{case_id}-{arm}-title">'
        f'<h3 id="{case_id}-{arm}-title"><span class="arm-letter">{letter}</span> '
        f'{title}</h3><p class="arm-description">{description}</p>'
    )
    if text is None:
        state = run["execution"]["arms"][arm]
        reason = state.get("error") or "이 질문의 정식 답변을 수집하지 못했습니다."
        return heading + (
            f'<div id="answer-{case_id}-{arm}" class="missing"><strong>수집 실패</strong>'
            f'{_paragraph(reason)}<p>답변 품질에 대한 판정으로 해석하지 않습니다.</p></div></section>'
        )
    label = "표시 사본 · 일부 가림" if redactions else "정식 제출 원문"
    body = _display_text(text, redactions)
    empty_note = '<p class="muted">빈 문자열로 제출된 원문입니다.</p>' if not text else ""
    return heading + (
        f'<p class="source-label">{label}</p>'
        f'<pre id="answer-{case_id}-{arm}" class="answer-text" data-role="answer-text" '
        f'data-arm="{arm}" data-case="{case_id}"><span>{_escape(body)}</span></pre>'
        f'{empty_note}</section>'
    )


def _evidence(run, evidence, arm, case_id):
    letter = ARM_LABELS[arm][0]
    anchor = f"answer-{case_id}-{arm}"
    heading = f'<h5>{letter}의 근거</h5>'
    if evidence["kind"] == "quote":
        display = _display_text(
            evidence["text"], _redactions(run, arm, case_id), evidence["start"]
        )
        location = (
            f'<a href="#{anchor}">{case_id} · {letter} 원문</a> · '
            f'문자 위치 [{evidence["start"]}, {evidence["end"]})'
        )
        return (
            f'<div class="evidence">{heading}<blockquote>{_escape(display)}</blockquote>'
            f'<p class="location">{location}</p></div>'
        )
    if evidence["kind"] == "absence":
        return (
            f'<div class="evidence">{heading}<p class="evidence-state">해당 내용 부재 · 메인 판단</p>'
            f'{_paragraph(evidence["note"])}<p class="location">검사 범위: 답변 전체 · '
            f'<a href="#{anchor}">{case_id} · {letter} 원문 확인</a></p></div>'
        )
    return (
        f'<div class="evidence">{heading}<p class="evidence-state">수집 실패 · 근거 확인 불가</p>'
        f'{_paragraph(evidence["reason"])}</div>'
    )


def _skill_evidence(run, links):
    if not links:
        return '<p class="muted">관련 스킬 지침 위치가 연결되지 않았습니다.</p>'
    fragments = []
    for link in links:
        path, start, end = link["file"], link["start_line"], link["end_line"]
        lines = run["source_texts"][path].splitlines()[start - 1:end]
        excerpt = "\n".join(f"{number:>3}  {line}" for number, line in enumerate(lines, start))
        fragments.append(
            '<figure class="skill-excerpt">'
            f'<figcaption>{_escape(path)}:{start}–{end}</figcaption>'
            f'<pre>{_escape(excerpt)}</pre></figure>'
        )
    return "".join(fragments)


def _comparison(run, comparison, criterion):
    case_id = comparison["case_id"]
    verdict = comparison["verdict"]
    verification = comparison["verification"]
    citations = ""
    if verification["sources"]:
        citations = '<ul class="citations">' + "".join(
            f'<li>{_link(source["url"], source["title"])}</li>'
            for source in verification["sources"]
        ) + "</ul>"
    criterion_id = _escape(criterion["id"])
    return (
        f'<section class="comparison" id="comparison-{case_id}-{criterion_id}" aria-labelledby="{case_id}-{criterion_id}-title">'
        '<div class="criterion-heading">'
        f'<h4 id="{case_id}-{criterion_id}-title">{_escape(criterion["label"])}</h4>'
        f'<span class="verdict verdict-{verdict}">{VERDICTS[verdict]}</span></div>'
        f'{_paragraph(criterion["rule"], "criterion-rule")}'
        '<div class="evidence-columns">'
        f'{_evidence(run, comparison["a"], "baseline", case_id)}'
        f'{_evidence(run, comparison["b"], "treatment", case_id)}</div>'
        '<dl class="judgment">'
        f'<div><dt>관찰한 변화</dt><dd>{_escape(comparison["observed"])}</dd></div>'
        f'<div><dt>메인의 판정 이유</dt><dd>{_escape(comparison["rationale"])}</dd></div>'
        f'<div><dt>확인 방식 · {VERIFICATION_KINDS[verification["kind"]]}</dt>'
        f'<dd>{_escape(verification["detail"])}{citations}</dd></div></dl>'
        '<details class="skill-details"><summary>관련 스킬 지침</summary>'
        '<p class="muted">이 지침과 변화의 연결은 메인 평가자의 해석이며, 원인을 확정하지 않습니다.</p>'
        f'{_skill_evidence(run, comparison["skill_links"])}</details></section>'
    )


def _image_uri(value):
    """Only raster data URLs from verified captures may become image sources."""
    if not isinstance(value, str):
        return None
    for prefix, signature in (
        ("data:image/png;base64,", b"\x89PNG\r\n\x1a\n"),
        ("data:image/jpeg;base64,", b"\xff\xd8\xff"),
    ):
        if value.startswith(prefix):
            try:
                raw = base64.b64decode(value[len(prefix):], validate=True)
            except (binascii.Error, ValueError):
                return None
            return value if raw.startswith(signature) else None
    return None


def _capture(run, screenshots, case_id, arm):
    letter, title, description = ARM_LABELS[arm]
    shot = screenshots.get((case_id, arm), {})
    uri = _image_uri(shot.get("data_uri"))
    heading = f'<h3><span class="arm-letter">{letter}</span> {title}</h3>'
    raw_link = f'<a href="#answer-{case_id}-{arm}">응답 원문 보기</a>'
    zoom_button = ""
    if uri:
        dimensions = "".join(
            f' {key}="{shot[key]}"' for key in ("width", "height")
            if isinstance(shot.get(key), int) and not isinstance(shot.get(key), bool) and shot[key] > 0
        )
        alt = shot.get("alt") or f"{case_id} · {letter}의 응답 화면"
        loading = "eager" if case_id == "Q1" else "lazy"
        frame_id = f"capture-{case_id}-{arm}"
        visual = (
            f'<div class="capture-frame" id="{frame_id}" tabindex="0" role="region" aria-label="{case_id} {letter} 응답 화면 스크롤">'
            f'<img src="{_escape(uri)}" alt="{_escape(alt)}"{dimensions} loading="{loading}" decoding="async"></div>'
            f'<figcaption>{_escape(shot["caption"])}</figcaption>'
        )
        zoom_button = (
            f'<button type="button" class="capture-zoom" data-capture-zoom="{frame_id}" '
            f'aria-controls="{frame_id}" aria-pressed="false" aria-expanded="false" hidden>크게 보기</button>'
        )
    else:
        visual = '<div class="capture-missing"><p>화면 캡처 미수집</p><p>아래 링크에서 제출 원문을 확인할 수 있습니다.</p></div>'
    return (
        f'<figure class="capture-figure">{heading}<p class="arm-description">{description}</p>'
        + visual + f'<p class="capture-links">{raw_link}{zoom_button}</p></figure>'
    )


def _highlights(run, case_id, comparisons):
    highlights = run["result"].get("presentation", {}).get("highlights", [])
    selected = [item for item in highlights if item["case_id"] == case_id]
    if not selected:
        return ""
    by_criterion = {item["criterion_id"]: item for item in comparisons}
    items = []
    for item in selected:
        comparison = by_criterion[item["criterion_id"]]
        verdict = comparison["verdict"]
        target = f'comparison-{case_id}-{_escape(item["criterion_id"])}'
        items.append(
            '<li><div class="highlight-heading">'
            f'<span class="verdict verdict-{verdict}">{VERDICTS[verdict]}</span>'
            f'<h4>{_escape(item["title"])}</h4></div>'
            f'{_paragraph(item["detail"])}<a href="#{target}">양쪽 발췌와 판정 근거 보기</a></li>'
        )
    return '<section class="highlights"><h3>눈여겨볼 차이</h3><ul>' + "".join(items) + '</ul></section>'


def _case(run, case, comparisons, screenshots):
    case_id = case["id"]
    criteria = {criterion["id"]: criterion for criterion in case["criteria"]}
    label = run["result"].get("presentation", {}).get("case_labels", {}).get(case_id, CASE_KINDS[case["kind"]])
    opened = " open" if case_id == "Q1" else ""
    raw_answers = "".join(
        f'<details class="raw-answer" id="original-{case_id}-{arm}"><summary>{ARM_LABELS[arm][0]} 응답 원문</summary>'
        + _answer(run, arm, case_id) + '</details>' for arm in ("baseline", "treatment")
    )
    return (
        f'<details class="case" id="{case_id}"{opened}><summary><span class="case-id">{case_id}</span>'
        f'<span class="case-label">{_escape(label)}</span><span class="case-action">응답 비교</span></summary>'
        '<div class="case-content">'
        '<details class="case-input"><summary>질문과 공통 입력 보기</summary>'
        f'{_paragraph(case["prompt"], "question")}'
        f'<pre class="input-text"><span>{_escape(case["input"])}</span></pre>'
        f'<p><strong>선정 이유</strong> {_escape(case["why"])}</p></details>'
        '<div class="capture-columns">'
        + _capture(run, screenshots, case_id, "baseline") + _capture(run, screenshots, case_id, "treatment")
        + '</div>' + _highlights(run, case_id, comparisons)
        + '<div class="originals">' + raw_answers + '</div>'
        f'<details class="main-assessment" id="criteria-{case_id}"><summary>전체 {len(comparisons)}개 기준과 판정 근거</summary>'
        f'{_paragraph(run["result"]["case_summaries"][case_id], "case-summary")}'
        + "".join(_comparison(run, item, criteria[item["criterion_id"]]) for item in comparisons)
        + '</details></div></details>'
    )


def _source_section(run):
    experiment = run["experiment"]
    source, scope = experiment["source"], experiment["scope"]
    files = "<ul>" + "".join(
        f'<li>{_link(item["url"], item["path"])} '
        f'<span class="muted">({"대상 스킬" if item["role"] == "skill" else "참조 자료"})</span>'
        f'<span class="hash">SHA-256 {_escape(run["source_files"][item["path"]]["sha256"])}</span></li>'
        for item in source["files"]
    ) + "</ul>"
    return (
        '<section id="scope" class="report-section"><h2>대상 스킬과 평가 범위</h2>'
        + _facts([
            ("원본 출처", _link(source["url"], source["url"])),
            ("저장소 버전 · commit", _value(source.get("commit"))),
            ("원문 조회 시각", _value(source.get("retrieved_at"))),
            ("이번 평가 범위", _escape(scope["evaluated"])),
            ("추가 자료의 영향", "지침 제공에 따른 변화" if scope["knowledge_effect"] == "instructions" else _escape(scope["knowledge_effect"])),
        ])
        + '<h3>제공한 파일</h3>' + files
        + '<h3>평가에서 제외한 범위</h3>' + _list(scope["excluded"])
        + '</section>'
    )


def _execution_section(run):
    experiment, execution, result = run["experiment"], run["execution"], run["result"]
    conditions = experiment["conditions"]
    integrity, application = execution["integrity"], execution["application"]
    arm_details = []
    for arm in ("baseline", "treatment"):
        record = execution["arms"][arm]
        usage = record.get("usage") or {}
        details = _facts([
            ("수집 상태", ARM_STATES[record["status"]]),
            ("실행 에이전트", _value(record.get("agent_id"))),
            ("시작 시각", _value(record.get("started_at"))),
            ("종료 시각", _value(record.get("finished_at"))),
            ("입력 토큰", _value(usage.get("input_tokens"))),
            ("출력 토큰", _value(usage.get("output_tokens"))),
        ])
        if record.get("error"):
            details += '<h4>수집 오류</h4>' + _paragraph(record["error"])
        details += '<h4>관찰 기록</h4>' + _list(record.get("observations", []), "추가 관찰 기록 없음")
        arm_details.append(f'<section><h3>{ARM_LABELS[arm][0]} 실행 기록</h3>{details}</section>')
    return (
        '<section id="conditions" class="report-section"><h2>실험 조건과 확인 범위</h2>'
        + _facts([
            ("실행 ID", _escape(experiment["run_id"])),
            ("비교 방식", "대상 스킬 명시 적용 지시 유무 비교"),
            ("실험 생성 시각", _value(experiment.get("created_at"))),
            ("요청한 모델", _value(conditions.get("model_requested"))),
            ("요청한 추론 설정", _value(conditions.get("reasoning_requested"))),
            ("A의 대상 지침 사전 노출", "미확인" if conditions["baseline_exposure"] == "unknown" else _escape(conditions["baseline_exposure"])),
            ("응답 언어", "한국어" if conditions["language"] == "ko" else _escape(conditions["language"])),
            ("B 대상 자료 제공", APPLICATION_STATES[application["status"]]),
            ("비교 조건 확인", INTEGRITY_STATES[integrity["status"]]),
            ("평가 기록", f'메인 에이전트 · {_escape(result["evaluated_by"]["agent_id"])} · 블라인드 평가 아님'),
        ])
        + _paragraph(application["note"])
        + '<h3>공통 제약</h3>' + _list(conditions["constraints"])
        + '<h3>조건 확인의 한계</h3>' + _list(integrity["notes"])
        + '<p class="scope-note">평가자는 대상 스킬을 읽고 실행을 조율한 메인 에이전트입니다. 평가자 정보는 자기 기록이며 실행 주체의 암호학적 증명이 아닙니다. 파일 해시 일치는 답변과 자료의 보존 확인이며, 에이전트 간 완전한 격리 증명이 아닙니다.</p>'
        + '<div class="execution-columns">' + "".join(arm_details) + '</div>'
        + '<p class="muted">미확인 값은 추정하지 않았습니다. 시작·종료 시각에는 대기와 도구 사용 시간이 포함될 수 있습니다.</p></section>'
    )


def _answer_records(run):
    records = []
    for arm in ("baseline", "treatment"):
        metadata = run["answer_files"].get(arm)
        if not metadata:
            records.append(f'<li><strong>{ARM_LABELS[arm][0]}</strong> · 제출 파일 미확인</li>')
            continue
        # Metadata is presentational; hashes are supplied by the validating loader.
        path = metadata.get("path", f"{arm}/answers.json")
        digest = metadata.get("sha256")
        records.append(
            f'<li><strong>{ARM_LABELS[arm][0]}</strong> · {_escape(path)}'
            + (f'<span class="hash">SHA-256 {_escape(digest)}</span>' if digest else '') + '</li>'
        )
    return (
        '<section id="records" class="report-section"><h2>원문 보존 기록</h2>'
        '<p>이 HTML의 답변은 제출 파일의 문자열을 표시합니다. 원문은 수정하지 않으며, 명시된 인증정보 구간만 표시 사본과 인용에서 가립니다.</p>'
        '<p class="muted">인용의 문자 위치는 원문 기준으로 0부터 세는 Python 문자열 위치이며, 끝 위치는 포함하지 않습니다. 스킬 지침의 줄 번호는 1부터 셉니다.</p>'
        '<ul class="record-list">' + "".join(records) + '</ul></section>'
    )


def render_report(run_dir: Path, output: Path | None = None) -> Path:
    """Validate first; render only main-authored results without changing them."""
    run_dir = Path(run_dir)
    run = load_run(run_dir)
    experiment, result, execution = run["experiment"], run["result"], run["execution"]
    target = Path(output) if output is not None else run_dir / "report.html"
    if target.resolve().is_relative_to((run_dir / "screenshots").resolve()):
        raise ValidationError("HTML 출력은 보존된 캡처 폴더를 덮어쓸 수 없습니다.")
    protected_paths = [run_dir / path for path in (
        "experiment.json", "execution.json", "result.json", "lock.json", "answers-lock.json",
        "baseline/answers.json", "treatment/answers.json",
        "prompts/baseline.txt", "prompts/treatment.txt",
    )]
    protected_paths.extend(run_dir / item["path"] for item in experiment["source"]["files"])
    for protected in protected_paths:
        if target.resolve() == protected.resolve() or (
            target.exists() and protected.exists() and target.samefile(protected)
        ):
            raise ValidationError("HTML 출력은 보존된 실험·원문 파일을 덮어쓸 수 없습니다.")
    cases = experiment["cases"]
    comparisons = {
        case["id"]: [item for item in result["comparisons"] if item["case_id"] == case["id"]]
        for case in cases
    }
    screenshots = load_screenshots(run_dir, run)
    presentation = result.get("presentation")
    what = presentation["what"] if presentation else experiment["scope"]["evaluated"]
    takeaway = presentation["takeaway"] if presentation else result["summary"]
    flow = ""
    if presentation:
        flow = '<ol class="skill-flow" aria-label="입력에서 처리와 출력으로 이어지는 흐름">' + "".join(
            f'<li><h2>{label}</h2><p>{_escape(presentation[key])}</p></li>'
            for key, label in (("input", "입력"), ("process", "처리"), ("output", "출력"))
        ) + '</ol>'
    fallback_note = "" if presentation else '<p class="fallback-note">짧은 스킬 설명이 없는 기존 기록입니다. 평가 범위와 메인 요약을 표시합니다.</p>'
    navigation = "".join(
        f'<a href="#{case["id"]}">{case["id"]}</a>' for case in cases
    )
    header = (
        '<header class="report-header" id="report-top">'
        f'<h1>{_escape(experiment["source"]["title"])}</h1>'
        f'{_paragraph(what, "skill-description")}{_paragraph(takeaway, "takeaway")}'
        f'{fallback_note}{flow}'
        '<p class="scope-line">질문 3개 · 단일 실행 · '
        f'{INTEGRITY_STATES[execution["integrity"]["status"]]} · <a href="#conditions">확인 범위</a></p>'
        '</header>'
    )
    body = (
        header + '<section class="results" aria-labelledby="results-title">'
        + '<div class="results-heading"><h2 id="results-title">실제 응답으로 비교하기</h2>'
        + '<nav class="question-nav" aria-label="질문으로 이동">' + navigation + '</nav></div>'
        + '<p class="evaluator">평가자: 메인 에이전트 · 블라인드 평가 아님</p>'
        + "".join(_case(run, case, comparisons[case["id"]], screenshots) for case in cases)
        + '</section><details class="appendix" id="method"><summary>전체 요약·실험 조건·출처와 해시</summary>'
        + '<section class="report-section"><h2>메인의 전체 요약</h2>' + _paragraph(result["summary"]) + '</section>'
        + _execution_section(run) + _source_section(run) + _answer_records(run) + '</details>'
        + '<footer><p>제출 원문과 메인의 판정을 보존한 단일 실행 보고서입니다. 생성기는 내용을 평가하거나 채점하지 않습니다.</p>'
        + '<noscript><p>원문과 상세 근거는 해당 질문의 펼침 항목에서 확인할 수 있습니다.</p></noscript></footer>'
    )
    template = TEMPLATE.read_text(encoding="utf-8")
    script_hash = base64.b64encode(hashlib.sha256(DISCLOSURE_SCRIPT.encode("utf-8")).digest()).decode("ascii")
    substitutions = {
        "TITLE": _escape(experiment["source"]["title"]) + " · 응답 비교", "BODY": body,
        "SCRIPT": DISCLOSURE_SCRIPT, "SCRIPT_CSP": f"'sha256-{script_hash}'",
    }
    html = re.sub(r"\{\{(TITLE|BODY|SCRIPT|SCRIPT_CSP)\}\}", lambda match: substitutions[match[1]], template)
    # Encode before opening any output: encoding errors must not truncate an
    # existing report. The temporary file shares the target's filesystem so
    # replacement happens atomically only after the entire write succeeds.
    try:
        payload = html.encode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("출력 HTML을 UTF-8로 인코딩할 수 없습니다.") from exc
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        try:
            stream = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with stream as writer:
            writer.write(payload)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description="메인 평가 결과를 단일 오프라인 HTML로 렌더링합니다.")
    parser.add_argument("run_dir", type=Path, help="검증할 실행 폴더")
    parser.add_argument("--output", type=Path, help="HTML 출력 경로 (기본: RUN_DIR/report.html)")
    args = parser.parse_args(argv)
    try:
        output = render_report(args.run_dir, args.output)
    except (ValidationError, OSError) as exc:
        print(f"보고서 생성 실패: {exc}", file=sys.stderr)
        return 1
    print(f"보고서 생성: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
