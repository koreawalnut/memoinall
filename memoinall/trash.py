"""휴지통 — 지운 메모를 30일 동안 보관했다가 되살리거나 완전히 지운다.

메모를 지우면 memos 에서 실제로 뺀다. 그래야 검색·AI 근거·일정·공유·내보내기에서 예전처럼 바로 사라지고,
그 많은 조회마다 '지운 메모 빼기' 조건을 달다가 하나라도 빠뜨려 지운 메모가 새어 나가는 일이 없다.
대신 되살릴 때 필요한 것을 memo_trash 에 적어 둔다.
  - memos 행 그대로. 같은 id 로 되살린다(memos 는 AUTOINCREMENT 라 그 id 를 다른 메모가 받지 않는다).
  - 일정에서 직접 정한 것 — todos 행(완료·날짜·태그 등). 같은 id 로 되돌리면 다시 분석할 때 그대로 이어진다.
  - 받은 파일의 중복 판단 기록(memo_import_aliases)
  - 이 메모를 가리키던 취합표·AI 결과 연결(지우면 NULL 이 된다)
  - 항상 위에 띄워 두었던 창의 위치(memo_windows). 10개가 차 있으면 창만 빼고 되살린다.
청크·임베딩·추출 결과는 되살린 뒤 다시 만든다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import db, store
from .memo_windows import MAX_WINDOWS

KEEP_DAYS = 30


class Conflict(ValueError):
    """되살릴 수 없다(같은 원본의 메모가 이미 다시 들어와 있다 등)."""


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _rows(conn, sql: str, params) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _insert(conn, table: str, row: dict) -> None:
    """지금 표에 있는 열만 넣는다 — 지운 뒤 앱이 바뀌어 열이 늘거나 줄어도 되살릴 수 있게."""
    keep = [k for k in row if k in _columns(conn, table)]
    conn.execute(f"INSERT INTO {table}({','.join(keep)}) VALUES({','.join('?' for _ in keep)})",
                 [row[k] for k in keep])


def move(memo_id: int) -> dict:
    """메모를 휴지통으로. 없는 메모면 KeyError."""
    conn = db.connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        found = conn.execute("SELECT * FROM memos WHERE id=?", (memo_id,)).fetchone()
        if found is None:
            raise KeyError(f"메모 {memo_id} 없음")
        memo = dict(found)
        saved = {
            "memo": memo,
            "todos": _rows(conn, "SELECT * FROM todos WHERE memo_id=?", (memo_id,)),
            "aliases": _rows(conn, "SELECT * FROM memo_import_aliases WHERE memo_id=?", (memo_id,)),
            "collections": [r["id"] for r in conn.execute("SELECT id FROM collections WHERE memo_id=?", (memo_id,))],
            "ai_versions": [[r["result_id"], r["version"]] for r in conn.execute(
                "SELECT result_id,version FROM ai_versions WHERE memo_id=?", (memo_id,))],
            # 항상 위에 띄워 두었던 메모면 그 창 위치까지(되살리면 다음에 앱을 켤 때 다시 뜬다)
            "memo_window": next(iter(_rows(conn, "SELECT * FROM memo_windows WHERE memo_id=?", (memo_id,))), None),
        }
        deleted_at = _now().isoformat()
        conn.execute("INSERT OR REPLACE INTO memo_trash(memo_id,title,body,deleted_at,saved_json) VALUES(?,?,?,?,?)",
                     (memo_id, memo["title"], memo["body"], deleted_at, json.dumps(saved, ensure_ascii=False)))
        conn.execute("DELETE FROM memo_fts WHERE rowid=?", (memo_id,))
        conn.execute("DELETE FROM memos WHERE id=?", (memo_id,))
    store.invalidate_cache()
    purge_expired()
    return {"id": memo_id, "title": memo["title"], "deleted_at": deleted_at}


def restore(memo_id: int) -> dict:
    """휴지통의 메모를 같은 id 로 되살린다. 없으면 KeyError, 되살릴 수 없으면 Conflict."""
    conn = db.connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT saved_json FROM memo_trash WHERE memo_id=?", (memo_id,)).fetchone()
        if row is None:
            raise KeyError(f"휴지통에 메모 {memo_id} 없음")
        saved = json.loads(row["saved_json"])
        memo = dict(saved["memo"])
        if conn.execute("SELECT 1 FROM memos WHERE id=?", (memo_id,)).fetchone():
            raise Conflict("같은 번호의 메모가 이미 있어 되살릴 수 없습니다.")
        if memo.get("external_id") and conn.execute(
                "SELECT 1 FROM memos WHERE source=? AND external_id=?", (memo["source"], memo["external_id"])).fetchone():
            raise Conflict("같은 원본에서 다시 가져온 메모가 이미 있습니다. 휴지통의 것은 완전히 지워도 됩니다.")
        memo.update(enriched_at=None, todo_version=0)
        _insert(conn, "memos", memo)
        store._index_body(conn, memo_id, memo["body"])
        for todo in saved.get("todos", []):
            _insert(conn, "todos", todo)
        for alias in saved.get("aliases", []):
            conn.execute("INSERT OR IGNORE INTO memo_import_aliases(memo_id,original_uid,tagged_uid) VALUES(?,?,?)",
                         (memo_id, alias["original_uid"], alias["tagged_uid"]))
        for cid in saved.get("collections", []):
            conn.execute("UPDATE collections SET memo_id=? WHERE id=? AND memo_id IS NULL", (memo_id, cid))
        for result_id, version in saved.get("ai_versions", []):
            conn.execute("UPDATE ai_versions SET memo_id=? WHERE result_id=? AND version=? AND memo_id IS NULL",
                         (memo_id, result_id, version))
        window = saved.get("memo_window")
        # 그 사이 다른 메모로 10개가 찼으면 창은 되살리지 않는다(메모는 되살린다).
        if window and conn.execute("SELECT COUNT(*) FROM memo_windows").fetchone()[0] < MAX_WINDOWS:
            _insert(conn, "memo_windows", window)
        conn.execute("DELETE FROM memo_trash WHERE memo_id=?", (memo_id,))
    store.invalidate_cache()
    store.enqueue(memo_id)
    return store.get_memo(memo_id)


def purge(memo_id: int) -> None:
    """휴지통에서 완전히 지운다. 없으면 KeyError."""
    if not db.execute("DELETE FROM memo_trash WHERE memo_id=?", (memo_id,)).rowcount:
        raise KeyError(f"휴지통에 메모 {memo_id} 없음")


def empty() -> int:
    return db.execute("DELETE FROM memo_trash").rowcount


def purge_expired(now: datetime | None = None) -> int:
    """30일 지난 것을 지운다. 앱을 켤 때·휴지통을 볼 때·메모를 지울 때 부른다."""
    cutoff = ((now or _now()) - timedelta(days=KEEP_DAYS)).isoformat()
    return db.execute("DELETE FROM memo_trash WHERE deleted_at < ?", (cutoff,)).rowcount


def items(now: datetime | None = None) -> list[dict]:
    now = now or _now()
    purge_expired(now)
    out = []
    for r in db.query("SELECT memo_id,title,body,deleted_at,saved_json FROM memo_trash ORDER BY id DESC"):
        deleted = datetime.fromisoformat(r["deleted_at"])
        memo = json.loads(r["saved_json"])["memo"]
        out.append({
            "id": r["memo_id"], "title": r["title"], "body": r["body"], "deleted_at": r["deleted_at"],
            "created_at": memo.get("created_at", ""),
            # 지운 날로부터 30일째 되는 날까지 남는다. 오늘 지우면 30, 내일 지워지면 1.
            "days_left": max(1, KEEP_DAYS - (now - deleted).days),
        })
    return out
