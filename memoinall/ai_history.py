"""Local AI documents, recoverable drafts and immutable revisions."""
from __future__ import annotations

import json
import sqlite3

from . import db, store

MAX_BODY = 200_000
PACK_KEYS = ("instruction", "format", "queries", "query_method", "budget_tokens", "used_tokens",
             "sources", "dropped", "prompt", "provider", "memo_ids", "scope", "collection")


class Conflict(ValueError):
    pass


def check_body(body, *, empty=False) -> str:
    if not isinstance(body, str) or len(body) > MAX_BODY:
        raise ValueError("내용은 20만 자 이내로 입력하세요.")
    if not empty and not body.strip():
        raise ValueError("내용을 입력하세요.")
    return body


def _record(result_id):
    row = db.one("SELECT * FROM ai_results WHERE id=?", (result_id,))
    if row is None:
        raise KeyError("정리한 결과를 찾을 수 없습니다.")
    return dict(row)


def _check_version(record, version):
    if not isinstance(version, int) or isinstance(version, bool) or version != record["latest_version"]:
        raise Conflict("다른 곳에서 수정한 내용이 있습니다. 이전 결과에서 다시 열어주세요.")


def _append(conn, result_id, body, pack, kind, request=""):
    record = _record(result_id)
    version = record["latest_version"] + 1
    now = store.now_iso()
    data = {key: pack[key] for key in PACK_KEYS if key in pack}
    conn.execute("INSERT INTO ai_versions(result_id,version,body,pack_json,kind,request,created_at) VALUES(?,?,?,?,?,?,?)",
                 (result_id, version, body, json.dumps(data, ensure_ascii=False), kind, request, now))
    conn.execute("UPDATE ai_results SET latest_version=?,draft_body=NULL,updated_at=? WHERE id=?", (version, now, result_id))


def create(pack: dict) -> dict:
    body = check_body(pack.get("output"))
    conn = db.connect()
    with conn:
        now = store.now_iso()
        cur = conn.execute("INSERT INTO ai_results(title,created_at,updated_at) VALUES(?,?,?)",
                           (str(pack.get("instruction") or "AI로 정리한 메모")[:100], now, now))
        result_id = cur.lastrowid
        _append(conn, result_id, body, pack, "generated")
    return get(result_id)


def list_results(limit=20, offset=0, q="") -> dict:
    # Search as literal text, including '%' and '_'.
    query = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = db.query("SELECT id,title,created_at,updated_at,latest_version,draft_body IS NOT NULL AS has_draft "
                    "FROM ai_results WHERE title LIKE ? ESCAPE '\\' ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
                    (f"%{query}%", limit + 1, offset))
    return {"items": [dict(r) for r in rows[:limit]], "has_more": len(rows) > limit}


def get(result_id, version=None) -> dict:
    record = _record(result_id)
    number = record["latest_version"] if version is None else version
    row = db.one("SELECT * FROM ai_versions WHERE result_id=? AND version=?", (result_id, number))
    if row is None:
        raise KeyError("이전 내용을 찾을 수 없습니다.")
    return {**json.loads(row["pack_json"]), "output": row["body"], "history_id": result_id,
            "version": number, "latest_version": record["latest_version"], "title": record["title"],
            "draft_body": record["draft_body"] if number == record["latest_version"] else None,
            "memo_id": row["memo_id"], "created_at": row["created_at"], "reason": None,
            "versions": [dict(v) for v in db.query(
                "SELECT version,kind,request,created_at FROM ai_versions WHERE result_id=? ORDER BY version DESC", (result_id,))]}


def save_draft(result_id, version, body):
    check_body(body, empty=True)
    conn = db.connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _check_version(_record(result_id), version)
        original = get(result_id)["output"]
        conn.execute("UPDATE ai_results SET draft_body=?,updated_at=? WHERE id=?",
                     (None if body == original else body, store.now_iso(), result_id))
    return {"ok": True}


def commit(result_id, version, body, *, kind="edited", pack=None, request="") -> dict:
    check_body(body)
    conn = db.connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _check_version(_record(result_id), version)
        current = get(result_id)
        if body != current["output"] or kind not in ("edited",):
            _append(conn, result_id, body, pack or current, kind, request)
        else:
            conn.execute("UPDATE ai_results SET draft_body=NULL WHERE id=?", (result_id,))
    return get(result_id)


def restore(result_id, version, target_version):
    conn = db.connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _check_version(_record(result_id), version)
        old, current = get(result_id, target_version), get(result_id)
        # Preserve unfinished edits too, so restoring never destroys the draft.
        if current["draft_body"] is not None and current["draft_body"] != current["output"]:
            _append(conn, result_id, current["draft_body"], current, "edited")
        _append(conn, result_id, old["output"], old, "restored", f"{target_version}번째 내용으로 되돌림")
    return get(result_id)


def save_as_memo(result_id, version) -> dict:
    record = _record(result_id)
    _check_version(record, version)
    current = get(result_id, version)
    if current["draft_body"] is not None:
        raise ValueError("수정 완료를 누른 뒤 메모로 저장하세요.")
    # One memo per revision, including retries after an interrupted response.
    external_id = f"ai-result:{result_id}:{version}"
    memo_id = current["memo_id"] or store.find_by_external("ai", external_id)
    if memo_id is None:
        collection = current.get("collection")
        if collection:
            # 메모를 공유해도 상대 PC에는 이 취합 자료가 없다. 어떤 자료·버전이었는지 글로 남긴다.
            references = (f"- [C{collection['id']}] {collection['title']} · {collection['version']}번째 취합"
                          f" · 파일 {collection['files']}개 · {collection['rows']:,}행")
            body = current["output"] + "\n\n---\nAI로 정리한 메모\n사용한 취합 자료:\n" + references
        else:
            references = "\n".join(f"- [M{s['id']}] {s['title']}" for s in current.get("sources", []))
            body = current["output"] + "\n\n---\nAI로 정리한 메모\n참고한 메모:\n" + (references or "없음")
        try:
            memo_id = store.add_memo(body, source="ai", external_id=external_id)["id"]
        except sqlite3.IntegrityError:
            memo_id = store.find_by_external("ai", external_id)
            if memo_id is None:
                raise
    db.execute("UPDATE ai_versions SET memo_id=? WHERE result_id=? AND version=?", (memo_id, result_id, version))
    return {"memo_id": memo_id}
