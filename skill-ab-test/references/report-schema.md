> **기존 v1 재현 전용.** 이 형식은 지침 추가 제공과 답변 비교용이며 실제 OFF/ON 또는 실행 로그 증거가 아니다. 새 실행은 [trace-protocol.md](trace-protocol.md)를 따른다.

# 실행 파일과 JSON 계약

모든 JSON은 UTF-8로 기록한다. JSON 문자열의 짝 없는 유니코드 surrogate(lone surrogate)는 검증에서 거부한다. 아래 예시는 필수 필드와 세 문항을 모두 포함한 **형식 예시**다. 실제 GitHub 조회나 비교 완료의 증거가 아니므로 실제 실행에서는 출처·원문·질문·답변·판정을 각각 관찰값으로 작성한다.

## 파일 순서

```text
RUN_DIR/
├── source/SKILL.md
├── experiment.json          # 메인: 응답 생성 전 작성
├── prompts/baseline.txt     # prepare_run 생성
├── prompts/treatment.txt    # prepare_run 생성
├── lock.json               # prepare_run 생성, 수정하지 않음
├── baseline/answers.json   # A의 정식 제출
├── treatment/answers.json  # B의 정식 제출
├── execution.json         # 메인: 관찰한 실행 기록
├── answers-lock.json      # --capture 생성, 수정하지 않음
├── result.json            # 메인만 작성하는 분석과 판정
└── report.html            # render_report 생성
```

`skill_root`는 읽은 스킬 폴더, `run_dir`는 실행 폴더의 절대 경로다. 순서는 `experiment.json`과 원문 작성 → `python3 "$skill_root/scripts/prepare_run.py" "$run_dir"` → 두 응답 수집 → `execution.json` 작성 → 같은 명령에 `--capture` 추가 → 메인의 `result.json` 작성 → `python3 "$skill_root/scripts/render_report.py" "$run_dir"`다.

## 1. experiment.json

```json
{
  "schema_version": 1,
  "run_id": "format-example",
  "created_at": "2026-11-17T06:00:00Z",
  "source": {
    "title": "형식 예시: 제공된 자료로 답하기",
    "url": "https://github.com/example/skills/blob/main/SKILL.md",
    "commit": null,
    "retrieved_at": "2026-11-17T06:00:00Z",
    "files": [
      {
        "path": "source/SKILL.md",
        "url": "https://github.com/example/skills/blob/main/SKILL.md",
        "role": "skill"
      }
    ]
  },
  "scope": {
    "evaluated": "제공 자료에 근거한 짧은 텍스트 답변",
    "excluded": ["실제 설치", "외부 검색"],
    "knowledge_effect": "instructions"
  },
  "conditions": {
    "model_requested": null,
    "reasoning_requested": null,
    "baseline_exposure": "unknown",
    "language": "ko",
    "constraints": ["질문당 약 2000자 이내"]
  },
  "cases": [
    {
      "id": "Q1",
      "kind": "representative",
      "prompt": "행사 날짜를 한 문장으로 알려줘.",
      "input": "가상 안내문: 행사는 11월 20일에 열린다.",
      "why": "자료에서 명시한 사실을 전달하는 대표 상황이다.",
      "criteria": [
        {
          "id": "C1",
          "label": "날짜 정확성",
          "rule": "날짜를 11월 20일로 전달한다.",
          "verification": "input"
        }
      ]
    },
    {
      "id": "Q2",
      "kind": "constraints",
      "prompt": "가격과 신청 기간을 두 문장으로 알려줘.",
      "input": "가상 안내문: 가격은 3만원이다. 신청 기간은 11월 18일부터 19일까지다.",
      "why": "두 정보와 출력 형식 조건을 함께 처리한다.",
      "criteria": [
        {
          "id": "C1",
          "label": "정보와 형식 충족",
          "rule": "가격과 신청 기간을 정확히 포함하여 두 문장으로 답한다.",
          "verification": "input"
        }
      ]
    },
    {
      "id": "Q3",
      "kind": "insufficient_information",
      "prompt": "행사 장소를 알려줘.",
      "input": "가상 안내문: 행사 날짜는 11월 20일이다. 장소 정보는 없다.",
      "why": "요청한 사실이 자료에 없는 상황이다.",
      "criteria": [
        {
          "id": "C1",
          "label": "정보 부족 처리",
          "rule": "장소를 지어내지 않고 확인되지 않았음을 밝힌다.",
          "verification": "input"
        }
      ]
    }
  ]
}
```

