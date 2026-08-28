# memoinall

**적을 땐 무형식, 꺼낼 땐 구조화.**

업무 중 떠오르는 걸 형식 없이 쭉 적어두면, 나중에 의미 기반으로 찾고
LLM에 그대로 먹일 수 있는 근거 묶음으로 뽑아 쓰는 개인 메모 시스템입니다.

메모 앱이 실패하는 흔한 이유는 **적는 순간에 분류를 강요**하는 것입니다.
그래서 여기서는 구조를 입력이 아니라 **저장 후 자동 파생**으로 넘깁니다.

```
적기 ──▶ 파생 ──▶ 검색 ──▶ 재사용
무형식   자동추출  하이브리드  컨텍스트 팩
```

## 무엇을 해주나

| 계층 | 내용 |
|---|---|
| **Capture** | 텍스트 박스 하나. `Ctrl+Enter` 저장. 형식 규칙 없음 |
| **Derive** | 저장 즉시 백그라운드에서 청킹 → 임베딩 → 태그·사람·할일·결정·질문·날짜·링크 추출 |
| **Retrieve** | 벡터 유사도 + 한글 n-gram FTS 를 RRF 로 융합한 하이브리드 검색 |
| **Reuse** | 지시사항 → 다각도 검색 → **출처 붙은 결과물** 생성 (또는 붙여 쓸 프롬프트) |
| **Organize** | 임베딩 기반 주제 자동 클러스터링, 일/주/월 브리핑, 할일 보드 |

웹 UI 는 8개 탭입니다 — 메모 · 할일 · 주제 · 브리핑 · **생성** · 컨텍스트 팩 · **가져오기** · **설정**.

원문은 절대 건드리지 않습니다. 청크·임베딩·태그·할일은 전부 **언제든 재생성 가능한 캐시**로 취급하므로,
추출 규칙을 고치고 `reindex` 만 돌리면 과거 메모에 새 규칙이 소급 적용됩니다.

## 설치와 실행

**윈도우 앱 (단일 exe)** — 파이썬 설치 없이 `memoinall.exe` 하나만 있으면 됩니다.

```bash
python build_exe.py           # dist/memoinall.exe 생성 (약 60MB)
```

브라우저가 아니라 네이티브 창(Edge WebView2)으로 뜹니다. 서버는 프로세스 안에서
임의의 빈 포트로만 돌고 밖으로 열리지 않습니다. 중복 실행하면 새로 뜨지 않고 알려줍니다.

**개발 중이라면** 파이썬으로 바로 실행할 수도 있습니다.

```bash
pip install -r requirements.txt
python desktop_main.py        # 데스크톱 창 (= python -m memoinall app)
python run.py                 # 웹 서버 (http://127.0.0.1:8787)
```

### exe 를 54MB 로 유지한 방법

그냥 묶으면 torch(496MB) + transformers(94MB) 가 딸려와 **1GB를 넘고**, onefile 은
실행할 때마다 그걸 임시폴더에 풀기 때문에 시작이 수십 초 걸립니다. 그래서:

| | 그대로 묶으면 | 지금 |
|---|---|---|
| 임베딩 실행기 | torch + transformers (590MB) | onnxruntime + tokenizers (49MB) |
| 모델 가중치 | exe 안에 (450MB) | 첫 실행 때 내려받아 `~/.memoinall/models` |
| exe 크기 | 1GB 이상 | **54MB** |
| 모델 로드 | 17.8초 | **1.5초** |

**두 백엔드는 가중치가 같아서 벡터가 완전히 동일합니다** — 코사인 유사도 1.0,
최대 오차 1.2e-7(float32 반올림), 검색 순위 동일. 그래서 웹으로 쓰다가 exe 로 바꿔도
**이미 만들어둔 임베딩을 그대로 씁니다** (재색인 불필요).

모델은 `exe` 가 아니라 사용자 폴더에 있으므로, exe 를 새 버전으로 바꿔도 다시 받지 않습니다.
받는 동안에도 앱은 쓸 수 있고(해시 임베더로 동작), 사이드바에 진행률이 표시됩니다.

