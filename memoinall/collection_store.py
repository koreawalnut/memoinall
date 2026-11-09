"""확정한 엑셀 취합 자료의 저장·조회·삭제.

취합 자료 하나는 대표 메모 하나로 목록에 보인다. 행마다 메모를 만들지 않는다.
다시 취합하면 같은 자료에 새 버전을 쌓고, 이전 버전과 그 AI 결과는 그대로 둔다.
"""

from __future__ import annotations

import json

from . import db, store, tags as tag_rules
from .excel_collection import CollectionError, analyze, get_job
from .extract import TAG_RE

COLLECTION_TAG = "엑셀취합"
MEMO_SOURCE = "collection"
MAX_TITLE = 100


class Conflict(CollectionError):
    pass


class NotFound(KeyError):
    pass


def _title(value) -> str:
    title = " ".join(str(value or "").split())
    if not title:
        raise CollectionError("취합 자료 이름을 입력하세요.")
    if len(title) > MAX_TITLE:
        raise CollectionError("취합 자료 이름은 100자 이하로 입력하세요.")
    return title


def _tags(values) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 20:
        raise CollectionError("태그는 20개까지 붙일 수 있습니다.")
    out = []
    for raw in values:
        try:
            name = tag_rules.normalize(str(raw))
        except tag_rules.TagError as exc:
            raise CollectionError(str(exc)) from None
        if name.casefold() != COLLECTION_TAG.casefold() and name not in out:
            out.append(name)
    return out


def _encode(cell):
    if cell is None:
        return None
    kind, display, value = cell
    return [kind, display] if value is None or value == display else [kind, display, value]


def commit(job_id: str, rules, revision, title=None, tag_names=None) -> dict:
    """확인 화면에서 본 그대로(같은 revision) 저장한다. 같은 요청을 두 번 보내도 한 번만 저장된다."""
    job = get_job(job_id)
    with job.lock:
        if job.result is not None:
            if job.result["revision"] == revision:
                return job.result["out"]
            raise Conflict("이미 저장한 작업입니다. 저장한 취합 자료에서 확인하세요.")
        if not isinstance(revision, str) or not revision:
            raise CollectionError("확인 화면을 다시 불러온 뒤 저장하세요.")
        clean_title = _title(title or job.title)
        clean_tags = _tags(tag_names)
        result = analyze(job, rules, full=True)
        if result["revision"] != revision:
            raise Conflict("확인한 뒤 파일이나 선택이 바뀌었습니다. 확인 화면을 다시 보고 저장하세요.")
        if result["summary"]["blocking"]:
            raise CollectionError(f"확인 필요 {result['summary']['blocking']}개를 먼저 처리하세요.")
        if not any(r[2] for r in result["rows"]):
            raise CollectionError("취합할 행이 없습니다. 파일과 시트 선택을 확인하세요.")
        out = _save(job, result, clean_title, clean_tags)
        job.result = {"revision": revision, "out": out}
        # 저장한 뒤에는 읽은 원본 표를 메모리에 둘 이유가 없다.
        job.files, job.cache = [], {}
    return out


def _duplicates(rows: list, sources: list) -> dict:
    seen: dict[str, list] = {}
    for src, row_no, included, _, cells in rows:
        if not included:
            continue
        key = json.dumps([_encode(c) for c in cells], ensure_ascii=False)
        seen.setdefault(key, []).append((src, row_no))
    groups = [v for v in seen.values() if len(v) > 1]
    return {
        "groups": len(groups),
        "rows": sum(len(g) for g in groups),
        "examples": [[{"file": sources[s]["file"], "sheet": sources[s]["sheet"], "row": n} for s, n in g[:4]]
                     for g in groups[:5]],
    }


def _stats(result: dict) -> dict:
    files = []
    for view in result["files"]:
        files.append({
            "path": view["path"], "status": view["status"], "error": view["error"],
            "selected": view["selected"], "reason": view.get("reason", ""), "rows": view.get("rows", 0),
            "sheets": [{"name": s["name"], "selected": s["selected"], "hidden": s["hidden"], "empty": s["empty"],
                        "header_row": s.get("header_row"), "rows": s.get("rows", 0),
                        "data_rows": s.get("data_rows", 0), "notes": s.get("notes", [])}
                       for s in view["sheets"]],
        })
    included = sum(1 for r in result["rows"] if r[2])
    return {
        "summary": {**result["summary"], "included_rows": included,
                    "excluded_rows": len(result["rows"]) - included},
        "files": files,
        "skipped": result["skipped"],
        "duplicates": _duplicates(result["rows"], result["sources"]),
    }