형식 예시의 `source/SKILL.md`는 다음 한 줄이다. **실제 실행에서는 이 예시 문구를 대상 원문 대신 사용하지 않는다.**

```text
제공된 사실만 사용하고, 없는 정보는 확인되지 않았다고 표시한다.
```

규칙:

- `schema_version`은 `1`. `run_id`는 실행마다 구분한다. 시각은 ISO 8601 문자열이다.
- `source.commit`은 확인한 전체 40자리 16진수 commit SHA 또는 `null`. `source.files[].path`는 실행 폴더의 `source/` 아래 상대 경로다. 정확히 한 파일의 `role`이 `skill`, 나머지는 `reference`다. 모든 등록 파일이 B에 제공된다.
- 출처 URL은 HTTP(S)만 허용하며 GitHub 원문 출처의 호스트는 `github.com`, `raw.githubusercontent.com`, `api.github.com` 중 하나다.
- `scope.knowledge_effect`는 지침만 제공하면 `instructions`, 참조 자료의 지식도 제공하면 `instructions_and_reference`다. 추가 자료가 결과에 미칠 수 있는 영향은 평가 범위에 설명한다.
- 요청 모델·추론 값은 문자열 또는 `null`. `baseline_exposure`는 관찰한 대상 노출 상태를 설명하는 문자열이며 미확인일 때 `unknown`으로 둔다.
- `cases`는 Q1, Q2, Q3를 이 순서로 정확히 한 번씩 포함한다. `kind`는 Q1=`representative`, Q2=`constraints`, Q3=`insufficient_information`으로 고정하며 준비 단계에서 대응을 검증한다. `input`은 고정한 공통 자료 문자열이다.
- 기준 ID는 해당 질문 안에서 유일하다. `criteria[].verification`은 입력 대조를 위한 `input`, 평가자의 판단을 위한 `interpretive`를 구분한다. 이 필드는 자식에게 전달되지 않는 사전 기준이다.
- `why`와 `criteria`는 메인 전용이다. 자식에게 필요한 사용자 요구사항은 `prompt`에 표현한다.

## 2. 자식의 answers.json

각 자식은 자기 파일 하나만 쓴다. 다음은 `baseline/answers.json` 예시다.

```json
{
  "answers": [
    {"case_id": "Q1", "text": "행사는 11월 20일입니다."},
    {"case_id": "Q2", "text": "가격은 3만원입니다. 신청 기간은 11월 18일부터 19일까지입니다."},
    {"case_id": "Q3", "text": "장소는 확인되지 않았습니다."}
  ]
}
```

`treatment/answers.json` 예시:

```json
{
  "answers": [
    {"case_id": "Q1", "text": "행사는 11월 20일입니다."},
    {"case_id": "Q2", "text": "가격은 3만원입니다. 신청 기간은 11월 18일부터 19일까지입니다."},
    {"case_id": "Q3", "text": "장소는 확인되지 않았습니다."}
  ]
}
```

`text` 문자열이 정식 원문이다. 메인이 교정·잘라내기·다른 메시지에서 복원하지 않는다. `completed` arm에는 Q1~Q3가 정확히 한 번씩 필요하며 빈 문자열이나 공백뿐인 답변은 거부한다. `partial` 또는 `failed` arm의 빈 문자열·공백뿐인 `text`는 해당 문항만 미수집으로 처리하고 다른 문항은 보존한다. 이때 원본 JSON의 바이트는 변경하지 않는다. 잘못된 JSON 파일도 실패 기록과 함께 바이트 그대로 보존한다.

## 3. execution.json

메인이 두 실행의 관찰값을 기록한 뒤 `--capture`를 호출한다.