첫 실행 때 임베딩 모델(약 450MB)을 내려받습니다. **다운로드를 기다리지 않아도 됩니다** —
그동안은 해시 기반 폴백 임베더로 즉시 동작하고, 모델이 준비되면 자동으로 교체한 뒤
기존 메모를 백그라운드에서 다시 임베딩합니다. 사이드바에 현재 상태가 표시됩니다.

데이터는 `~/.memoinall/memo.db` (SQLite) 하나에 들어갑니다. 그 파일만 백업하면 됩니다.

## 적는 법

형식은 지켜도 되고 안 지켜도 됩니다. 지키면 더 잘 뽑힐 뿐입니다.

```
결제 모듈 타임아웃 계속 남. @김민수 랑 얘기해봐야 함 #결제 #장애
[ ] APM 로그 3일치 뽑기
결정: 재시도는 멱등키 붙여서만
왜 재시도가 중복 승인을 만들까?
```

자동으로 잡히는 것:

- `#태그` · `@사람` · URL
- `2026-08-03`, `8월 3일`, `8/3`, `내일`, `다음주` → 실제 날짜로 환산
- 할일: `[ ]`, `TODO:`, `할일:` 그리고 **한국어 어미** — "정리해야 함", "얘기해봐야 함", "재검토 필요함", "다시 볼 것"
  - 액션 표현은 **문장 끝에 있을 때만** 인정합니다. "처리할 것으로 기대"는 서술이지 할일이 아닙니다
  - **합쇼체는 제외**합니다. "제출해야 합니다"는 가져온 계약서·공고문의 문투이지 내 할일이 아닙니다
- 결정: `결정:` `결론:` 또는 "~하기로 했다"
- 질문: `?` 로 끝나는 문장

한 줄에 사실·할일·질문이 섞여 있으면 **문장 단위로** 갈라서 각각 분류합니다.

## CLI

```bash
python -m memoinall add "PG사 응답 지연이 결제 실패 원인. 재시도 정책 다시 볼 것 #결제"
cat meeting.txt | python -m memoinall add -

python -m memoinall search "결제 관련해서 걱정했던 거"
python -m memoinall todos
python -m memoinall clusters
python -m memoinall digest --period week

# LLM 프롬프트로 바로 뽑아 쓰기
python -m memoinall context "결제 이슈 정리해줘" --budget 3000 | clip
python -m memoinall ask "이번 주에 결정된 게 뭐였지"
```

## 지시사항으로 결과물 만들기 (생성 탭)

이 프로젝트에서 가장 중요한 기능입니다. 지시사항을 적으면 관련 메모를 찾아 근거로 삼고
결과물을 씁니다.

```bash
python -m memoinall generate "연수원 AX 세미나 내용으로 교육과정 기획안 초안 써줘" --format outline
python -m memoinall generate "이번 분기에 결정된 것만 모아서 팀 공유용으로" --format brief
```

**사람이 쓰는 지시사항은 검색어가 아닙니다.** "…기획안 초안 써줘"를 그대로 벡터 검색에
넣으면 '초안', '써줘' 같은 노이즈가 섞입니다. 그래서 세 단계로 나눕니다.

1. **질의 도출** — 지시사항에서 서로 다른 각도의 검색 질의를 여러 개 뽑습니다.
   한 질의로는 반드시 놓치는 게 생깁니다. (API 키가 있으면 LLM 이, 없으면 규칙이)
2. **다중 검색** — 각 질의로 하이브리드 검색 후 RRF 로 합칩니다. 여러 질의에 공통으로
   걸린 메모가 자연히 위로 올라옵니다.
3. **생성** — 토큰 예산 안에서 근거를 채우고 지시사항·출력형식과 함께 씁니다.

**중간 단계를 감추지 않습니다.** 도출된 검색 질의를 화면에 그대로 보여주고,
직접 지우거나 추가할 수 있습니다. 어떤 메모가 어떤 질의에 걸려서 근거가 됐는지도 표시합니다 —
왜 그 근거가 뽑혔는지 안 보이면 결과물을 신뢰할 수 없기 때문입니다.

출력 형식: 자동 · 브리핑 · 보고서 · 개요/목차 · 목록 정리 · 이메일 초안 · 질문에 답하기

