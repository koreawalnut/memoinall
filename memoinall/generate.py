"""지시사항 → 문서 검색 → 결과물 생성.

`context.py` 가 "질문 하나 → 근거 묶음"이라면, 여기는 한 단계 위다.

문제: 사람이 쓰는 지시사항은 검색어가 아니다.
    "2025년 AX 세미나 내용으로 교육과정 기획안 초안 써줘"
    → 이걸 그대로 벡터 검색에 넣으면 '초안', '써줘' 같은 노이즈가 섞인다.

그래서 세 단계로 나눈다.
    1) 지시사항에서 **검색 질의 여러 개**를 뽑는다 (서로 다른 각도로).
       한 질의로는 반드시 놓치는 게 생긴다.
    2) 각 질의로 하이브리드 검색 → RRF 로 합친다.
    3) 토큰 예산 안에서 근거를 채우고, 지시사항 + 출력형식과 함께 생성한다.

1)은 LLM 이 있으면 LLM 이, 없으면 규칙으로 한다. 어느 쪽이든 사용자가 질의를
직접 보고 고칠 수 있게 결과에 담아 돌려준다 — 검색이 왜 그렇게 됐는지
보이지 않으면 결과물을 신뢰할 수 없다.
"""

from __future__ import annotations

import logging
import re

from . import config, context, llm, search, settings, store

log = logging.getLogger(__name__)

# 출력 형식 프리셋. 지시사항만으로는 형태가 안 잡히는 경우가 많아서 고른다.
FORMATS: dict[str, dict[str, str]] = {
    "auto": {
        "label": "자동",
        "hint": "내용에 맞는 형식을 알아서 고르세요.",
    },
    "brief": {
        "label": "브리핑",
        "hint": "핵심 3~5줄 → 세부 항목 순서로. 각 줄은 한 문장. 결론을 먼저 쓰세요.",
    },
    "report": {
        "label": "보고서",
        "hint": "배경 · 현황 · 쟁점 · 제안 순의 마크다운 보고서. 각 절은 짧게, 근거를 붙여서.",
    },
    "outline": {
        "label": "개요/목차",
        "hint": "계층형 목차만 쓰세요. 각 항목 뒤에 한 줄 설명. 산문 단락은 쓰지 마세요.",
    },
    "list": {
        "label": "목록 정리",
        "hint": "항목 목록으로만. 중복은 합치고, 비슷한 것끼리 묶어 소제목을 다세요.",
    },
    "email": {
        "label": "이메일 초안",
        "hint": "제목 한 줄 + 본문. 존댓말, 5문장 이내 본문. 요청사항을 마지막에 명확히.",
    },
    "qna": {
        "label": "질문에 답하기",
        "hint": "질문에 직접 답하세요. 근거가 부족하면 부족하다고 먼저 밝히세요.",
    },
}

SYSTEM = (
    "너는 사용자의 개인 업무 메모를 재료로 결과물을 만드는 조수다.\n"
    "\n"
    "규칙:\n"
    "1. 아래 '근거 메모'에 있는 내용만 사실로 사용한다. 없는 사실을 지어내지 않는다.\n"
    "2. 메모에서 가져온 내용에는 [M{id}] 로 출처를 붙인다.\n"
    "3. 지시사항을 수행하는 데 근거가 부족하면, 결과물 앞에 '## 근거가 부족한 부분'\n"
    "   절을 만들어 무엇이 없는지 먼저 밝힌 뒤 가능한 범위까지 작성한다.\n"
    "4. 메모는 사용자가 급하게 적은 것이라 오탈자·비문·중복이 있다. 의미를 살려 정리하되\n"
    "   내용을 바꾸지 않는다.\n"
    "5. 한국어로, 지시사항이 요구하는 형식으로만 출력한다. 서두 인사나 메타 설명은 쓰지 않는다."
)

QUERY_SYSTEM = (
    "너는 검색 질의 생성기다. 사용자의 지시사항을 보고, 개인 메모 데이터베이스에서\n"
    "필요한 자료를 찾기 위한 서로 다른 각도의 검색 질의를 만든다.\n"
    "\n"
    "- 각 질의는 서로 다른 측면을 노려야 한다(주제어 / 관련 인물·조직 / 배경이나 문제상황).\n"
    "- '정리해줘', '초안', '써줘' 같은 지시 동사는 질의에 넣지 않는다.\n"
    "- 질의는 짧은 한국어 명사구나 서술구로 쓴다."
)

QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {"type": "array", "items": {"type": "string"}},
        "topic": {"type": "string"},
    },
    "required": ["queries", "topic"],
    "additionalProperties": False,
}

# 지시 동사·요청 어미. 검색어에서 빼야 하는 것들.
INSTRUCTION_NOISE = re.compile(
    r"(정리|요약|작성|초안|개요|목차|보고서|브리핑|이메일|메일|만들|써|쓰|뽑|알려|찾아|보여|"
    r"해줘|해 줘|해주세요|해줄래|주세요|줘|바랍니다|부탁|필요해|싶어|싶다|하자|합시다)\S*"
)
PARTICLES = re.compile(r"(으로|로서|로써|에서|에게|한테|까지|부터|보다|처럼|같이|대해|관해|관련)\S*$")


def derive_queries(instruction: str, count: int | None = None) -> dict:
    """지시사항 → 검색 질의 목록. LLM 이 있으면 LLM, 없으면 규칙."""
    count = count or settings.get_int("gen.query_count", 3)
    instruction = (instruction or "").strip()
    if not instruction:
        return {"queries": [], "topic": "", "method": "none"}

    if llm.available():
        try:
            data = llm.complete_json(
                QUERY_SYSTEM,
                f"지시사항: {instruction}\n\n서로 다른 각도의 검색 질의를 {count}개 만드세요.",
                QUERY_SCHEMA,
                max_tokens=900,
            )
            queries = [str(q).strip() for q in data.get("queries", []) if str(q).strip()][:count]
            if queries and not _degenerate(queries, instruction):
                return {"queries": queries, "topic": str(data.get("topic", ""))[:100], "method": "llm"}
            if queries:
                log.info("LLM 질의가 부적합해 규칙 기반으로 대체: %s", queries)
                return {**_heuristic_queries(instruction, count), "method": "rule-llm거부"}
        except Exception:
            log.info("질의 생성 실패 — 규칙 기반으로 대체합니다", exc_info=True)

    return {**_heuristic_queries(instruction, count), "method": "rule"}


PLACEHOLDER_RE = re.compile(r"^[A-Z][A-Z0-9_]{3,}$")
HANGUL_RE = re.compile(r"[가-힣]")
HANJA_RE = re.compile(r"[一-鿿]")


def _degenerate(queries: list[str], instruction: str) -> bool:
    """LLM 이 낸 질의가 검색어로 쓸 수 없는 상태인지 본다.

    작은 모델은 스키마만 맞추고 내용은 'DOCUMENT_TITLE' 같은 자리표시자로
    채우는 일이 있다. 구조가 맞으니 예외는 안 나고, 그대로 검색하면 엉뚱한
    메모가 근거로 들어간다. 그런 결과는 규칙 기반으로 되돌리는 편이 낫다.
    """
    if any(PLACEHOLDER_RE.match(q.replace(" ", "")) for q in queries):
        return True
    # 한국어 지시사항인데 질의에 한글이 하나도 없으면 번역/치환 사고다.
    if HANGUL_RE.search(instruction) and not any(HANGUL_RE.search(q) for q in queries):
        return True
    # 지시사항에 없던 한자가 질의에 튀어나오면 모델이 언어를 섞은 것이다.
    # (실측: qwen3.5:2b 가 'AI 개발자 회고록摘抄' 같은 질의를 냈다)
    if not HANJA_RE.search(instruction) and any(HANJA_RE.search(q) for q in queries):
        return True
    # 지시사항과 글자 2-gram 이 전혀 안 겹치면 딴 얘기를 하고 있는 것이다.
    from .textutil import ngrams

    source = set(ngrams(instruction))
    return source and not any(set(ngrams(q)) & source for q in queries)


