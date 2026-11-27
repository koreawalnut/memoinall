"""정리 계층 — 쌓인 메모를 사람이 소화할 수 있는 단위로 묶는다.

- cluster(): 임베딩 기반 주제 자동 묶음. CLI `clusters` 와 /api/clusters 에서만 쓴다
  (화면은 묶은 기준이 보이는 '내 태그' 단위로 보여준다 — tags.board, rollup 의 tag_groups).
- rollup(): 기간별 브리핑 재료(메모/할일/결정/사람/태그 집계, 내 태그별 묶음).
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta

import numpy as np

from . import db, embed, store


def _memo_vectors(since: str | None, until: str | None, archived: bool = False):
    """메모당 하나의 대표 벡터(청크 평균)."""
    model = embed.current().name
    sql = [
        "SELECT c.memo_id, c.dim, c.embedding FROM chunks c JOIN memos m ON m.id=c.memo_id",
        "WHERE c.embedding IS NOT NULL AND c.model=? AND m.archived=?",
    ]
    params: list = [model, 1 if archived else 0]
    if since:
        sql.append("AND m.created_at >= ?")
        params.append(since)
    if until:
        sql.append("AND m.created_at <= ?")
        params.append(until + "T23:59:59" if len(until) == 10 else until)

    buckets: dict[int, list[np.ndarray]] = {}
    for row in db.query(" ".join(sql), params):
        buckets.setdefault(row["memo_id"], []).append(embed.from_blob(row["embedding"], row["dim"]))
    if not buckets:
        return [], np.zeros((0, embed.current().dim), dtype=np.float32)

    ids = sorted(buckets)
    mat = np.vstack([np.mean(buckets[i], axis=0) for i in ids]).astype(np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return ids, mat / norms


def cluster(k: int | None = None, *, since: str | None = None, until: str | None = None, seed: int = 7) -> list[dict]:
    """구면 k-means. 의존성 없이 numpy 로만 돌린다."""
    ids, mat = _memo_vectors(since, until)
    n = len(ids)
    if n == 0:
        return []
    if n <= 3:
        return [_describe_cluster(ids, mat, np.zeros(n, dtype=int), 0)]

    if k is None:
        k = max(2, min(12, int(round(n**0.5))))
    k = min(k, n)

    rng = np.random.default_rng(seed)
    centers = mat[rng.choice(n, size=k, replace=False)].copy()

    labels = np.zeros(n, dtype=int)
    for _ in range(40):
        sims = mat @ centers.T
        new_labels = np.argmax(sims, axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for j in range(k):
            members = mat[labels == j]
            if members.shape[0] == 0:
                centers[j] = mat[rng.integers(0, n)]
                continue
            c = members.mean(axis=0)
            norm = np.linalg.norm(c)
            centers[j] = c / norm if norm else c

    clusters = [_describe_cluster(ids, mat, labels, j) for j in range(k)]
    clusters = [c for c in clusters if c["size"] > 0]
    clusters.sort(key=lambda c: -c["size"])
    return clusters


def _describe_cluster(ids: list[int], mat: np.ndarray, labels: np.ndarray, j: int) -> dict:
    member_idx = [i for i, lab in enumerate(labels) if lab == j]
    member_ids = [ids[i] for i in member_idx]
    if not member_ids:
        return {"id": j, "size": 0, "label": "", "tags": [], "memos": []}

    sub = mat[member_idx]
    center = sub.mean(axis=0)
    norm = np.linalg.norm(center)
    center = center / norm if norm else center
    order = np.argsort(-(sub @ center))
    ordered_ids = [member_ids[i] for i in order]

    rows = db.query(
        f"SELECT id, title, created_at FROM memos WHERE id IN ({','.join('?' * len(ordered_ids))})",
        ordered_ids,
    )
    by_id = {r["id"]: dict(r) for r in rows}

    tag_rows = db.query(
        f"SELECT value FROM facets WHERE kind='tag' AND memo_id IN ({','.join('?' * len(ordered_ids))})",
        ordered_ids,
    )
    tags = [t for t, _ in Counter(r["value"] for r in tag_rows).most_common(5)]

    label = tags[0] if tags else (by_id.get(ordered_ids[0], {}).get("title") or f"주제 {j + 1}")
    return {
        "id": j,
        "size": len(ordered_ids),
        "label": label,
        "tags": tags,
        "memos": [by_id[i] for i in ordered_ids[:12] if i in by_id],
    }


def rollup(period: str = "week", anchor: str | None = None) -> dict:
    """일간/주간/월간 브리핑 재료."""
    today = date.fromisoformat(anchor) if anchor else date.today()
    if period == "day":
        start, end = today, today
    elif period == "month":
        start = today.replace(day=1)
        end = today
    else:
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)

    since, until = start.isoformat(), end.isoformat()
    memos = store.list_memos(limit=500, since=since, until=until)
    ids = [m["id"] for m in memos]

    todos_done, todos_open, decisions, questions = [], [], [], []
    tag_counter: Counter = Counter()
    people_counter: Counter = Counter()

    if ids:
        ph = ",".join("?" * len(ids))
        for r in db.query(f"SELECT * FROM todos WHERE memo_id IN ({ph}) AND active=1 AND status NOT IN ('dismissed','review')", ids):
            (todos_done if r["done"] else todos_open).append({"id": r["id"], "text": r["text"], "memo_id": r["memo_id"], "due": r["due"]})
        for r in db.query(f"SELECT kind, value, memo_id FROM facets WHERE memo_id IN ({ph})", ids):
            if r["kind"] == "tag":
                tag_counter[r["value"]] += 1
            elif r["kind"] == "person":
                people_counter[r["value"]] += 1
            elif r["kind"] == "decision":
                decisions.append({"text": r["value"], "memo_id": r["memo_id"]})
            elif r["kind"] == "question":
                questions.append({"text": r["value"], "memo_id": r["memo_id"]})

    return {
        "period": period,
        "start": since,
        "end": until,
        "memo_count": len(memos),
        "memos": [{"id": m["id"], "title": m["title"], "created_at": m["created_at"], "updated_at": m["updated_at"]} for m in memos],
        "decisions": decisions,
        "questions": questions,
        "todos_open": todos_open,
        "todos_done": todos_done,
        "top_tags": [{"value": v, "count": c} for v, c in tag_counter.most_common(10)],
        "top_people": [{"value": v, "count": c} for v, c in people_counter.most_common(10)],
        **_tag_groups(memos),
    }


def _tag_groups(memos: list[dict], per: int = 8) -> dict:
    """기간 안의 메모를 설정의 '내 태그' 순서대로 묶는다. 자동 군집과 달리 묶은 기준이 사용자에게 보인다.

    한 메모에 내 태그가 여럿이면 각 묶음에 다 들어간다. 내 태그가 하나도 없는 메모는 따로 모은다.
    """
    from . import tags  # tags → store 순환을 피해 여기서 부른다

    brief = lambda m: {"id": m["id"], "title": m["title"], "created_at": m["created_at"], "updated_at": m["updated_at"]}  # noqa: E731
    mine = tags.all_tags(with_counts=False)
    groups = []
    for tag in mine:
        hit = [m for m in memos if tag["name"] in (m.get("facets") or {}).get("tag", [])]
        if hit:
            groups.append({"name": tag["name"], "color": tag["color"], "count": len(hit),
                           "memos": [brief(m) for m in hit[:per]]})
    names = {t["name"] for t in mine}
    rest = [m for m in memos if not names & set((m.get("facets") or {}).get("tag", []))]
    return {"tag_groups": groups, "untagged": {"count": len(rest), "memos": [brief(m) for m in rest[:per]]}}


def rollup_prompt(data: dict) -> str:
    """롤업을 LLM 에 넘길 텍스트로. 키가 없어도 사람이 읽을 수 있게 만든다."""
    lines = [
        f"# {data['start']} ~ {data['end']} 업무 메모 요약 재료",
        f"메모 {data['memo_count']}건",
        "",
    ]
    untagged = data.get("untagged") or {"count": 0, "memos": []}
    if data.get("tag_groups") or untagged["count"]:
        lines.append("## 내 태그별")
        for g in data.get("tag_groups", []):
            titles = ", ".join(m["title"] for m in g["memos"][:5])
            lines.append(f"- #{g['name']} ({g['count']}건): {titles}")
        if untagged["count"]:
            titles = ", ".join(m["title"] for m in untagged["memos"][:5])
            lines.append(f"- 내 태그 없음 ({untagged['count']}건): {titles}")
        lines.append("")
    if data["decisions"]:
        lines.append("## 결정/결론")
        lines.extend(f"- {d['text']} [M{d['memo_id']}]" for d in data["decisions"])
        lines.append("")
    if data["todos_open"]:
        lines.append("## 미완 할일")
        lines.extend(
            f"- {t['text']}" + (f" (마감 {t['due']})" if t["due"] else "") + f" [M{t['memo_id']}]"
            for t in data["todos_open"]
        )
        lines.append("")
    if data["todos_done"]:
        lines.append("## 완료 할일")
        lines.extend(f"- {t['text']} [M{t['memo_id']}]" for t in data["todos_done"])
        lines.append("")
    if data["questions"]:
        lines.append("## 열린 질문")
        lines.extend(f"- {q['text']} [M{q['memo_id']}]" for q in data["questions"])
        lines.append("")
    if data["top_tags"]:
        lines.append("## 태그: " + ", ".join(f"#{t['value']}({t['count']})" for t in data["top_tags"]))
    if data["top_people"]:
        lines.append("## 사람: " + ", ".join(f"@{p['value']}({p['count']})" for p in data["top_people"]))
    return "\n".join(lines)