### 토큰 예산은 반드시 지킵니다

긴 메모 하나가 예산을 통째로 삼키지 않도록, 근거마다 **전문 → 검색에 걸린 부분만 → 앞부분만**
순으로 물러납니다. 부분만 실린 근거는 프롬프트에 `관련 부분만` / `앞부분만` 이라고 명시해서
모델이 "전부 봤다"고 오해하지 않게 합니다.

> 실측: 19,000자짜리 메모가 섞인 실제 데이터에서 예산 3000 → 사용 2999,
> 근거 12건. (이 처리가 없으면 근거 1건에 10,954토큰으로 터집니다.)

## LLM 설정 (설정 탭)

**Claude · ChatGPT · Gemini · Ollama** 중에서 고릅니다. 프로바이더마다 키·모델·서버주소를
따로 저장하므로, 바꿔 끼워도 다시 입력할 필요가 없습니다.

| 프로바이더 | 접속 방식 | 필요한 것 |
|---|---|---|
| **Claude** (Anthropic) | 네이티브 SDK | `pip install anthropic` + API 키 |
| **ChatGPT** (OpenAI) | OpenAI API | `pip install openai` + API 키 |
| **Gemini** (Google) | Google 의 **OpenAI 호환 엔드포인트** | `pip install openai` + AI Studio 키 |
| **Ollama** (로컬) | Ollama 의 **OpenAI 호환 엔드포인트** | `ollama serve` + `ollama pull <모델>` |

Gemini 와 Ollama 가 둘 다 OpenAI 호환 엔드포인트를 제공하기 때문에, 어댑터는 **두 개**뿐입니다
(Claude 네이티브 + OpenAI 호환). 같은 이유로 **LM Studio · vLLM · Groq · OpenRouter** 는
'ChatGPT' 를 고르고 서버 주소만 바꾸면 그대로 동작합니다.

```bash
python -m memoinall settings                          # 현재 상태
python -m memoinall settings --provider ollama        # 프로바이더 전환
python -m memoinall settings --set llm.openai.api_key=sk-...
python -m memoinall settings --set llm.ollama.model=qwen3.5:2b
python -m memoinall settings --test                   # 연결 테스트
```

- 우선순위는 **설정 > 환경변수 > 기본값** 이고, 각 값이 어디서 왔는지 화면에 표시됩니다
- 키는 마스킹해서만 보여주고 API 로 원문이 나가지 않습니다
- 키는 `~/.memoinall/memo.db` 에 **평문으로** 저장됩니다. 공용 PC 면 환경변수를 쓰세요
- `연결 테스트`는 **저장하기 전 입력값**으로 시험합니다 — 잘못된 키를 저장하고 나서 알게 되면 늦으니까요
- `서버에서 모델 목록 불러오기`는 실제 설치·제공 중인 모델을 가져옵니다 (특히 Ollama)

### 호환성은 어댑터가 알아서 흡수합니다

'OpenAI 호환'의 범위가 서버마다 달라서, 거절당한 파라미터를 하나씩 떨어뜨리며 재시도하고
그 결과를 기억합니다. 사용자가 서버별 차이를 알 필요가 없도록:

- `max_tokens` 를 안 받으면 → `max_completion_tokens` 로 전환
- `reasoning_effort` 를 모르면 → 빼고 재시도
- `response_format: json_schema` 를 모르면 → `json_object` → 프롬프트 지시 순으로 낙하

### 추론 강도 — 로컬 모델은 '추론 끄기'가 기본

`qwen3.5`, `gemma4` 같은 작은 추론 모델은 **추론에만 수천 토큰을 쓰다가 본문을 못 내는** 일이
잦습니다. 실측에서 `qwen3.5:2b` 는 6,000 토큰을 줘도 빈 응답을 냈습니다.
그래서 Ollama 는 기본값이 `추론 끄기`(`reasoning_effort: "none"`)이고, 이걸 켜면 답이 안정적으로 나옵니다.
추론 강도는 프로바이더별로 저장됩니다 (Claude 는 `높음`, Ollama 는 `추론 끄기`).

