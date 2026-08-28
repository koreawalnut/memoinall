"""텍스트 정규화 · 한글 n-gram · 청킹.

한글은 조사가 붙어 다녀서 공백 토크나이저(FTS5 unicode61)로는
'회의록을' 이 저장돼 있을 때 '회의록' 으로 못 찾는다.
그래서 CJK 구간만 문자 2-gram 으로 펼쳐 별도 컬럼에 색인하고,
질의도 같은 방식으로 펼쳐서 매칭한다.
"""

from __future__ import annotations

import re
import unicodedata

CJK_RE = re.compile(r"[가-힣぀-ヿ一-鿿]+")
LATIN_RE = re.compile(r"[A-Za-z0-9_]+")
NGRAM_N = 2


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").strip()


def ngrams(text: str, n: int = NGRAM_N) -> list[str]:
    """CJK 구간은 n-gram 으로, 라틴/숫자는 단어 그대로 반환."""
    out: list[str] = []
    text = normalize(text).lower()
    for run in CJK_RE.findall(text):
        if len(run) <= n:
            out.append(run)
        else:
            out.extend(run[i : i + n] for i in range(len(run) - n + 1))
    out.extend(LATIN_RE.findall(text))
    return out


def ngram_index_text(text: str) -> str:
    """FTS 색인용 문자열. 중복은 남겨둬야 빈도 기반 랭킹이 산다."""
    return " ".join(ngrams(text))


def ngram_query(text: str, op: str = "AND") -> str:
    """FTS5 MATCH 식.

    AND 는 정확도용(짧은 질의), OR 는 재현율용(자연어 질의) 이다.
    "개발 속도를 늦추는 병목" 같은 문장은 모든 n-gram 을 AND 하면 반드시 0건이라,
    검색 쪽에서 AND → OR 순으로 낙하시킨다.
    """
    toks = ngrams(text)
    if not toks:
        return ""
    seen: list[str] = []
    for t in toks:
        if t not in seen:
            seen.append(t)
    return f" {op} ".join(f'"{t}"' for t in seen)


_SPLIT_RE = re.compile(r"\n\s*\n")


def chunk(text: str, target: int, hard_max: int) -> list[str]:
    """빈 줄 → 줄 → 문장 순으로 쪼개 target 크기 근처로 뭉친다."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= hard_max:
        return [text]

    units: list[str] = []
    for block in _SPLIT_RE.split(text):
        block = block.strip()
        if not block:
            continue
        if len(block) <= hard_max:
            units.append(block)
            continue
        for line in block.split("\n"):
            line = line.strip()
            if not line:
                continue
            if len(line) <= hard_max:
                units.append(line)
            else:
                units.extend(_split_sentences(line, hard_max))

    chunks: list[str] = []
    buf = ""
    for unit in units:
        candidate = f"{buf}\n{unit}" if buf else unit
        if len(candidate) > target and buf:
            chunks.append(buf)
            buf = unit
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks


_SENT_RE = re.compile(r"(?<=[.!?。！？])\s+|(?<=다\.)\s*")


def sentences(line: str) -> list[str]:
    """한 줄을 문장 단위로. 규칙 추출이 줄 전체를 통째로 삼키지 않게 한다."""
    return [p.strip() for p in _SENT_RE.split(line) if p and p.strip()]


def _split_sentences(line: str, hard_max: int) -> list[str]:
    parts = [p.strip() for p in _SENT_RE.split(line) if p and p.strip()]
    out: list[str] = []
    for part in parts:
        while len(part) > hard_max:
            out.append(part[:hard_max])
            part = part[hard_max:]
        if part:
            out.append(part)
    return out or [line[:hard_max]]


def snippet(text: str, query: str, width: int = 220) -> str:
    """질의어가 처음 등장하는 근처를 잘라 미리보기로."""
    text = text.replace("\n", " ").strip()
    if len(text) <= width:
        return text
    toks = [t for t in ngrams(query) if len(t) >= 2]
    low = text.lower()
    pos = -1
    for t in toks:
        pos = low.find(t)
        if pos >= 0:
            break
    if pos < 0:
        return text[:width] + "…"
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


# 목록/체크박스/제목 표시만 걷어낸다. 문자 단위로 lstrip 하면
# "[#100] 이슈" 같은 제목이 "100] 이슈" 로 잘려 나간다(Redmine 이슈에서 실제로 겪음).
_TITLE_MARKERS = re.compile(r"^(?:[-*•]\s+|\[[ xX]\]\s*|#{1,6}\s+)+")


def title_from(text: str, limit: int = 60) -> str:
    for line in (text or "").splitlines():
        line = _TITLE_MARKERS.sub("", line.strip()).strip()
        if line:
            return line[:limit] + ("…" if len(line) > limit else "")
    return "(빈 메모)"
