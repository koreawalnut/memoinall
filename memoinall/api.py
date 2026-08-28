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
    generate,
    importers,
    llm,
    organize,
    providers,
    search,
    settings,
    store,
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
        return store.add_memo(payload.get("body", ""), payload.get("source", "web"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/memos")
def list_memos(
    limit: int = 50,
    offset: int = 0,
    tag: str | None = None,
    person: str | None = None,
    since: str | None = None,
    until: str | None = None,
    archived: bool = False,
):
    return {"items": store.list_memos(limit=limit, offset=offset, tag=tag, person=person, since=since, until=until, archived=archived)}


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
    person: str | None = None,
    since: str | None = None,
    until: str | None = None,
):
    hits = search.search(q, limit=limit, tag=tag, person=person, since=since, until=until)
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


@app.get("/api/todos")
def get_todos(limit: int = 100):
    return {"items": store.open_todos(limit)}


@app.patch("/api/todos/{todo_id}")
def patch_todo(todo_id: int, payload: dict = Body(...)):
    store.toggle_todo(todo_id, bool(payload.get("done")))
    return {"ok": True}


# --------------------------------------------------------------------------- 가져오기


@app.get("/api/import/sources")
def import_sources(path: str | None = None):
    """각 소스의 가용 여부. UI 가 켜고 끌 판단을 여기서 한다."""
    out = []
    for imp in importers.all_importers(files_path=path):
        available = imp.available()
        out.append(
            {
                "name": imp.name,
                "label": imp.label,
                "available": available,
                "path": str(imp.path or ""),
                "reason": "" if available else imp.unavailable_reason(),
                "network": imp.name == "redmine",
            }
        )
    return {"sources": out}


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


def _run_imports(payload: dict, *, dry_run: bool):
    source = payload.get("source", "all")
    files_path = payload.get("path")
    min_chars = int(payload.get("min_chars") or 0)
    opts = {"files_path": files_path, **_redmine_opts(payload.get("redmine") or {})}
    try:
        targets = (
            importers.all_importers(**opts)
            if source == "all"
            else [importers.get_importer(source, **opts)]
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    update_existing = bool(payload.get("update_existing"))
    results = []
    for imp in targets:
        r = importers.run_import(
            imp, dry_run=dry_run, min_chars=min_chars, update_existing=update_existing
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
