"""컨텍스트 팩 — 이 프로젝트의 핵심 산출물.

검색 결과를 사람이 읽는 목록이 아니라 "LLM 프롬프트에 그대로 붙일 근거 묶음"으로
만든다. 조건:
  1) 토큰 예산 안에 들어갈 것 (넘치면 잘라내되 무엇을 잘랐는지 밝힐 것)
  2) 모든 조각에 출처(메모 ID·날짜)가 붙을 것 — 생성물의 근거 추적을 위해
  3) 같은 메모가 중복 인용되지 않을 것
"""

from __future__ import annotations

from . import search, store

# 한국어 혼용 텍스트의 대략적인 토큰 환산. 정확할 필요는 없고, 예산을 넘기지만
# 않으면 된다. 보수적으로 잡는다.
CHARS_PER_TOKEN = 1.8


# 근거 하나에 붙는 머리말(제목·날짜·태그 줄)의 대략적인 비용
ENTRY_OVERHEAD = 30
# 이보다 적게 남았으면 잘라 넣어도 의미가 없다
MIN_SLICE_TOKENS = 60

TRUNCATED_MARK = "\n…(분량이 커서 일부만 실었습니다)"


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def fit(full: str, chunk: str, remaining: int, *, force: bool = False) -> tuple[str, str] | None:
    """남은 예산에 맞춰 근거 하나를 어떤 형태로 실을지 고른다.

    전문 → 검색에 걸린 부분만 → 잘라서, 순으로 물러난다.
    긴 메모 하나가 예산을 통째로 삼키거나(예산 초과), 길다는 이유만으로
    통째로 빠지는(재현율 손실) 두 가지 실패를 모두 막는다.

    반환: (본문, 형태) 또는 못 넣으면 None. 형태는 full | chunk | truncated.
    """
    full, chunk = (full or "").strip(), (chunk or "").strip()
    budget = remaining - ENTRY_OVERHEAD
    if budget <= 0:
        return None

    if full and estimate_tokens(full) <= budget:
        return full, "full"
    if chunk and estimate_tokens(chunk) <= budget:
        return chunk, "chunk"

    # 첫 근거는 비워 둘 수 없으니 잘라서라도 넣는다.
    if not force and budget < MIN_SLICE_TOKENS:
        return None
    source = full or chunk
    if not source:
        return None
    # estimate_tokens 는 int(len/CPT)+1 로 올림하므로, 잘라낼 길이도 그만큼 빼야
    # 결과가 예산을 1토큰 넘기지 않는다.
    keep = int((budget - 1) * CHARS_PER_TOKEN) - len(TRUNCATED_MARK)
    if keep <= 0:
        return None
    sliced = source[:keep].rstrip() + TRUNCATED_MARK
    if estimate_tokens(sliced) > budget:  # 방어: 어떤 경우에도 예산을 넘기지 않는다
        return None
    return sliced, "truncated"


def build(
    query: str,
    *,
    budget_tokens: int = 2000,
    limit: int = 12,
    tag: str | None = None,
    person: str | None = None,
    since: str | None = None,
    until: str | None = None,
    full_body: bool = False,
) -> dict:
    hits = search.search(query, limit=limit, tag=tag, person=person, since=since, until=until)

    included: list[dict] = []
    dropped: list[dict] = []
    used = estimate_tokens(query) + 80  # 헤더/지시문 여유분

    for hit in hits:
        primary = hit["body"] if full_body else (hit.get("matched_chunk") or hit["body"])
        secondary = (hit.get("matched_chunk") or "") if full_body else ""
        fitted = fit(primary, secondary, budget_tokens - used, force=not included)
        if fitted is None:
            dropped.append(
                {"id": hit["id"], "title": hit["title"], "tokens": estimate_tokens(primary)}
            )
            continue
        text, how = fitted
        used += estimate_tokens(text) + ENTRY_OVERHEAD
        included.append(
            {
                "id": hit["id"],
                "title": hit["title"],
                "created_at": hit["created_at"],
                "text": text,
                "included_as": how,
                "tags": hit.get("facets", {}).get("tag", []),
                "people": hit.get("facets", {}).get("person", []),
                "score": hit["score"],
                "why": hit["why"],
            }
        )

    return {
        "query": query,
        "budget_tokens": budget_tokens,
        "used_tokens": used,
        "sources": included,
        "dropped": dropped,
        "prompt": render(query, included, dropped),
    }


def render(query: str, sources: list[dict], dropped: list[dict]) -> str:
    """프롬프트에 그대로 붙이는 텍스트."""
    lines = [
        "다음은 사용자의 개인 업무 메모에서 검색된 근거입니다.",
        "이 근거만 사용해 답하고, 근거에 없는 내용은 추측하지 말고 '메모에 없음'이라고 밝히세요.",
        "문장을 인용할 때는 [M{id}] 형식으로 출처를 표시하세요.",
        "",
        f"# 질문\n{query}",
        "",
        "# 근거 메모",
    ]
    if not sources:
        lines.append("(관련 메모 없음)")
    for s in sources:
        meta = [s["created_at"][:10]]
        if s["tags"]:
            meta.append(" ".join("#" + t for t in s["tags"]))
        if s["people"]:
            meta.append(" ".join("@" + p for p in s["people"]))
        partial = {"chunk": "관련 부분만", "truncated": "앞부분만"}.get(s.get("included_as", "full"))
        if partial:
            meta.append(partial)
        lines.append(f"\n## [M{s['id']}] {s['title']}  ({' · '.join(meta)})")
        lines.append(s["text"])
    if dropped:
        ids = ", ".join(f"M{d['id']}" for d in dropped)
        lines.append(f"\n---\n(토큰 예산 초과로 제외된 관련 메모: {ids})")
    return "\n".join(lines)


def for_memo(memo_id: int, *, budget_tokens: int = 2000) -> dict:
    """특정 메모를 중심으로 한 컨텍스트 팩(주변 맥락 포함)."""
    memo = store.get_memo(memo_id)
    pack = build(memo["body"][:600], budget_tokens=budget_tokens)
    pack["query"] = f"[M{memo_id}] {memo['title']}"
    return pack