def _heuristic_queries(instruction: str, count: int) -> dict:
    """LLM 없이도 쓸 만한 질의를 만든다.

    지시 동사를 걷어내고 내용어만 남긴다. 절 단위로도 하나씩 뽑아
    서로 다른 각도를 흉내낸다.
    """
    cleaned = INSTRUCTION_NOISE.sub(" ", instruction)
    cleaned = re.sub(r"[.,!?~·…]+", " ", cleaned)
    words = [PARTICLES.sub("", w) for w in cleaned.split()]
    words = [w for w in words if len(w) >= 2]

    queries: list[str] = []

    def add(q: str) -> None:
        """겹치는 질의는 버린다 — 같은 검색을 두 번 하는 셈이기 때문.

        단 첫 질의(지시사항 전체)는 의도적으로 나머지의 상위집합이므로 비교에서 뺀다.
        """
        q = q.strip()
        if not q or q in queries:
            return
        new = set(q.split())
        for existing in queries[1:]:
            old = set(existing.split())
            if new <= old or old <= new:
                return
        queries.append(q)

    if words:
        queries.append(" ".join(words[:8]))  # 1) 전체 의도

    # 2) 절이 여럿이면 절마다 하나씩 — 진짜로 다른 각도다.
    clauses = [c for c in re.split(r"[,\n]|그리고|및", instruction) if c.strip()]
    if len(clauses) >= 2:
        for clause in clauses:
            clause = INSTRUCTION_NOISE.sub(" ", clause).strip()
            clause_words = [PARTICLES.sub("", w) for w in clause.split() if len(w) >= 2]
            if len(clause_words) >= 2:
                add(" ".join(clause_words[:6]))

    # 3) 절이 하나뿐이면 내용어를 앞/뒤로 갈라 겹치지 않는 두 각도를 만든다.
    #    앞쪽은 보통 주제어("연수원 AX 세미나"), 뒤쪽은 산출물("교육과정 기획안").
    if len(queries) < count and len(words) >= 4:
        half = max(2, len(words) // 2)
        add(" ".join(words[:half]))
        add(" ".join(words[half:][:6]))

    if not queries:
        queries.append(instruction[:60])
    return {"queries": queries[:count], "topic": queries[0][:100]}


def multi_search(
    queries: list[str],
    *,
    per_query: int = 12,
    limit: int = 20,
    tag: str | None = None,
    tags: list[str] | None = None,
    person: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """여러 질의 결과를 RRF 로 합친다.

    질의마다 순위가 따로 나오므로, 여러 질의에서 공통으로 상위권인 메모가
    자연히 위로 올라온다. 한 질의에만 강하게 걸린 메모도 살아남는다.
    """
    rankings: list[list[int]] = []
    hits_by_id: dict[int, dict] = {}
    matched_by: dict[int, list[str]] = {}

    for q in queries:
        hits = search.search(q, limit=per_query, tag=tag, tags=tags, person=person,
                             since=since, until=until)
        rankings.append([h["id"] for h in hits])
        for h in hits:
            hits_by_id.setdefault(h["id"], h)
            matched_by.setdefault(h["id"], []).append(q)

    if not hits_by_id:
        return []

    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, memo_id in enumerate(ranking):
            scores[memo_id] = scores.get(memo_id, 0.0) + 1.0 / (config.RRF_K + rank + 1)

    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    out = []
    for memo_id, score in ordered[:limit]:
        memo = dict(hits_by_id[memo_id])
        memo["score"] = round(score, 6)
        memo["matched_queries"] = matched_by[memo_id]
        out.append(memo)
    return out


def build_pack(
    instruction: str,
    *,
    queries: list[str] | None = None,
    budget_tokens: int | None = None,
    limit: int = 20,
    fmt: str = "auto",
    tag: str | None = None,
    tags: list[str] | None = None,
    person: str | None = None,
    since: str | None = None,
    until: str | None = None,
    full_body: bool = True,
) -> dict:
    """생성 직전까지 — 질의 도출, 검색, 프롬프트 조립. LLM 호출은 안 한다."""
    budget_tokens = budget_tokens or settings.get_int("gen.budget_tokens", 6000)
    derived = {"queries": queries or [], "topic": "", "method": "manual"}
    if not queries:
        derived = derive_queries(instruction)

    hits = multi_search(
        derived["queries"], limit=limit, tag=tag, tags=tags, person=person,
        since=since, until=until,
    )

    included: list[dict] = []
    dropped: list[dict] = []
    used = context.estimate_tokens(instruction) + 250  # 시스템 지시문 여유

    for hit in hits:
        primary = (hit["body"] if full_body else (hit.get("matched_chunk") or hit["body"])).strip()
        secondary = (hit.get("matched_chunk") or "") if full_body else ""
        # 예산은 반드시 지킨다. 긴 메모는 전문 → 검색에 걸린 부분 → 잘라서 순으로 물러난다.
        fitted = context.fit(primary, secondary, budget_tokens - used, force=not included)
        if fitted is None:
            dropped.append(
                {"id": hit["id"], "title": hit["title"], "tokens": context.estimate_tokens(primary)}
            )
            continue
        text, how = fitted
        used += context.estimate_tokens(text) + context.ENTRY_OVERHEAD
        facets = hit.get("facets", {})
        included.append(
            {
                "id": hit["id"],
                "title": hit["title"],
                "created_at": hit["created_at"],
                "text": text,
                "included_as": how,
                "tags": facets.get("tag", []),
                "people": facets.get("person", []),
                "matched_queries": hit.get("matched_queries", []),
            }
        )

    return {
        "instruction": instruction,
        "format": fmt,
        "queries": derived["queries"],
        "query_method": derived["method"],
        "topic": derived.get("topic", ""),
        "budget_tokens": budget_tokens,
        "used_tokens": used,
        "sources": included,
        "dropped": dropped,
        "prompt": render(instruction, fmt, derived["queries"], included, dropped),
    }


def render(instruction: str, fmt: str, queries: list[str], sources: list[dict], dropped: list[dict]) -> str:
    preset = FORMATS.get(fmt, FORMATS["auto"])
    lines = [
        "# 지시사항",
        instruction.strip(),
        "",
        f"# 출력 형식\n{preset['hint']}",
        "",
        "# 근거 메모",
        f"(검색 질의: {', '.join(queries) if queries else '없음'})",
    ]
    if not sources:
        lines.append("\n관련 메모를 찾지 못했습니다. 이 사실을 먼저 밝히고, "
                     "지시사항만으로 쓸 수 있는 부분이 있으면 그 범위를 명시해 작성하세요.")
    for s in sources:
        meta = [s["created_at"][:10]]
        if s["tags"]:
            meta.append(" ".join("#" + t for t in s["tags"]))
        if s["people"]:
            meta.append(" ".join("@" + p for p in s["people"]))
        # 부분만 실린 근거는 그렇다고 밝힌다 — 모델이 '전부 봤다'고 오인하면 안 된다.
        partial = {"chunk": "관련 부분만", "truncated": "앞부분만"}.get(s.get("included_as", "full"))
        if partial:
            meta.append(partial)
        lines.append(f"\n## [M{s['id']}] {s['title']}  ({' · '.join(meta)})")
        lines.append(s["text"])
    if dropped:
        ids = ", ".join(f"M{d['id']}" for d in dropped)
        lines.append(f"\n---\n(토큰 예산 초과로 제외된 관련 메모: {ids})")
    return "\n".join(lines)


def generate(instruction: str, **kwargs) -> dict:
    """전체 파이프라인. 키가 없으면 프롬프트까지만 만들어 돌려준다."""
    effort = kwargs.pop("effort", None)
    max_tokens = kwargs.pop("max_tokens", None)
    pack = build_pack(instruction, **kwargs)

    if not pack["sources"] and not instruction.strip():
        return {**pack, "output": None, "reason": "지시사항이 비어 있습니다."}

    if not llm.available():
        return {**pack, "output": None, "reason": llm._no_key_reason()}

    try:
        output = llm.complete(SYSTEM, pack["prompt"], max_tokens=max_tokens, effort=effort)
        return {**pack, "output": output, "reason": None, "provider": llm.current_label()}
    except llm.ProviderError as exc:
        return {**pack, "output": None, "reason": str(exc)}
    except Exception as exc:
        return {**pack, "output": None, "reason": f"생성 실패: {type(exc).__name__}: {exc}"}


def stats_for_ui() -> dict:
    s = store.stats()
    return {
        "memos": s["memos"],
        "llm_ready": llm.available(),
        "provider": llm.current_label(),
        "formats": FORMATS,
    }
