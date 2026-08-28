"""규칙 기반 구조 추출.

LLM 없이도 메모의 80%는 여기서 건진다. #태그 · @사람 · 링크 · 날짜 · 할일 ·
결정 · 질문. 사용자가 형식을 지키지 않는다는 전제로, 흔한 한국어 업무 표현을
같이 잡는다.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from . import textutil

TAG_RE = re.compile(r"(?:^|\s)#([0-9A-Za-z가-힣_/\-]{1,40})")
PERSON_RE = re.compile(r"(?:^|\s)@([0-9A-Za-z가-힣._\-]{1,40})")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")

ISO_DATE_RE = re.compile(r"\b(20\d{2})[-./](\d{1,2})[-./](\d{1,2})\b")
KO_DATE_RE = re.compile(r"\b(?:(20\d{2})년\s*)?(\d{1,2})월\s*(\d{1,2})일")
SHORT_DATE_RE = re.compile(r"(?<![\d/.])(\d{1,2})/(\d{1,2})(?![\d/])")

RELATIVE_DAYS = {
    "오늘": 0, "내일": 1, "모레": 2, "글피": 3,
    "어제": -1, "그제": -2, "그저께": -2,
    "다음주": 7, "담주": 7, "차주": 7, "지난주": -7, "저번주": -7,
}

# 체크박스/명시적 마커
TODO_MARK_RE = re.compile(r"^\s*(?:[-*•]\s*)?\[( |x|X)\]\s*(.+)$")
TODO_KEYWORD_RE = re.compile(r"^\s*(?:[-*•]\s*)?(?:TODO|todo|To-?do|할\s?일|투두)\s*[:：]?\s*(.+)$")
# 한국어 액션 표현. "~해야 함", "~봐야겠다", "~할 것", "~필요함" 등.
# 어간을 특정하지 않고 "…야 + 종결" 패턴으로 잡아야 '얘기해봐야 함' 같은 변형이 걸린다.
#
# 반드시 문장 끝에 붙어야 한다. 안 그러면 산문이 통째로 할일이 된다 —
# "처리할 것으로 기대", "필요함에 따라 방안을 제시함" 은 할일이 아니라 서술이다.
TODO_TAIL_RE = re.compile(
    # '합니다'체는 일부러 뺐다. 자기 할일을 합쇼체로 적는 사람은 없다 —
    # "제출해야 합니다"는 가져온 문서(계약서·공고문)의 문투이지 내 할일이 아니다.
    r"(?:[가-힣]야\s*(?:한다|함|해|하고|됨|겠|지)"
    r"|하기로"
    r"|필요(?:함|하다|있음)"
    r"|요청\s?(?:드림|함|하기)"
    r"|확인\s?(?:바람|필요)"
    r"|잊지\s?말(?:것|기|자)?)"
    r"\s*$"
)

# 문장 끝의 해시태그·군더더기를 걷어내야 위 앵커가 제대로 걸린다.
TAIL_NOISE_RE = re.compile(r"(?:\s*#[0-9A-Za-z가-힣_/\-]+)*\s*[.!~\s]*$")

# 관형형 '-ㄹ 것' (볼 것 / 할 것 / 만들 것 / 읽을 것). 어간을 나열할 수 없으니
# 앞 음절의 받침이 ㄹ 인지로 판정한다. '이 것 / 그 것' 은 받침이 없어 걸러진다.
ADNOMINAL_RE = re.compile(r"([가-힣])\s?것$")
_RIEUL_FINAL = 8  # 한글 음절 종성 인덱스에서 ㄹ


def _ends_with_action(core: str) -> bool:
    if TODO_TAIL_RE.search(core):
        return True
    m = ADNOMINAL_RE.search(core)
    return bool(m and (ord(m.group(1)) - 0xAC00) % 28 == _RIEUL_FINAL)
DECISION_RE = re.compile(r"^\s*(?:[-*•]\s*)?(?:결정|결론|합의|정함|decision|conclusion)\s*[:：]\s*(.+)$", re.I)
DECISION_TAIL_RE = re.compile(r"(하기로\s?(?:했|함|결정)|결론\s?(?:은|:)|합의(?:했|함))")


def _mk_date(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def extract_dates(text: str, base: datetime) -> list[str]:
    """텍스트 안의 절대/상대 날짜를 ISO 문자열로."""
    found: list[str] = []

    for y, m, d in ISO_DATE_RE.findall(text):
        iso = _mk_date(int(y), int(m), int(d))
        if iso:
            found.append(iso)

    for y, m, d in KO_DATE_RE.findall(text):
        iso = _mk_date(int(y) if y else base.year, int(m), int(d))
        if iso:
            found.append(iso)

    for m, d in SHORT_DATE_RE.findall(text):
        iso = _mk_date(base.year, int(m), int(d))
        if iso:
            found.append(iso)

    for word, delta in RELATIVE_DAYS.items():
        if word in text:
            found.append((base.date() + timedelta(days=delta)).isoformat())

    seen: list[str] = []
    for f in found:
        if f not in seen:
            seen.append(f)
    return seen


def _clean(line: str) -> str:
    return line.strip().strip("-*•").strip()


def extract(text: str, created_at: datetime) -> dict:
    """메모 하나에서 파셋과 할일을 뽑는다."""
    tags = _uniq(TAG_RE.findall(text))
    people = _uniq(PERSON_RE.findall(text))
    links = _uniq(URL_RE.findall(text))
    dates = extract_dates(text, created_at)

    todos: list[dict] = []
    decisions: list[str] = []
    questions: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        mark = TODO_MARK_RE.match(line)
        if mark:
            todos.append({"text": _clean(mark.group(2)), "done": mark.group(1).lower() == "x"})
            continue

        kw = TODO_KEYWORD_RE.match(line)
        if kw:
            todos.append({"text": _clean(kw.group(1)), "done": False})
            continue

        dec = DECISION_RE.match(line)
        if dec:
            decisions.append(_clean(dec.group(1)))
            continue

        # 줄 단위 마커가 없으면 문장 단위로 본다. 한 줄에 사실·할일·질문이
        # 섞여 있을 때 줄 전체가 할일로 둔갑하는 걸 막는다.
        for sent in textutil.sentences(line):
            body = _clean(sent)
            if not body:
                continue
            if body.endswith("?") or body.endswith("？"):
                questions.append(body)
                continue
            core = TAIL_NOISE_RE.sub("", body)  # 앵커 판정용 (저장은 원문 그대로)
            if _ends_with_action(core):
                todos.append({"text": body, "done": False})
            elif DECISION_TAIL_RE.search(body):
                decisions.append(body)

    # 할일에 날짜가 붙어 있으면 마감으로 본다
    for todo in todos:
        due = extract_dates(todo["text"], created_at)
        todo["due"] = due[0] if due else None

    return {
        "tag": tags,
        "person": people,
        "link": links,
        "date": dates,
        "decision": _uniq(decisions),
        "question": _uniq(questions),
        "todos": _dedupe_todos(todos),
    }


def _uniq(items: list[str]) -> list[str]:
    out: list[str] = []
    for i in items:
        i = i.strip()
        if i and i not in out:
            out.append(i)
    return out


def _dedupe_todos(todos: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for t in todos:
        key = t["text"]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out