```json
{
  "arms": {
    "baseline": {
      "agent_id": "/root/response_a",
      "status": "completed",
      "started_at": null,
      "finished_at": null,
      "error": null,
      "observations": [],
      "usage": {"input_tokens": null, "output_tokens": null}
    },
    "treatment": {
      "agent_id": "/root/response_b",
      "status": "completed",
      "started_at": null,
      "finished_at": null,
      "error": null,
      "observations": [],
      "usage": {"input_tokens": null, "output_tokens": null}
    }
  },
  "application": {
    "status": "provided",
    "note": "형식 예시: B에 고정 원문 패킷 전체를 제공한 실행 기록이 있다."
  },
  "integrity": {
    "status": "limited_visibility",
    "notes": ["전역 지침 주입과 공유 파일 접근을 완전히 확인하지 못했다."]
  }
}
```

- `arms`는 `baseline`, `treatment`를 갖는다. `status`는 `completed / partial / failed`. 두 `agent_id`가 존재하면 서로 달라야 한다. 생성 실패로 ID가 없으면 `agent_id: null`, `status: "failed"`로 기록한다.
- 미확인 시간은 `null`, 확인한 시간은 ISO 8601. 미확인 사용량은 `null`, 확인한 사용량은 음수가 아닌 정수다. `error`는 오류 설명 문자열 또는 `null`, `observations`는 관찰 기록 문자열 목록이다.
- `application.status`는 `provided / partially_provided / not_verified`다. 패킷 제공은 실제 지침 이행과 구분한다.
- `integrity.status`는 `observed_no_contamination / limited_visibility / contaminated`다. 관찰한 범위와 미확인 범위를 `notes`에 적는다.

## 4. result.json — 메인만 작성

아래는 세 기준을 모두 포함하며 앞의 응답 파일과 인용 위치가 일치하는 예시다. 같은 답변이므로 추가 효과가 관찰되지 않았다고 기록했다. 실제 결과의 판정은 메인이 사전 기준과 원문으로 결정한다.

```json
{
  "schema_version": 1,
  "evaluated_by": {"role": "main", "agent_id": "/root", "blind": false},
  "summary": "형식 예시의 세 기준에서 두 답변은 같았다. 이번 한 실행에서는 대상 지침의 추가 이익이 관찰되지 않았다.",
  "case_summaries": {
    "Q1": "양쪽이 제공된 행사 날짜를 정확히 전달했다.",
    "Q2": "양쪽이 두 정보와 두 문장 조건을 충족했다.",
    "Q3": "양쪽이 누락된 장소를 지어내지 않았다."
  },
  "comparisons": [
    {
      "case_id": "Q1",
      "criterion_id": "C1",
      "verdict": "unchanged",
      "a": {"kind": "quote", "start": 4, "end": 11, "text": "11월 20일"},
      "b": {"kind": "quote", "start": 4, "end": 11, "text": "11월 20일"},
      "observed": "양쪽이 같은 날짜를 제시했다.",
      "rationale": "양쪽 모두 입력의 날짜와 일치하여 이 기준의 차이가 없다.",
      "verification": {"kind": "input", "detail": "Q1 입력의 행사 날짜와 대조했다.", "sources": []},
      "skill_links": [{"file": "source/SKILL.md", "start_line": 1, "end_line": 1}]
    },
    {
      "case_id": "Q2",
      "criterion_id": "C1",
      "verdict": "unchanged",
      "a": {"kind": "quote", "start": 0, "end": 38, "text": "가격은 3만원입니다. 신청 기간은 11월 18일부터 19일까지입니다."},
      "b": {"kind": "quote", "start": 0, "end": 38, "text": "가격은 3만원입니다. 신청 기간은 11월 18일부터 19일까지입니다."},
      "observed": "양쪽이 같은 두 문장으로 가격과 신청 기간을 전달했다.",
      "rationale": "두 정보가 입력과 일치하고 두 문장 조건도 충족하여 차이가 없다.",
      "verification": {"kind": "input", "detail": "Q2 입력의 가격·신청 기간과 요청의 두 문장 조건을 대조했다.", "sources": []},
      "skill_links": [{"file": "source/SKILL.md", "start_line": 1, "end_line": 1}]
    },
    {
      "case_id": "Q3",
      "criterion_id": "C1",
      "verdict": "unchanged",
      "a": {"kind": "quote", "start": 0, "end": 15, "text": "장소는 확인되지 않았습니다."},
      "b": {"kind": "quote", "start": 0, "end": 15, "text": "장소는 확인되지 않았습니다."},
      "observed": "양쪽이 장소의 확인 불가를 명시했다.",
      "rationale": "입력에 없는 장소를 양쪽 모두 만들어내지 않아 차이가 없다.",
      "verification": {"kind": "input", "detail": "Q3 입력에는 장소 정보가 없다는 사실과 대조했다.", "sources": []},
      "skill_links": [{"file": "source/SKILL.md", "start_line": 1, "end_line": 1}]
    }
  ],
  "redactions": []
}
```

