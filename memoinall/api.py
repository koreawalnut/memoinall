"""FastAPI 앱. 웹 UI + 외부 도구(LLM/스크립트)가 쓸 REST."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import (
    config,
    context,
    db,
    embed,
    exchange,
    generate,
    importers,
    llm,
    organize,
    providers,
    search,
    settings,
    store,
    tags,
)

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_home()
    db.init()
    settings.migrate_legacy()
    embed.on_upgrade(lambda name: store.reindex_pending())
    embed.ensure_loaded_async()
    store.start_worker()
    store.reindex_pending()
    yield


app = FastAPI(title="memoinall", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------- 메모


@app.post("/api/memos")
def create_memo(payload: dict = Body(...)):
    try:
        # tags 로 넘어온 것은 본문 끝에 #태그 로 붙인다. 태그의 원본은 늘 본문이라야
        # 내보낸 파일을 받은 쪽에서도 태그가 살아난다.
        body = tags.append_to_body(payload.get("body", ""), payload.get("tags") or [])
        return store.add_memo(body, payload.get("source", "web"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/memos")
def list_memos(
    limit: int = 50,
    offset: int = 0,
    tag: str | None = None,
    # 모듈 tags 를 가리지 않도록 이름을 달리 받는다 — 가려지면 이 함수 안에서
    # tags.xxx 가 조용히 문자열을 참조하게 된다.
    tag_csv: str | None = Query(None, alias="tags"),
    person: str | None = None,
    since: str | None = None,
    until: str | None = None,
    archived: bool = False,
):
    return {
        "items": store.list_memos(
            limit=limit, offset=offset, tag=tag, tags=_csv(tag_csv), person=person,
            since=since, until=until, archived=archived,
        )
    }


def _csv(value: str | None) -> list[str]:
    """쉼표로 붙여 보낸 태그 목록. 쿼리스트링에 배열을 넣는 것보다 다루기 쉽다."""
    return [t.strip().lstrip("#").strip() for t in str(value or "").split(",") if t.strip()]


@app.get("/api/memos/{memo_id}")
def read_memo(memo_id: int):
    try:
        memo = store.get_memo(memo_id)
    except KeyError:
        raise HTTPException(404, "메모를 찾을 수 없습니다.")
    memo["similar"] = [
        {"id": s["id"], "title": s["title"], "created_at": s["created_at"], "score": s["score"]}
        for s in search.similar(memo_id)
    ]
    return memo


@app.put("/api/memos/{memo_id}")
def edit_memo(memo_id: int, payload: dict = Body(...)):
    try:
        return store.update_memo(memo_id, payload.get("body", ""))
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@app.patch("/api/memos/{memo_id}")
def flag_memo(memo_id: int, payload: dict = Body(...)):
    return store.set_flag(memo_id, pinned=payload.get("pinned"), archived=payload.get("archived"))


@app.delete("/api/memos/{memo_id}")
def remove_memo(memo_id: int):
    store.delete_memo(memo_id)
    return {"ok": True}


# --------------------------------------------------------------------------- 검색/컨텍스트


@app.get("/api/search")
def do_search(
    q: str = Query(""),
    limit: int = 20,
    tag: str | None = None,
    tag_csv: str | None = Query(None, alias="tags"),
    person: str | None = None,
    since: str | None = None,
    until: str | None = None,
):
    hits = search.search(q, limit=limit, tag=tag, tags=_csv(tag_csv), person=person,
                         since=since, until=until)
    return {"query": q, "count": len(hits), "items": hits}


@app.get("/api/context")
def get_context(
    q: str,
    budget: int = 2000,
    limit: int = 12,
    tag: str | None = None,
    person: str | None = None,
    since: str | None = None,
    until: str | None = None,
    full: bool = False,
):
    return context.build(q, budget_tokens=budget, limit=limit, tag=tag, person=person, since=since, until=until, full_body=full)


@app.get("/api/context.txt", response_class=PlainTextResponse)
def get_context_text(q: str, budget: int = 2000, limit: int = 12, full: bool = False):
    """파이프로 바로 넘기기 좋은 평문 버전."""
    return context.build(q, budget_tokens=budget, limit=limit, full_body=full)["prompt"]


@app.post("/api/ask")
def ask(payload: dict = Body(...)):
    q = (payload.get("q") or "").strip()
    if not q:
        raise HTTPException(400, "q 가 필요합니다.")
    return llm.answer(q, budget_tokens=int(payload.get("budget", 3000)), limit=int(payload.get("limit", 12)))


# --------------------------------------------------------------------------- 정리


@app.get("/api/clusters")
def get_clusters(k: int | None = None, since: str | None = None, until: str | None = None):
    return {"clusters": organize.cluster(k, since=since, until=until)}


@app.get("/api/rollup")
def get_rollup(period: str = "week", anchor: str | None = None):
    data = organize.rollup(period, anchor)
    data["prompt"] = organize.rollup_prompt(data)
    return data


@app.post("/api/digest")
def make_digest(payload: dict = Body(default={})):
    return llm.digest(payload.get("period", "week"), payload.get("anchor"))


@app.get("/api/facets/{kind}")
def get_facets(kind: str, limit: int = 30):
    if kind not in {"tag", "person", "link", "date", "decision", "question"}:
        raise HTTPException(400, "알 수 없는 파셋 종류")
    return {"items": store.top_facets(kind, limit)}


# --------------------------------------------------------------------------- 내 태그


@app.get("/api/tags")
def get_tags():
    """정해 둔 태그 + 아직 등록 안 한 자주 쓰는 태그(권유용)."""
    return {
        "items": tags.all_tags(),
        "colors": tags.COLORS,
        "suggested": tags.unregistered(),
    }


@app.post("/api/tags")
def create_tag(payload: dict = Body(...)):
    try:
        return tags.add(payload.get("name", ""), color=payload.get("color", ""),
                        note=payload.get("note", ""))
    except tags.TagError as exc:
        raise HTTPException(400, str(exc))


@app.put("/api/tags/{name}")
def edit_tag(name: str, payload: dict = Body(...)):
    """이름·색·설명을 고친다. 이름을 바꾸면 메모 본문의 태그도 같이 바뀐다."""
    try:
        if payload.get("name") and payload["name"] != name:
            out = tags.rename(name, payload["name"])
            name = out["name"]
        else:
            out = tags.get(name)
        if payload.get("color") is not None or payload.get("note") is not None:
            out = {**tags.update(name, color=payload.get("color"), note=payload.get("note")),
                   "memos_changed": out.get("memos_changed", 0)}
        return out
    except tags.TagError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/tags/{name}")
def delete_tag(name: str, purge: bool = False):
    """목록에서 뺀다. purge=true 면 메모 본문에서도 지운다(되돌릴 수 없음)."""
    try:
        return tags.remove(name, purge=purge)
    except tags.TagError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/tags/order")
def order_tags(payload: dict = Body(...)):
    order = payload.get("order")
    if not isinstance(order, list):
        raise HTTPException(400, "order 는 태그 이름 목록이어야 합니다.")
    return {"items": tags.reorder([str(x) for x in order])}


@app.post("/api/tags/adopt")
def adopt_tags(payload: dict = Body(...)):
    """이미 본문에 쓰던 태그를 목록으로 데려온다."""
    names = payload.get("names")
    if not isinstance(names, list):
        raise HTTPException(400, "names 는 태그 이름 목록이어야 합니다.")
    return {"added": tags.adopt([str(x) for x in names]), "items": tags.all_tags()}


@app.get("/api/todos")
def get_todos(limit: int = 100):
    return {"items": store.open_todos(limit)}


@app.patch("/api/todos/{todo_id}")
def patch_todo(todo_id: int, payload: dict = Body(...)):
    store.toggle_todo(todo_id, bool(payload.get("done")))
    return {"ok": True}


# --------------------------------------------------------------------------- 가져오기


@app.get("/api/import/sources")
def import_sources(path: str | None = None, shared: str | None = None):
    """각 소스의 가용 여부. UI 가 켜고 끌 판단을 여기서 한다."""
    out = []
    for imp in importers.all_importers(files_path=path, shared_path=shared):
        available = imp.available()
        out.append(
            {
                "name": imp.name,
                "label": imp.label,
                "available": available,
                "path": str(imp.path or ""),
                "reason": "" if available else imp.unavailable_reason(),
                "network": imp.name == "redmine",
                # 이미 가져와 있는 양. 다시 가져올지 지울지 판단할 근거가 된다.
                "stored": store.count_by_source(imp.name),
            }
        )
    return {"sources": out}


@app.post("/api/import/reset")
def import_reset(payload: dict = Body(default={})):
    """한 소스에서 가져온 메모를 통째로 지운다.

    되돌릴 수 없으므로 confirm 을 명시적으로 받는다. 지우는 대상은 임포터가
    만든 source 로 한정한다 — 손으로 쓴 메모는 이 경로로 지워지면 안 된다.
    """
    source = str(payload.get("source") or "")
    if source not in importers.SOURCE_NAMES:
        raise HTTPException(
            400,
            f"초기화할 수 없는 소스: {source or '(없음)'} "
            f"(가능: {', '.join(importers.SOURCE_NAMES)})",
        )
    before = store.count_by_source(source)
    if not payload.get("confirm"):
        # 미리보기 — 몇 건이 지워질지만 알려주고 아무것도 건드리지 않는다.
        return {"deleted": 0, "confirmed": False, "stored": before}
    deleted = store.delete_by_source(source)
    return {"deleted": deleted, "confirmed": True, "stored": store.count_by_source(source)}


@app.get("/api/import/sticky/raw")
def sticky_raw(limit: int = 3):
    """가공 전 원문 — 저장 형식이 예상과 다를 때 원인을 눈으로 확인하려고."""
    imp = importers.get_importer("sticky")
    if not imp.available():
        raise HTTPException(400, imp.unavailable_reason())
    return {"rows": imp.raw_rows(max(1, min(limit, 20)))}


@app.post("/api/import/redmine/test")
def redmine_test(payload: dict = Body(default={})):
    """가져오기 전에 주소·키를 확인한다. 저장 전 입력값으로도 시험할 수 있다."""
    return importers.build_redmine(**_redmine_opts(payload)).test_connection()


@app.post("/api/import/redmine/projects")
def redmine_projects(payload: dict = Body(default={})):
    imp = importers.build_redmine(**_redmine_opts(payload))
    if not imp.available():
        raise HTTPException(400, imp.unavailable_reason())
    return {"projects": imp.list_projects()}


def _redmine_opts(payload: dict) -> dict:
    """UI 가 저장 전 값을 그대로 넘길 수 있게 매핑한다."""
    keys = ("url", "api_key", "projects", "kinds", "limit", "since", "include_notes")
    return {f"redmine_{k}": payload.get(k) for k in keys if payload.get(k) is not None}


# --------------------------------------------------------------------------- 내보내기


def _export_filters(payload: dict) -> dict:
    ids = payload.get("ids")
    return {
        "ids": [int(i) for i in ids] if isinstance(ids, list) else None,
        "q": str(payload.get("q") or ""),
        "tag": payload.get("tag") or None,
        "tags": [str(t).lstrip("#") for t in (payload.get("tags") or []) if str(t).strip()],
        "person": payload.get("person") or None,
        "source": payload.get("source") or None,
        "since": payload.get("since") or None,
        "until": payload.get("until") or None,
        "archived": bool(payload.get("archived")),
        "limit": max(1, min(int(payload.get("limit") or 10000), 100000)),
    }


@app.post("/api/export/preview")
def export_preview(payload: dict = Body(default={})):
    """무엇이 나갈지 먼저 보여준다. 남에게 보내는 파일이라 내용 확인이 필요하다."""
    rows = exchange.select(**_export_filters(payload))
    body = exchange.dumps(exchange.build(rows, note=str(payload.get("note") or "")))
    return {
        "count": len(rows),
        "bytes": len(body.encode("utf-8")),
        "filename": exchange.default_filename(len(rows)),
        "default_dir": str(exchange.export_dir()),
        "items": [
            {
                "id": m["id"],
                "title": m["title"],
                "created_at": m["created_at"],
                "source": m.get("source") or "",
            }
            for m in rows[:30]
        ],
    }


@app.post("/api/export")
def export_run(payload: dict = Body(default={})):
    """파일로 저장. 경로를 안 주면 다운로드 폴더에 기본 이름으로 만든다."""
    rows = exchange.select(**_export_filters(payload))
    if not rows:
        raise HTTPException(400, "내보낼 메모가 없습니다 — 조건을 확인하세요.")
    pack = exchange.build(rows, note=str(payload.get("note") or ""))
    target = str(payload.get("path") or "").strip() or exchange.export_dir()
    try:
        saved = exchange.save(pack, target)
    except OSError as exc:
        raise HTTPException(400, f"파일을 저장하지 못했습니다: {exc}")
    return {"path": str(saved), "count": pack["count"], "bytes": saved.stat().st_size}


@app.post("/api/export/content")
def export_content(payload: dict = Body(default={})):
    """파일 대신 내용 그대로. 메신저에 붙여넣어 보낼 때 쓴다."""
    rows = exchange.select(**_export_filters(payload))
    if not rows:
        raise HTTPException(400, "내보낼 메모가 없습니다 — 조건을 확인하세요.")
    pack = exchange.build(rows, note=str(payload.get("note") or ""))
    return {
        "content": exchange.dumps(pack),
        "count": pack["count"],
        "filename": exchange.default_filename(pack["count"]),
    }


@app.post("/api/import/shared/describe")
def shared_describe(payload: dict = Body(default={})):
    """넣기 전에 파일 겉면만 확인 — 언제 만든 몇 건짜리인지."""
    imp = importers.build_shared(
        shared_path=payload.get("path"), shared_content=payload.get("content")
    )
    if not imp.available():
        raise HTTPException(400, imp.unavailable_reason())
    try:
        return imp.describe()
    except exchange.ExchangeError as exc:
        raise HTTPException(400, str(exc))


def _run_imports(payload: dict, *, dry_run: bool):
    source = payload.get("source", "all")
    files_path = payload.get("path")
    min_chars = int(payload.get("min_chars") or 0)
    opts = {
        "files_path": files_path,
        "shared_path": payload.get("shared_path"),
        "shared_content": payload.get("shared_content"),
        **_redmine_opts(payload.get("redmine") or {}),
    }
    try:
        targets = (
            importers.all_importers(**opts)
            if source == "all"
            else [importers.get_importer(source, **opts)]
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    update_existing = bool(payload.get("update_existing"))
    # 이번에 가져온 것에 붙일 태그. 나중에 '이건 어디서 온 건지' 골라내는 손잡이가 된다.
    extra_tags = []
    for raw in payload.get("tags") or []:
        try:
            extra_tags.append(tags.normalize(str(raw)))
        except tags.TagError as exc:
            raise HTTPException(400, str(exc))

    results = []
    for imp in targets:
        r = importers.run_import(
            imp, dry_run=dry_run, min_chars=min_chars, update_existing=update_existing,
            extra_tags=extra_tags,
        )
        lengths = sorted(r.lengths)
        results.append(
            {
                "source": r.source,
                "label": imp.label,
                "available": r.available,
                "path": r.path,
                "found": r.found,
                "importable": r.imported,
                "updated": r.updated,
                "unchanged": r.unchanged,
                "skipped_existing": r.skipped_existing,
                "skipped_duplicate": r.skipped_duplicate,
                "skipped_empty": r.skipped_empty,
                "skipped_short": r.skipped_short,
                "error": r.error,
                "samples": r.samples,
                "median_chars": lengths[len(lengths) // 2] if lengths else 0,
                "max_chars": lengths[-1] if lengths else 0,
            }
        )
    return {
        "dry_run": dry_run,
        "update_existing": update_existing,
        "tags": extra_tags,
        "results": results,
        "total": sum(r["importable"] for r in results),
        "total_updated": sum(r["updated"] for r in results),
    }


@app.post("/api/import/preview")
def import_preview(payload: dict = Body(default={})):
    return _run_imports(payload, dry_run=True)


@app.post("/api/import/run")
def import_run(payload: dict = Body(default={})):
    """실제 저장. 보강은 백그라운드 워커가 처리한다(서버는 계속 살아 있으므로)."""
    return _run_imports(payload, dry_run=False)


# --------------------------------------------------------------------------- 설정


@app.get("/api/settings")
def read_settings():
    return settings.public_view()


@app.put("/api/settings")
def write_settings(payload: dict = Body(...)):
    values = payload.get("values") or {}
    unknown = [k for k in values if k not in settings.SCHEMA]
    if unknown:
        raise HTTPException(400, f"알 수 없는 설정: {', '.join(unknown)}")
    if "llm.provider" in values and values["llm.provider"] not in providers.SPECS:
        raise HTTPException(400, f"알 수 없는 프로바이더: {values['llm.provider']}")
    settings.set_many(values)
    llm.reset()  # 캐시된 어댑터를 버려야 새 설정이 즉시 반영된다
    return settings.public_view()


@app.get("/api/providers")
def list_providers():
    return {
        "active": settings.provider_name(),
        "providers": [
            {**s, "ready": settings.provider_ready(s["name"])} for s in providers.public_specs()
        ],
    }


@app.get("/api/providers/{name}/models")
def provider_models(name: str, base_url: str | None = None, api_key: str | None = None):
    """서버에 실제로 설치·제공되는 모델 목록.

    하드코딩한 후보는 금방 틀린다 — 특히 Ollama 는 사용자가 받은 모델만 있다.
    조회에 실패하면 정적 후보로 되돌아간다.
    """
    try:
        spec = providers.spec(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    cfg = settings.provider_config(name)
    if base_url:
        cfg["base_url"] = base_url
    if api_key:
        cfg["api_key"] = api_key
    try:
        live = providers.build(name, cfg).list_models()
    except Exception:
        live = []
    return {"models": live or spec.model_choices, "live": bool(live)}


@app.post("/api/settings/test")
def test_llm(payload: dict = Body(default={})):
    """저장하기 전의 입력값으로도 테스트할 수 있다 — 잘못된 키를 커밋하지 않도록."""
    overrides = {k: payload.get(k) for k in ("api_key", "model", "base_url") if payload.get(k)}
    return llm.test_connection(payload.get("provider"), overrides)


# --------------------------------------------------------------------------- 생성


@app.get("/api/generate/formats")
def generate_formats():
    return {
        "formats": [{"key": k, **v} for k, v in generate.FORMATS.items()],
        "llm_ready": llm.available(),
        "provider": llm.current_label(),
    }


@app.post("/api/generate/plan")
def generate_plan(payload: dict = Body(...)):
    """지시사항 → 검색 질의 + 근거 + 프롬프트. 생성은 하지 않는다."""
    instruction = (payload.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(400, "instruction 이 필요합니다.")
    return generate.build_pack(
        instruction,
        queries=payload.get("queries") or None,
        budget_tokens=payload.get("budget"),
        fmt=payload.get("format", "auto"),
        tag=payload.get("tag"),
        tags=[str(t) for t in (payload.get("tags") or [])],
        person=payload.get("person"),
        since=payload.get("since"),
        until=payload.get("until"),
    )


@app.post("/api/generate")
def do_generate(payload: dict = Body(...)):
    instruction = (payload.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(400, "instruction 이 필요합니다.")
    return generate.generate(
        instruction,
        queries=payload.get("queries") or None,
        budget_tokens=payload.get("budget"),
        fmt=payload.get("format", "auto"),
        tag=payload.get("tag"),
        tags=[str(t) for t in (payload.get("tags") or [])],
        person=payload.get("person"),
        since=payload.get("since"),
        until=payload.get("until"),
        effort=payload.get("effort"),
    )


# --------------------------------------------------------------------------- 시스템


@app.get("/api/stats")
def get_stats():
    s = store.stats()
    s["queue"] = store.queue_size()
    return s


@app.post("/api/reindex")
def reindex(payload: dict = Body(default={})):
    n = store.reindex_all() if payload.get("all") else store.reindex_pending()
    return {"queued": n}


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")