본문이 비어서 오면 조용히 넘기지 않고 **왜 비었는지 알려줍니다** —
"모델이 추론에만 토큰을 다 써서 답을 내지 못했습니다 (최대 출력 N 토큰)".

### 작은 모델의 한계

작은 로컬 모델은 스키마만 맞추고 내용은 `DOCUMENT_TITLE` 같은 자리표시자로 채우기도 합니다.
그런 질의로 검색하면 엉뚱한 메모가 근거로 들어가므로, **부적합한 질의를 걸러내고 규칙 기반으로
되돌립니다** (한글 지시사항인데 질의에 한글이 없거나, 지시사항과 전혀 안 겹치는 경우 등).

품질 자체는 모델 크기를 넘지 못합니다. 실측상 2B 급 로컬 모델은 요약은 쓸 만하지만
긴 기획안은 산만합니다. 제대로 된 결과물이 필요하면 큰 로컬 모델이나 클라우드 프로바이더를 쓰세요.

**설정이 없어도 전부 동작합니다.** 생성 기능이 결과물 대신 완성된 프롬프트를 돌려주니
원하는 LLM 에 붙여 넣으면 됩니다.

## 기존 메모 가져오기

`가져오기` 탭에서 소스별 가용 여부를 확인하고 미리보기 → 실행할 수 있습니다. CLI 도 동일합니다.

```bash
python -m memoinall import                          # 미리보기 (아무것도 저장 안 함)
python -m memoinall import --commit                 # 실제 저장
python -m memoinall import --source samsung --commit
python -m memoinall import --source files --path ./notes --commit
python -m memoinall import --commit --min-chars 20  # 너무 짧은 메모 제외
```

| 소스 | 대상 | 읽는 위치 |
|---|---|---|
| `sticky` | Windows 스티커 메모 | `%LOCALAPPDATA%\Packages\Microsoft.MicrosoftStickyNotes_*\LocalState\plum.sqlite` |
| `samsung` | Samsung Notes | `...\SAMSUNGELECTRONICSCoLtd.SamsungNotes_*\LocalState\Storage.sqlite` |
| `redmine` | Redmine 이슈·위키·문서·공지 | REST API (주소 + API 키) |
| `files` | `.txt` / `.md` 폴더 | `--path` 로 지정 |

### Redmine

`가져오기` 탭에서 주소와 API 키를 넣습니다. 키는 Redmine 의 **[내 계정]** 화면 오른쪽에서 확인합니다.

```bash
python -m memoinall settings --set import.redmine.url=https://redmine.example.com
python -m memoinall settings --set import.redmine.api_key=...
python -m memoinall import --source redmine --redmine-kinds issues,wiki --commit
```

가져오는 것: **이슈**(제목·트래커·상태·담당·본문, 선택적으로 코멘트) · **위키** · **문서** · **공지**.
프로젝트 식별자와 갱신일(`--redmine-since`)로 좁힐 수 있고, 프로젝트명·트래커는 태그가 됩니다.

- **상한은 종류별로** 적용됩니다. 전체 상한으로 두면 이슈가 한도를 다 먹고 문서·위키가
  0건이 되는데, 사용자는 왜 안 왔는지 알 수가 없습니다
- **오류를 구분해서 알려줍니다** — 401(키) / 403(권한·REST API 비활성) / 404(주소) /
  연결 실패는 사용자가 할 일이 전혀 다릅니다
- 이슈 코멘트는 건별 요청이라 기본은 꺼져 있습니다
- 가져오기 전에 `연결 테스트`로 주소·키를 확인할 수 있습니다 (저장 전 입력값으로)
- API 키는 평문 저장되니 공용 PC 면 `REDMINE_API_KEY` 환경변수를 쓰세요

보장하는 것:

- **원본 불변** — 원본 DB 는 임시 복사본으로만 읽습니다(앱이 켜져 있어도 안전). 쓰기는 절대 하지 않습니다
- **작성 시각 유지** — 임포트 시각이 아니라 원래 작성 시각을 씁니다. 안 그러면 기간 검색·롤업이 무의미해집니다
- **멱등** — `external_id` 로 중복을 막습니다. 몇 번을 돌려도 같은 메모가 두 번 들어가지 않습니다
- **미리보기 기본** — `--commit` 없이는 읽기만 하고 건수·길이 분포·샘플만 보여줍니다