### 판정과 검증

`evaluated_by.role`은 `main`, `blind`는 `false`다. `agent_id`는 실제 메인의 ID이며 자식 ID와 같으면 검증 실패다. 이 필드는 자기 기록이며 실행 주체의 암호학적 증명은 아니다.

`comparisons`는 사전 등록한 모든 `(case_id, criterion_id)`를 정확히 한 번 포함한다. `case_summaries`에는 Q1~Q3가 모두 필요하다. `verdict`는 다음 네 값이다.

| 값 | HTML 표시 | 의미 |
|---|---|---|
| `improved` | 개선 | B가 해당 기준에서 더 나음 |
| `worsened` | 악화 | B가 해당 기준에서 더 불리함 |
| `unchanged` | 차이 없음 | 해당 기준의 차이가 관찰되지 않음 |
| `undetermined` | 판단 불가 | 필요한 근거 또는 유효한 비교 조건이 부족함 |

답변이 없는 문항은 판정을 `undetermined`로 기록한다. 어느 arm의 `status`가 `failed`이거나, `integrity.status`가 `contaminated`이거나, `application.status`가 `not_verified`이면 전체 판정이 `undetermined`여야 한다. `partial`은 존재하는 문항을 비교할 수 있다. 검증기는 잘못된 판정을 다른 값으로 바꾸지 않고 오류를 반환한다.

`verification.kind`는 `input / interpretation / unverified`다. 사전 기준의 `interpretive`와 결과의 `interpretation`은 필드별 값이 다르다. `detail`은 대조한 자료 또는 해석 근거를 설명한다. `sources`는 `{"title": "출처 제목", "url": "https://example.com/source"}` 목록이며 URL은 HTTP(S)만 허용한다. 실제 출처가 없으면 빈 목록을 사용한다. `unverified`인 기준의 판정은 반드시 `undetermined`여야 한다. 다른 판정이면 코드가 자동 변경하지 않고 메인이 수정해야 할 검증 오류를 반환한다.

`skill_links`는 등록된 원문 파일의 실제 줄 범위를 가리킨다. `start_line`과 `end_line`은 1부터 시작하고 양끝을 포함한다. 연결할 지침이 없으면 빈 목록을 사용한다. 원문 전체가 아니라 연결한 줄의 발췌만 HTML에 표시된다.

### 근거 a/b의 세 가지 형태

인용은 Python 문자열의 **0부터 시작하는 code point 인덱스, 끝 제외**다. UTF-8 바이트 수나 JavaScript UTF-16 인덱스가 아니다. `text`는 실제 `answer[start:end]`와 정확히 같아야 하며 빈 인용은 허용하지 않는다.

```json
{"kind": "quote", "start": 4, "end": 11, "text": "11월 20일"}
```

해당 내용이 답변 전체에 없다고 메인이 판단한 경우:

```json
{"kind": "absence", "scope": "entire_answer", "note": "답변 전체를 확인했으나 신청 기간이 없다."}
```

부재의 의미 판단은 메인 책임이다. 검증기는 전체 답변이 존재하고 검사 범위를 명시했는지만 확인한다.

정식 답변을 수집하지 못한 경우:

```json
{"kind": "unavailable", "reason": "자식이 Q3 정식 답변을 제출하지 못했다."}
```

`unavailable`은 해당 답변이 없거나 `partial`·`failed`에서 빈 문자열·공백뿐이라 미수집 처리된 문항에 허용한다. 내용이 있는 답변을 빈약하다는 이유로 대신 표시하는 용도가 아니다.

### 인증정보 표시 가림