def _save(job, result: dict, title: str, tags: list[str]) -> dict:
    fields = [{"name": n, "type": t} for n, t in zip(result["fields"], result["field_types"])]
    stats = _stats(result)
    rules = json.dumps(result["rules"], ensure_ascii=False, sort_keys=True)
    now = store.now_iso()
    conn = db.connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if job.collection_id is not None:
            row = conn.execute("SELECT id, current_version FROM collections WHERE id=?", (job.collection_id,)).fetchone()
            if row is None:
                raise NotFound("다시 취합할 자료를 찾을 수 없습니다. 새로 취합하세요.")
            cid, version = row["id"], row["current_version"] + 1
            conn.execute("UPDATE collections SET title=?, current_version=?, rules_json=?, updated_at=? WHERE id=?",
                         (title, version, rules, now, cid))
        else:
            cur = conn.execute("INSERT INTO collections(title, current_version, rules_json, created_at, updated_at) "
                               "VALUES(?,1,?,?,?)", (title, rules, now, now))
            cid, version = int(cur.lastrowid), 1
        conn.execute(
            "INSERT INTO collection_versions(collection_id, version, created_at, fields_json, sources_json, "
            "stats_json, rules_json, fingerprint) VALUES(?,?,?,?,?,?,?,?)",
            (cid, version, now, json.dumps(fields, ensure_ascii=False), json.dumps(result["sources"], ensure_ascii=False),
             json.dumps(stats, ensure_ascii=False), rules, result["revision"]))
        conn.executemany(
            "INSERT INTO collection_rows(collection_id, version, seq, src, row_no, included, reason, cells_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ((cid, version, seq, src, row_no, int(included), reason,
              json.dumps([_encode(c) for c in cells], ensure_ascii=False, separators=(",", ":")))
             for seq, (src, row_no, included, reason, cells) in enumerate(result["rows"])))
    memo_id = _sync_memo(cid, version, tags)
    return {"collection_id": cid, "version": version, "memo_id": memo_id, "title": title}


def memo_body(meta: dict, headline: list[str], tags: list[str]) -> str:
    stats = meta["stats"]["summary"]
    when = meta["created_at"][:16].replace("T", " ")
    lines = [f"엑셀 취합: {meta['title']}",
             f"파일 {stats['files']}개 · {stats['included_rows']:,}행 · {meta['version']}번째 취합 · {when}"]
    lines += headline
    excluded = [f["path"] for f in meta["stats"]["files"] if f["status"] == "read" and f.get("reason")]
    failed = [f["path"] for f in meta["stats"]["files"] if f["status"] != "read"] + [s["path"] for s in meta["stats"]["skipped"]]
    if excluded:
        lines.append(f"제외한 파일 {len(excluded)}개: " + ", ".join(excluded[:5]) + (" 외" if len(excluded) > 5 else ""))
    if failed:
        lines.append(f"읽지 못한 파일 {len(failed)}개: " + ", ".join(failed[:5]) + (" 외" if len(failed) > 5 else ""))
    # 메모만 공유하면 받는 PC에는 표가 없다. 연결이 있는 것처럼 쓰지 않는다.
    lines.append(f"취합 자료 C{meta['id']} · 전체 행과 출처는 취합한 PC의 memoinall에서 ‘취합표 보기’로 확인합니다.")
    lines.append("")
    lines.append(" ".join("#" + t for t in [COLLECTION_TAG, *tags]))
    return "\n".join(lines)


def _sync_memo(cid: int, version: int, tags: list[str]) -> int:
    from . import collection_analysis

    meta = version_meta(cid, version)
    summary = collection_analysis.summary(cid, version)
    headline = [f"{n['field']} 합계 {collection_analysis.fmt(n['sum'])}"
                + (f" (값 확인 필요 {n['invalid']}행 제외)" if n["invalid"] else "")
                for n in summary["numbers"][:2]]
    record = db.one("SELECT memo_id FROM collections WHERE id=?", (cid,))
    memo_id = record["memo_id"] if record else None
    existing = None
    if memo_id is not None:
        try:
            existing = store.get_memo(memo_id, with_facets=False)
        except KeyError:
            existing = None
    if existing is not None:
        # 사용자가 대표 메모에 붙인 태그는 다시 취합해도 남긴다.
        kept = [t for t in TAG_RE.findall(existing["body"]) if t.casefold() != COLLECTION_TAG.casefold()]
        merged = list(dict.fromkeys(kept + tags))
        return store.update_memo(memo_id, memo_body(meta, headline, merged))["id"]
    external = f"collection:{cid}"
    memo_id = store.find_by_external(MEMO_SOURCE, external)
    body = memo_body(meta, headline, tags)
    if memo_id is None:
        memo_id = store.add_memo(body, source=MEMO_SOURCE, external_id=external)["id"]
    else:
        store.update_memo(memo_id, body)
    db.execute("UPDATE collections SET memo_id=? WHERE id=?", (memo_id, cid))
    return memo_id