Samsung Notes 는 노트 종류(타이핑/손글씨/PDF)마다 본문이 다른 컬럼에 들어가서
실측 커버리지 순서대로 폴백 체인을 태웁니다. 손글씨 인식 결과와 PDF 텍스트도
가져오며, 각각 `#손글씨` `#PDF` 태그가 붙어 나중에 구분할 수 있습니다.
폴더명도 태그가 되지만 '폴더' 같은 기본 이름은 버립니다.

## AI 자료로 쓰는 법

핵심은 `context` 입니다. 검색 결과를 사람이 읽는 목록이 아니라
**프롬프트에 그대로 붙일 근거 묶음**으로 렌더합니다.

```bash
curl "http://127.0.0.1:8787/api/context.txt?q=결제+이슈&budget=3000"
```

```
다음은 사용자의 개인 업무 메모에서 검색된 근거입니다.
이 근거만 사용해 답하고, 근거에 없는 내용은 추측하지 말고 '메모에 없음'이라고 밝히세요.
문장을 인용할 때는 [M{id}] 형식으로 출처를 표시하세요.

# 질문
결제 이슈

# 근거 메모

## [M3] PG사 응답 지연이 결제 실패 원인 (2026-07-27 · #결제)
...
```

세 가지를 보장합니다.

1. **예산 준수** — 토큰 예산을 넘지 않고, 넘쳐서 제외한 메모는 `dropped` 로 밝힙니다(조용히 자르지 않음)
2. **출처 추적** — 모든 조각에 `[M{id}]` 와 날짜가 붙어 생성물의 근거를 되짚을 수 있습니다
3. **중복 제거** — 같은 메모가 여러 청크로 걸려도 한 번만 인용됩니다

`ANTHROPIC_API_KEY` 를 넣으면 `/api/ask` 와 `/api/digest` 가 답변까지 생성합니다.
**키가 없어도 앱은 완전히 동작합니다** — LLM 호출 대신 프롬프트를 돌려주므로
원하는 도구에 붙여 넣으면 됩니다. LLM은 의존 대상이 아니라 선택적 보강입니다.

## 검색이 동작하는 방식

