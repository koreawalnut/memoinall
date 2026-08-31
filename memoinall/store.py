"""메모 저장 · 파생물 생성 파이프라인.

원문 저장은 동기(즉시 성공), 임베딩/추출은 비동기 큐로 돌린다.
적는 사람이 파이프라인을 기다리게 만들면 안 되기 때문이다.
"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime

from . import config, db, embed, extract, settings, textutil

log = logging.getLogger(__name__)

_jobs: "queue.Queue[int | None]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _parse(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return datetime.now()


# --------------------------------------------------------------------------- 쓰기


def add_memo(
    body: str,
    source: str = "web",
    *,
    created_at: str | None = None,
    updated_at: str | None = None,
    external_id: str | None = None,
    enqueue_enrich: bool = True,
) -> dict:
    """메모 저장.

    created_at/external_id 는 임포트용이다. 외부 앱에서 가져온 메모는 원본 작성
    시각을 유지해야 시간 기반 검색·롤업이 의미를 갖는다.
    """
    body = (body or "").strip()
    if not body:
        raise ValueError("빈 메모는 저장할 수 없습니다.")
    created = created_at or now_iso()
    cur = db.execute(
        "INSERT INTO memos(body, title, created_at, updated_at, source, external_id) VALUES(?,?,?,?,?,?)",
        (body, textutil.title_from(body), created, updated_at or created, source, external_id),
    )
    memo_id = int(cur.lastrowid)
    _index_fts(memo_id, body)
    if enqueue_enrich:
        enqueue(memo_id)
    return get_memo(memo_id)


def find_by_external(source: str, external_id: str) -> int | None:
    row = db.one(
        "SELECT id FROM memos WHERE source=? AND external_id=?", (source, external_id)
    )
    return int(row["id"]) if row else None


def update_memo(memo_id: int, body: str, *, enqueue_enrich: bool = True) -> dict:
    body = (body or "").strip()
    if not body:
        raise ValueError("빈 메모는 저장할 수 없습니다.")
    db.execute(
        "UPDATE memos SET body=?, title=?, updated_at=?, enriched_at=NULL WHERE id=?",
        (body, textutil.title_from(body), now_iso(), memo_id),
    )
    _index_fts(memo_id, body)
    if enqueue_enrich:
        enqueue(memo_id)
    return get_memo(memo_id)


def delete_memo(memo_id: int) -> None:
    db.execute("DELETE FROM memo_fts WHERE rowid=?", (memo_id,))
    db.execute("DELETE FROM memos WHERE id=?", (memo_id,))
    _search_cache.invalidate()


def count_by_source(source: str) -> dict:
    """한 소스에서 가져온 메모의 건수와 기간. '무엇을 지우게 되는지' 보여주려고."""
    row = db.one(
        "SELECT COUNT(*) n, MIN(created_at) first, MAX(created_at) last, "
        "SUM(CASE WHEN archived=1 THEN 1 ELSE 0 END) archived FROM memos WHERE source=?",
        (source,),
    )
    n = int(row["n"]) if row else 0
    return {
        "source": source,
        "memos": n,
        "archived": int(row["archived"] or 0) if n else 0,
        "first": (row["first"] or "")[:10] if n else "",
        "last": (row["last"] or "")[:10] if n else "",
    }


def delete_by_source(source: str) -> int:
    """한 소스에서 가져온 메모를 통째로 지운다. 지운 건수를 돌려준다.

    파생물(chunks/facets/todos)은 FK CASCADE 로 따라 지워지지만 memo_fts 는
    가상 테이블이라 외래키가 안 걸린다 — 직접 지우지 않으면 지워진 메모가
    검색 결과에 유령으로 남는다.

    source 를 통째로 지우므로 손으로 쓴 메모('web')를 넘기면 그것도 지워진다.
    막는 것은 호출자(API/CLI)의 몫이다.
    """
    ids = [int(r["id"]) for r in db.query("SELECT id FROM memos WHERE source=?", (source,))]
    if not ids:
        return 0
    conn = db.connect()
    with conn:  # 하나라도 실패하면 통째로 되돌린다 — 반쯤 지워진 상태가 제일 나쁘다
        conn.executemany("DELETE FROM memo_fts WHERE rowid=?", [(i,) for i in ids])
        conn.execute("DELETE FROM memos WHERE source=?", (source,))
    _search_cache.invalidate()
    return len(ids)


def set_flag(memo_id: int, *, pinned: bool | None = None, archived: bool | None = None) -> dict:
    if pinned is not None:
        db.execute("UPDATE memos SET pinned=? WHERE id=?", (1 if pinned else 0, memo_id))
    if archived is not None:
        db.execute("UPDATE memos SET archived=? WHERE id=?", (1 if archived else 0, memo_id))
        _search_cache.invalidate()  # 보관된 메모는 검색 인덱스에서 빠져야 한다
    return get_memo(memo_id)


def toggle_todo(todo_id: int, done: bool) -> None:
    db.execute("UPDATE todos SET done=? WHERE id=?", (1 if done else 0, todo_id))


def _index_fts(memo_id: int, body: str) -> None:
    db.execute("DELETE FROM memo_fts WHERE rowid=?", (memo_id,))
    db.execute(
        "INSERT INTO memo_fts(rowid, grams, raw) VALUES(?,?,?)",
        (memo_id, textutil.ngram_index_text(body), textutil.normalize(body).lower()),
    )


# --------------------------------------------------------------------------- 읽기


def get_memo(memo_id: int, *, with_facets: bool = True) -> dict:
    row = db.one("SELECT * FROM memos WHERE id=?", (memo_id,))
    if not row:
        raise KeyError(f"메모 {memo_id} 없음")
    memo = dict(row)
    if with_facets:
        memo["facets"] = facets_of(memo_id)
        memo["todos"] = [dict(r) for r in db.query("SELECT * FROM todos WHERE memo_id=? ORDER BY id", (memo_id,))]
    return memo


def facets_of(memo_id: int) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for row in db.query("SELECT kind, value FROM facets WHERE memo_id=?", (memo_id,)):
        out.setdefault(row["kind"], []).append(row["value"])
    return out


def list_memos(
    *,
    limit: int = 50,
    offset: int = 0,
    tag: str | None = None,
    person: str | None = None,
    since: str | None = None,
    until: str | None = None,
    archived: bool = False,
) -> list[dict]:
    sql = ["SELECT m.* FROM memos m"]
    params: list = []
    if tag:
        sql.append("JOIN facets f ON f.memo_id=m.id AND f.kind='tag' AND f.value=?")
        params.append(tag)
    if person:
        sql.append("JOIN facets p ON p.memo_id=m.id AND p.kind='person' AND p.value=?")
        params.append(person)
    sql.append("WHERE m.archived=?")
    params.append(1 if archived else 0)
    if since:
        sql.append("AND m.created_at >= ?")
        params.append(since)
    if until:
        sql.append("AND m.created_at <= ?")
        params.append(until + "T23:59:59" if len(until) == 10 else until)
    sql.append("ORDER BY m.pinned DESC, m.created_at DESC LIMIT ? OFFSET ?")
    params.extend([limit, offset])

    rows = db.query(" ".join(sql), params)
    out = []
    for row in rows:
        memo = dict(row)
        memo["facets"] = facets_of(memo["id"])
        out.append(memo)
    return out


def stats() -> dict:
    total = db.one("SELECT COUNT(*) c FROM memos WHERE archived=0")["c"]
    chunks = db.one("SELECT COUNT(*) c FROM chunks")["c"]
    embedded = db.one("SELECT COUNT(*) c FROM chunks WHERE embedding IS NOT NULL")["c"]
    pending = db.one("SELECT COUNT(*) c FROM memos WHERE enriched_at IS NULL AND archived=0")["c"]
    open_todos = db.one("SELECT COUNT(*) c FROM todos WHERE done=0")["c"]
    # DB 에 실제로 들어있는 임베딩 모델. 현재 프로세스의 임베더와 다를 수 있어서
    # (모델을 아직 안 올린 명령 등) 둘 다 보여줘야 오해가 없다.
    stored = [
        {"model": r["model"], "chunks": r["n"]}
        for r in db.query("SELECT model, COUNT(*) n FROM chunks GROUP BY model ORDER BY n DESC")
    ]
    return {
        "memos": total,
        "chunks": chunks,
        "embedded": embedded,
        "pending": pending,
        "open_todos": open_todos,
        "embedder": embed.status(),
        "stored_embeddings": stored,
        "llm_enabled": settings.provider_ready(),
        "llm_provider": settings.provider_name(),
        "llm_model": settings.provider_config().get("model", ""),
        "db_path": str(config.DB_PATH),
    }


def top_facets(kind: str, limit: int = 30) -> list[dict]:
    rows = db.query(
        "SELECT value, COUNT(*) n FROM facets WHERE kind=? GROUP BY value ORDER BY n DESC, value LIMIT ?",
        (kind, limit),
    )
    return [{"value": r["value"], "count": r["n"]} for r in rows]


def open_todos(limit: int = 100) -> list[dict]:
    rows = db.query(
        "SELECT t.*, m.title AS memo_title FROM todos t JOIN memos m ON m.id=t.memo_id "
        "WHERE t.done=0 AND m.archived=0 "
        "ORDER BY (t.due IS NULL), t.due, t.id DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- 파생물


def enrich(memo_id: int) -> bool:
    """청킹 → 임베딩 → 규칙 추출.

    임베딩(느림)은 트랜잭션 밖에서 먼저 끝내고, DB 반영만 한 트랜잭션으로 묶는다.
    큐에 남아 있는 사이 메모가 지워질 수 있으므로 트랜잭션 안에서 존재를 다시 확인한다.
    """
    row = db.one("SELECT * FROM memos WHERE id=?", (memo_id,))
    if not row:
        return False
    body = row["body"]
    created = _parse(row["created_at"])

    pieces = textutil.chunk(body, config.CHUNK_TARGET, config.CHUNK_MAX)
    vectors, model_name = embed.encode(pieces)
    facets = extract.extract(body, created)

    conn = db.connect()
    with conn:  # 예외 시 자동 롤백
        still_there = conn.execute("SELECT body FROM memos WHERE id=?", (memo_id,)).fetchone()
        if still_there is None or still_there["body"] != body:
            return False  # 삭제됐거나 그 사이 또 수정됨 — 뒤따르는 작업이 처리한다

        # 파생물은 지웠다 다시 만든다. 단, 사용자가 UI 에서 체크한 완료 상태는
        # 원문에 없는 정보라 재생성으로 날아가면 안 된다 — 본문 기준으로 살려둔다.
        prev_done = {
            r["text"]
            for r in conn.execute("SELECT text FROM todos WHERE memo_id=? AND done=1", (memo_id,))
        }
        conn.execute("DELETE FROM chunks WHERE memo_id=?", (memo_id,))
        conn.execute("DELETE FROM facets WHERE memo_id=?", (memo_id,))
        conn.execute("DELETE FROM todos WHERE memo_id=?", (memo_id,))

        for seq, (text, vec) in enumerate(zip(pieces, vectors)):
            conn.execute(
                "INSERT INTO chunks(memo_id, seq, text, model, dim, embedding) VALUES(?,?,?,?,?,?)",
                (memo_id, seq, text, model_name, int(vec.shape[0]), embed.to_blob(vec)),
            )
        for kind in ("tag", "person", "link", "date", "decision", "question"):
            for value in facets.get(kind, []):
                conn.execute(
                    "INSERT OR IGNORE INTO facets(memo_id, kind, value) VALUES(?,?,?)",
                    (memo_id, kind, value[:500]),
                )
        for todo in facets["todos"]:
            text = todo["text"][:500]
            done = todo["done"] or text in prev_done
            conn.execute(
                "INSERT INTO todos(memo_id, text, done, due, created_at) VALUES(?,?,?,?,?)",
                (memo_id, text, 1 if done else 0, todo.get("due"), row["created_at"]),
            )
        conn.execute("UPDATE memos SET enriched_at=? WHERE id=?", (now_iso(), memo_id))

    _search_cache.invalidate()
    return True


def enqueue(memo_id: int) -> None:
    _jobs.put(memo_id)
    start_worker()


def start_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run_worker, name="enrich", daemon=True)
        _worker.start()


def _run_worker() -> None:
    while True:
        memo_id = _jobs.get()
        if memo_id is None:
            return
        try:
            enrich(memo_id)
        except Exception:
            log.exception("메모 %s 보강 실패", memo_id)
        finally:
            _jobs.task_done()


def queue_size() -> int:
    return _jobs.qsize()


def reindex_pending() -> int:
    """미보강 메모 + 모델이 바뀐 청크를 다시 큐에 넣는다."""
    model = embed.current().name
    rows = db.query(
        "SELECT DISTINCT m.id FROM memos m "
        "LEFT JOIN chunks c ON c.memo_id=m.id "
        "WHERE m.enriched_at IS NULL OR c.id IS NULL OR c.model IS NOT ? ",
        (model,),
    )
    for row in rows:
        _jobs.put(row["id"])
    if rows:
        start_worker()
    return len(rows)


def reindex_all() -> int:
    rows = db.query("SELECT id FROM memos")
    for row in rows:
        _jobs.put(row["id"])
    start_worker()
    return len(rows)


# --------------------------------------------------------------------------- 벡터 캐시


class _VectorCache:
    """검색용 임베딩 행렬을 메모리에 들고 있는다. 개인 규모(수만 건)에선 충분하다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dirty = True
        self._matrix = None
        self._meta: list[tuple[int, int, str]] = []  # (chunk_id, memo_id, text)
        self._model = ""

    def invalidate(self) -> None:
        with self._lock:
            self._dirty = True

    def get(self):
        import numpy as np

        model = embed.current().name
        with self._lock:
            if not self._dirty and self._model == model and self._matrix is not None:
                return self._matrix, self._meta

            rows = db.query(
                "SELECT c.id, c.memo_id, c.text, c.dim, c.embedding FROM chunks c "
                "JOIN memos m ON m.id=c.memo_id "
                "WHERE c.embedding IS NOT NULL AND c.model = ? AND m.archived = 0",
                (model,),
            )
            meta: list[tuple[int, int, str]] = []
            vecs = []
            for r in rows:
                vecs.append(embed.from_blob(r["embedding"], r["dim"]))
                meta.append((r["id"], r["memo_id"], r["text"]))
            matrix = np.vstack(vecs) if vecs else np.zeros((0, embed.current().dim), dtype="float32")
            self._matrix, self._meta, self._model, self._dirty = matrix, meta, model, False
            return matrix, meta


_search_cache = _VectorCache()


def vector_index():
    return _search_cache.get()


def invalidate_cache() -> None:
    _search_cache.invalidate()