# --------------------------------------------------------------------------- 조회


def _brief(stats_json: str | None) -> dict:
    if not stats_json:
        return {"files": 0, "rows": 0}
    summary = json.loads(stats_json)["summary"]
    return {"files": summary["files"], "rows": summary["included_rows"]}


def list_collections(limit: int = 50, offset: int = 0) -> dict:
    rows = db.query(
        "SELECT c.id, c.title, c.memo_id, c.current_version, c.updated_at, v.stats_json "
        "FROM collections c LEFT JOIN collection_versions v ON v.collection_id=c.id AND v.version=c.current_version "
        "ORDER BY c.updated_at DESC, c.id DESC LIMIT ? OFFSET ?", (limit + 1, offset))
    items = [{"id": r["id"], "title": r["title"], "memo_id": r["memo_id"], "version": r["current_version"],
              "updated_at": r["updated_at"], **_brief(r["stats_json"])} for r in rows[:limit]]
    return {"items": items, "has_more": len(rows) > limit}


def _collection(cid: int) -> dict:
    row = db.one("SELECT * FROM collections WHERE id=?", (cid,))
    if row is None:
        raise NotFound("취합 자료를 찾을 수 없습니다. 삭제되었을 수 있습니다.")
    return dict(row)


def reusable_rules(rules: dict) -> dict:
    """다시 취합할 때 쓸 규칙. 확인 표시는 버린다 — 새 구조가 나타나면 다시 확인해야 한다."""
    sheets = {}
    for key, value in (rules.get("sheets") or {}).items():
        kept = {k: v for k, v in value.items() if k in ("selected", "header_row", "mapping")}
        if kept:
            sheets[key] = kept
    return {"files": rules.get("files") or {}, "sheets": sheets}


def get(cid: int) -> dict:
    record = _collection(cid)
    versions = db.query("SELECT version, created_at, stats_json FROM collection_versions WHERE collection_id=? "
                        "ORDER BY version DESC", (cid,))
    return {"id": record["id"], "title": record["title"], "memo_id": record["memo_id"],
            "version": record["current_version"], "created_at": record["created_at"],
            "updated_at": record["updated_at"], "rules": reusable_rules(json.loads(record["rules_json"])),
            "versions": [{"version": v["version"], "created_at": v["created_at"], **_brief(v["stats_json"])}
                         for v in versions]}


def version_meta(cid: int, version: int | None = None) -> dict:
    record = _collection(cid)
    number = record["current_version"] if version is None else version
    row = db.one("SELECT * FROM collection_versions WHERE collection_id=? AND version=?", (cid, number))
    if row is None:
        raise NotFound("해당 취합 버전을 찾을 수 없습니다.")
    return {"id": cid, "title": record["title"], "memo_id": record["memo_id"],
            "current_version": record["current_version"], "version": number, "created_at": row["created_at"],
            "fields": json.loads(row["fields_json"]), "sources": json.loads(row["sources_json"]),
            "stats": json.loads(row["stats_json"]), "rules": json.loads(row["rules_json"])}


def decode(cells_json: str) -> list:
    return json.loads(cells_json)


def iter_rows(cid: int, version: int, *, included: bool | None = True):
    sql = "SELECT seq, src, row_no, included, reason, cells_json FROM collection_rows WHERE collection_id=? AND version=?"
    params: list = [cid, version]
    if included is not None:
        sql += " AND included=?"
        params.append(int(included))
    for row in db.connect().execute(sql + " ORDER BY seq", params):
        yield row["seq"], row["src"], row["row_no"], bool(row["included"]), row["reason"], decode(row["cells_json"])


def count_rows(cid: int, version: int, *, included: bool = True) -> int:
    row = db.one("SELECT COUNT(*) AS n FROM collection_rows WHERE collection_id=? AND version=? AND included=?",
                 (cid, version, int(included)))
    return int(row["n"])


def page(cid: int, version: int, offset: int, limit: int, *, included: bool = True) -> list:
    rows = db.query("SELECT seq, src, row_no, reason, cells_json FROM collection_rows "
                    "WHERE collection_id=? AND version=? AND included=? ORDER BY seq LIMIT ? OFFSET ?",
                    (cid, version, int(included), limit, offset))
    return [(r["seq"], r["src"], r["row_no"], r["reason"], decode(r["cells_json"])) for r in rows]


def delete(cid: int) -> dict:
    """표와 모든 버전을 지운다. 대표 메모와 이미 만든 AI 결과(당시 근거 사본 포함)는 남는다."""
    _collection(cid)
    conn = db.connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM collection_rows WHERE collection_id=?", (cid,))
        conn.execute("DELETE FROM collection_versions WHERE collection_id=?", (cid,))
        conn.execute("DELETE FROM collections WHERE id=?", (cid,))
    return {"ok": True}