실제 인증 비밀이 확인됐을 때만 해당 원문 구간을 아래와 같이 기록한다.

```json
{"arm": "baseline", "case_id": "Q1", "start": 1, "end": 5, "reason": "인증정보"}
```

이는 `redactions` 배열 원소다. 문자 위치 규칙은 인용과 같고 같은 답변의 구간끼리 겹치지 않는다. 원본 JSON은 보존하고 HTML 표시 사본과 겹친 인용만 `[인증정보 가림]`으로 치환한다. 원문 교정이나 일반 개인정보 편집 기능이 아니다.

## 스크립트 경계

`prepare_run.py`는 필수 입력과 원문을 검증하고 두 프롬프트·해시 잠금을 만든다. `--capture`는 실행 기록과 제출 파일을 고정한다. 기존 잠금은 덮어쓰지 않는다.

`render_report.py`는 `report_lib.load_run()`의 검증된 데이터만 사용한다. 근거 위치·파일 해시·스키마 오류가 있으면 HTML을 생성하지 않는다. 검증과 UTF-8 인코딩을 마친 HTML을 임시 파일에 작성한 뒤 출력 경로를 원자적으로 교체하며, 실패 시 기존 보고서는 보존한다. 스크립트에는 네트워크·모델 호출·품질 평가 기능이 없으며, 메인의 분석이 옳은지는 메인이 직접 확인한다.

## GitHub 원문 바이트 검증

`source.files`의 각 항목에 선택적으로 `blob_sha`(40자리 Git blob SHA)를 넣을 수 있다. GitHub 파일 조회에서 받은 값이 있으면 기록한다. 준비 단계는 파일 바이트로 Git blob 해시를 계산해 불일치를 거부한다. 저장소 commit과 파일 blob SHA는 서로 다른 값이다.


## 짧은 소개와 주요 관찰: result.presentation

신규 실행은 메인이 다음 객체를 추가한다. 기존 실행 호환을 위해 검증기는 이 필드가 없는 과거 결과도 허용한다. 렌더러가 대신 요약하거나 판정을 만들지 않는다.

```json
{
  "what": "이 스킬이 하는 일을 쉬운 한 문장으로 설명",
  "input": "사용자가 주는 입력",
  "process": "스킬이 처리하는 핵심 동작",
  "output": "사용자가 받는 결과물",
  "takeaway": "이번 실제 실행에서 관찰한 핵심 결론",
  "case_labels": {"Q1": "대표 과제 이름", "Q2": "조건 과제 이름", "Q3": "정보 부족 과제 이름"},
  "highlights": [
    {"case_id": "Q1", "criterion_id": "C1", "title": "핵심 관찰 제목", "detail": "실제 원문과 기존 비교가 뒷받침하는 짧은 설명"}
  ]
}
```

글자 수 상한은 `what` 140, `input`·`output` 각 100, `process`·`takeaway` 각 180, 문항 이름 각 50이다. 강조 항목은 최대 3개이며 제목 80·설명 220자 이내다. 상한까지 채울 필요는 없다. 각 강조 항목은 이미 존재하는 고유한 비교를 가리키며 판정은 원래 비교 값을 표시한다. 상세 `summary`·`case_summaries`·`comparisons`는 그대로 보존한다.

## 실제 응답 캡처

`capture_responses.py`는 정식 답변의 표시 화면과 촬영 계획을 `screenshots/`에 만든다. 브라우저로 촬영한 PNG/JPEG를 등록하면 `screenshots/manifest.json`에 출처와 해시가 기록된다. 보고서에는 검증한 이미지 바이트를 data URI로 내장하므로 HTML만 복사해도 이미지를 볼 수 있다. 원문은 접힌 텍스트 영역에도 남긴다.

이미지 등록은 실제 촬영 뒤 수행한다. 오래된 답변·가림·뷰·이미지와의 해시 불일치는 오류이며, 캡처 미수집은 원문 대안과 함께 표시한다. 화면은 ‘제출 원문을 표시한 화면’이라고 설명하며 원본 채팅 UI로 위장하지 않는다. 등록 필드는 도구가 기록하고 메인이 촬영 여부와 원문 일치를 직접 확인한다.
