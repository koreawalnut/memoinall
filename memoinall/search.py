"""하이브리드 검색: 벡터 유사도 + n-gram FTS 를 RRF 로 융합.

둘 중 하나만 쓰면 반드시 새는 구멍이 있다.
- 벡터만: "그때 그 결제 모듈 이슈" 같은 애매한 질의는 잘 찾지만, 정확한 고유명사에 약하다.
- FTS만: 고유명사는 정확하지만 "비슷한 얘기"를 못 찾는다.
RRF(Reciprocal Rank Fusion)는 점수 스케일이 다른 두 랭킹을 정규화 없이 합칠 수 있다.
"""

from __future__ import annotations

import numpy as np

from . import config, db, embed, store, textutil


def _vector_hits(query: str, k: int) -> list[tuple[int, float, str]]:
    matrix, meta = store.vector_index()
    if matrix.shape[0] == 0:
        return []
    qvec, _ = embed.encode([query], is_query=True)
    sims = matrix @ qvec[0]
    k = min(k, sims.shape[0])
    idx = np.argpartition(-sims, k - 1)[:k]
    idx = idx[np.argsort(-sims[idx])]
    return [(meta[i][1], float(sims[i]), meta[i][2]) for i in idx]


def _match(expr: str, k: int) -> list[int]:
    if not expr:
        return []
    try:
        rows = db.query(
            "SELECT rowid, bm25(memo_fts, 1.0, 0.5) AS score FROM memo_fts "
            "WHERE memo_fts MATCH ? ORDER BY score LIMIT ?",
            (expr, k),
        )
    except Exception:
        return []  # 구문 오류나는 질의는 벡터 쪽에 맡긴다
    return [int(r["rowid"]) for r in rows]


def _fts_hits(query: str, k: int) -> list[int]:
    """AND 로 먼저 찾고, 빈약하면 OR 로 넓힌다.

    AND 결과를 앞에 두는 게 핵심이다. 전부 포함한 메모가 부분 일치보다
    항상 위에 오도록 순서로 우선순위를 표현한다(RRF 는 순위만 본다).
    """
    strict = _match(textutil.ngram_query(query, "AND"), k)
    if len(strict) >= max(3, k // 10):
        return strict
    loose = _match(textutil.ngram_query(query, "OR"), k)
    seen = set(strict)
    return strict + [mid for mid in loose if mid not in seen]


def _rrf(rankings: list[list[int]], weights: list[float]) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking, weight in zip(rankings, weights):
        for rank, memo_id in enumerate(ranking):
            scores[memo_id] = scores.get(memo_id, 0.0) + weight / (config.RRF_K + rank + 1)
    return scores


def search(
    query: str,
    *,
    limit: int = 20,
    tag: str | None = None,
    person: str | None = None,
    since: str | None = None,
    until: str | None = None,
    pool: int = 120,
) -> list[dict]:
    query = (query or "").strip()
    if not query:
        memos = store.list_memos(limit=limit, tag=tag, person=person, since=since, until=until)
        for m in memos:
            m["score"] = 0.0
            m["snippet"] = textutil.snippet(m["body"], "")
            m["why"] = "최근순"
        return memos

    vec = _vector_hits(query, pool)
    fts = _fts_hits(query, pool)

    # 같은 메모의 청크가 여러 번 뜨면 가장 좋은 것만 남긴다
    best_chunk: dict[int, tuple[float, str]] = {}
    vec_order: list[int] = []
    for memo_id, sim, text in vec:
        if memo_id not in best_chunk:
            best_chunk[memo_id] = (sim, text)
            vec_order.append(memo_id)

    fused = _rrf([vec_order, fts], [1.0, 0.8])
    if not fused:
        return []

    ranked = sorted(fused.items(), key=lambda kv: -kv[1])
    allowed = _filter_ids([mid for mid, _ in ranked], tag=tag, person=person, since=since, until=until)

    results: list[dict] = []
    vec_set, fts_set = set(vec_order), set(fts)
    for memo_id, score in ranked:
        if memo_id not in allowed:
            continue
        try:
            memo = store.get_memo(memo_id)
        except KeyError:
            continue
        sim, chunk_text = best_chunk.get(memo_id, (0.0, ""))
        memo["score"] = round(score, 6)
        memo["similarity"] = round(sim, 4)
        memo["matched_chunk"] = chunk_text
        memo["snippet"] = textutil.snippet(chunk_text or memo["body"], query)
        memo["why"] = _why(memo_id in vec_set, memo_id in fts_set)
        results.append(memo)
        if len(results) >= limit:
            break
    return results


def _why(by_vector: bool, by_keyword: bool) -> str:
    if by_vector and by_keyword:
        return "의미+키워드"
    if by_vector:
        return "의미"
    return "키워드"


def _filter_ids(ids: list[int], *, tag, person, since, until) -> set[int]:
    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    sql = [f"SELECT m.id FROM memos m WHERE m.id IN ({placeholders}) AND m.archived=0"]
    params: list = list(ids)
    if tag:
        sql.append("AND EXISTS(SELECT 1 FROM facets f WHERE f.memo_id=m.id AND f.kind='tag' AND f.value=?)")
        params.append(tag)
    if person:
        sql.append("AND EXISTS(SELECT 1 FROM facets f WHERE f.memo_id=m.id AND f.kind='person' AND f.value=?)")
        params.append(person)
    if since:
        sql.append("AND m.created_at >= ?")
        params.append(since)
    if until:
        sql.append("AND m.created_at <= ?")
        params.append(until + "T23:59:59" if len(until) == 10 else until)
    return {r["id"] for r in db.query(" ".join(sql), params)}


def similar(memo_id: int, limit: int = 8) -> list[dict]:
    """이 메모와 비슷한 다른 메모. '전에 비슷한 생각 했었는데' 를 잡아준다."""
    row = db.one("SELECT body FROM memos WHERE id=?", (memo_id,))
    if not row:
        return []
    hits = search(row["body"][:800], limit=limit + 1)
    return [h for h in hits if h["id"] != memo_id][:limit]