두 갈래를 [RRF](https://learn.microsoft.com/azure/search/hybrid-search-ranking)로 융합합니다.
어느 한쪽만 쓰면 반드시 새는 구멍이 있기 때문입니다.

- **벡터** — "그때 그 결제 모듈 이슈" 같은 애매한 질의에 강하고 고유명사에 약함
- **n-gram FTS** — 그 반대

한글은 조사가 붙어 다녀서(`회의록을`) 공백 토크나이저로는 `회의록` 을 못 찾습니다.
그래서 CJK 구간을 문자 2-gram 으로 펼쳐 별도 컬럼에 색인합니다.
질의는 먼저 AND 로 걸고, 자연어 문장처럼 AND 가 0건이 되는 경우 OR + BM25 로 낙하시킵니다.

> 측정 결과(8개 메모, 실제 모델): 어휘가 겹치는 현실적 질의는 top-1 9/9.
> 어휘가 전혀 안 겹치는 은유적 질의("돈이 많이 새고 있는 곳" → 서버 비용 메모)는 top-3 5/6.
> 후자를 더 올리려면 아래처럼 더 큰 모델로 바꾸면 됩니다.

## 설정

전부 환경변수입니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MEMOINALL_HOME` | `~/.memoinall` | DB 위치 |
| `MEMOINALL_EMBED_MODEL` | `intfloat/multilingual-e5-small` | 임베딩 모델. `intfloat/multilingual-e5-base` 로 바꾸면 정확도↑ 속도↓ |
| `MEMOINALL_EMBED_BACKEND` | `auto` | `onnx` / `st` / `hash`. exe 는 `onnx` 고정 |
| `MEMOINALL_DISABLE_ST` | — | `1` 이면 모델 없이 해시 임베더만 사용 |
| `MEMOINALL_PORT` | `8787` | 웹 포트 |
| `MEMOINALL_LLM_PROVIDER` | `anthropic` | `anthropic` / `openai` / `gemini` / `ollama` |
| `ANTHROPIC_API_KEY` | — | Claude 키 |
| `OPENAI_API_KEY` | — | ChatGPT 키 |
| `GEMINI_API_KEY` | — | Gemini 키 (Google AI Studio) |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama 서버 주소 |
| `REDMINE_URL` | — | Redmine 루트 주소 |
| `REDMINE_API_KEY` | — | Redmine API 키 |
| `MEMOINALL_LLM_MODEL` | 프로바이더별 | 설정 탭에서 프로바이더별로 지정합니다 |
| `MEMOINALL_LLM_EFFORT` | `high` | `none`~`max`. `none` 은 추론 끄기 |

모델을 바꾸면 청크에 기록된 모델명이 달라져 **자동으로 재임베딩**됩니다.

## 테스트

```bash
python tests/test_memoinall.py          # 코어 50종 (모델 없이, 빠름)
python tests/test_memoinall.py --real   # 실제 임베딩 모델로 의미 검색까지
python tests/test_api.py                # HTTP 라우트 55종
python tests/test_importers.py          # 임포터 35종 (합성 DB + 실데이터 읽기)
python tests/test_generate.py           # 프로바이더·설정·생성 111종 (가짜 SDK 로)
python tests/test_desktop.py            # ONNX 임베더·데스크톱 36종
python tests/test_redmine.py            # Redmine 65종 (가짜 서버를 띄워 실제 소켓으로)
```

`test_generate.py` 는 각 어댑터가 **실제로 어떤 파라미터를 보내는지**까지 검사합니다 —
Claude 에 `temperature` 를 안 보내는지, 서버가 `max_tokens` 를 거부하면
`max_completion_tokens` 로 전환하는지 등.

## 구조

```
memoinall/
  config.py      환경설정
  db.py          SQLite 스키마 (memos / chunks / facets / todos / FTS5)
  textutil.py    한글 n-gram · 문장 분리 · 청킹
  extract.py     규칙 기반 추출 (태그·사람·날짜·할일·결정·질문)
  embed.py       임베딩 (해시 폴백 → 실모델 자동 승격)
  store.py       저장 + 비동기 보강 워커 + 벡터 캐시
  search.py      하이브리드 검색 (벡터 + FTS, RRF)
  context.py     컨텍스트 팩 빌더 + 예산 맞춤(fit)
  generate.py    지시사항 → 다각도 검색 → 결과물  ← 이 프로젝트의 핵심
  organize.py    클러스터링 · 기간 롤업
  settings.py    런타임 설정 (설정 > 환경변수 > 기본값), 프로바이더별 키/모델
  llm.py         프로바이더 선택 + 앱 수준 프롬프트
  providers/     Claude(네이티브) · OpenAI호환(ChatGPT/Gemini/Ollama)
  embed_onnx.py  ONNX 임베더 (torch 불필요 — exe 패키징의 핵심)
  desktop.py     네이티브 창 + 내부 서버 (pywebview)
  api.py         FastAPI 라우트
  cli.py         CLI
  importers/     가져오기 (스티커 메모 / Samsung Notes / Redmine / 텍스트 폴더)
  static/        웹 UI (단일 HTML, 외부 의존성 없음)

desktop_main.py  데스크톱 진입점 (PyInstaller 가 묶는 파일)
memoinall.spec   PyInstaller 스펙 (torch 제외, WebView2 DLL 포함)
build_exe.py     빌드 스크립트
```

## 알려진 한계

- 벡터 검색은 전체 임베딩을 메모리에 올려 완전 탐색합니다. 개인 규모(수만 건)까진 충분하지만
  그 이상이면 ANN 인덱스가 필요합니다.
- 할일 완료 상태는 본문 텍스트로 매칭해 보존합니다. 메모를 수정하며 할일 문장 자체를 바꾸면
  그 항목은 새 할일로 잡힙니다.
- 클러스터 레이블은 가장 흔한 태그를 씁니다. 태그가 하나도 없는 묶음은 대표 메모 제목을 씁니다.
