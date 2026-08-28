"""CLI. 웹 UI 없이도 적고, 찾고, 컨텍스트를 뽑을 수 있게.

    python -m memoinall add "결제 모듈 타임아웃 다시 봐야 함 #결제"
    echo "긴 메모..." | python -m memoinall add -
    python -m memoinall search "결제 관련 걱정"
    python -m memoinall context "결제 이슈 정리해줘" --budget 3000 | clip
    python -m memoinall digest --period week
    python -m memoinall serve
"""

from __future__ import annotations

import argparse
import json
import sys

from . import config, context, db, embed, llm, organize, providers, search, settings, store


def _read_stdin() -> str:
    """파이프 입력을 직접 디코드한다.

    Windows 콘솔은 stdin 을 cp949 로 열어서 한글이 서러게이트로 깨진다.
    바이트로 받아 UTF-8 우선, 실패하면 시스템 인코딩으로 되돌린다.
    """
    raw = sys.stdin.buffer.read()
    # utf-8-sig 를 먼저 시도해 PowerShell 파이프가 붙이는 BOM 을 걷어낸다.
    for encoding in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _wait_for_enrich(memo_id: int) -> None:
    """CLI 는 프로세스가 바로 죽으므로 보강을 동기로 돌린다."""
    try:
        store.enrich(memo_id)
    except Exception as exc:
        print(f"(보강 실패: {exc})", file=sys.stderr)


def cmd_add(args) -> int:
    parts = args.text if isinstance(args.text, list) else [args.text]
    body = _read_stdin() if parts == ["-"] else " ".join(parts)
    memo = store.add_memo(body, source="cli")
    if not args.no_wait:
        embed.ensure_loaded_async()
        _wait_for_enrich(memo["id"])
        memo = store.get_memo(memo["id"])
    facets = memo.get("facets", {})
    print(f"[M{memo['id']}] {memo['title']}")
    if facets.get("tag"):
        print("  태그:", " ".join("#" + t for t in facets["tag"]))
    if memo.get("todos"):
        for t in memo["todos"]:
            print(f"  할일: {t['text']}" + (f" (마감 {t['due']})" if t["due"] else ""))
    return 0


def cmd_search(args) -> int:
    hits = search.search(args.query, limit=args.limit, tag=args.tag, person=args.person)
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2, default=str))
        return 0
    if not hits:
        print("결과 없음")
        return 1
    for h in hits:
        print(f"[M{h['id']}] {h['title']}  ({h['created_at'][:16]} · {h['why']})")
        print(f"    {h['snippet']}")
    return 0


def cmd_context(args) -> int:
    pack = context.build(args.query, budget_tokens=args.budget, limit=args.limit, full_body=args.full)
    if args.json:
        print(json.dumps(pack, ensure_ascii=False, indent=2, default=str))
    else:
        print(pack["prompt"])
    return 0


def cmd_ask(args) -> int:
    result = llm.answer(args.query, budget_tokens=args.budget)
    if result["answer"]:
        print(result["answer"])
    else:
        print(f"# {result['reason']}\n", file=sys.stderr)
        print(result["prompt"])
    return 0


def cmd_generate(args) -> int:
    from . import generate

    result = generate.generate(
        args.instruction,
        fmt=args.format,
        budget_tokens=args.budget,
        tag=args.tag,
        since=args.since,
        until=args.until,
    )
    print(f"# 검색 질의({result['query_method']}): {', '.join(result['queries'])}", file=sys.stderr)
    print(f"# 근거 {len(result['sources'])}건 / {result['used_tokens']}토큰", file=sys.stderr)
    if result["output"]:
        print(result["output"])
    else:
        print(f"# {result['reason']}\n", file=sys.stderr)
        print(result["prompt"])
    return 0


def cmd_settings(args) -> int:
    from . import llm, providers
    from . import settings as st

    if args.provider:
        if args.provider not in providers.SPECS:
            print(f"알 수 없는 프로바이더: {args.provider}\n사용 가능: {', '.join(providers.SPECS)}", file=sys.stderr)
            return 1
        st.set_many({"llm.provider": args.provider})
        llm.reset()
        print(f"프로바이더를 {providers.spec(args.provider).label} 로 바꿨습니다.")

    if args.set:
        updates = {}
        for pair in args.set:
            if "=" not in pair:
                print(f"형식 오류(key=value): {pair}", file=sys.stderr)
                return 1
            key, value = pair.split("=", 1)
            if key not in st.SCHEMA:
                print(f"알 수 없는 설정: {key}\n사용 가능: {', '.join(st.SCHEMA)}", file=sys.stderr)
                return 1
            updates[key] = value
        st.set_many(updates)
        llm.reset()
        print(f"{len(updates)}건 저장했습니다.")

    if args.test:
        result = llm.test_connection(args.test if args.test != "active" else None)
        print(("  ✓ " if result["ok"] else "  ✗ ") + result["message"])
        return 0 if result["ok"] else 1

    view = st.public_view()
    active = view["provider"]
    print("프로바이더:")
    for p in view["providers"]:
        mark = "▶" if p["name"] == active else " "
        state = "준비됨" if p["ready"] else ("키 필요" if p["needs_key"] else "서버 필요")
        print(f"  {mark} {p['name']:<10} {p['label']:<20} [{state}]")

    print("\n설정:")
    for key, info in view["settings"].items():
        # 지금 안 쓰는 프로바이더의 항목은 숨긴다 — 목록이 너무 길어진다.
        if key.startswith("llm.") and key.count(".") == 2 and not key.startswith(f"llm.{active}."):
            continue
        shown = info["value"] or "(없음)"
        print(f"  {key:<26} {shown:<30} [{info['source']}]")
    print(f"\n준비됨: {'예' if view['llm_ready'] else '아니오'}   (전체 보기: --all)")
    if args.all:
        print("\n전체 설정:")
        for key, info in view["settings"].items():
            print(f"  {key:<26} {(info['value'] or '(없음)'):<30} [{info['source']}]")
    return 0


def cmd_digest(args) -> int:
    result = llm.digest(args.period, args.anchor)
    print(result["digest"] or result["prompt"])
    return 0


def cmd_todos(args) -> int:
    todos = store.open_todos(args.limit)
    if not todos:
        print("미완 할일 없음")
        return 0
    for t in todos:
        due = f" (마감 {t['due']})" if t["due"] else ""
        print(f"[{t['id']:>4}] {t['text']}{due}  ← M{t['memo_id']}")
    return 0


def cmd_clusters(args) -> int:
    for c in organize.cluster(args.k):
        print(f"■ {c['label']} ({c['size']}건)")
        for m in c["memos"]:
            print(f"    [M{m['id']}] {m['title']}")
    return 0


def cmd_import(args) -> int:
    from . import importers

    opts = {
        "files_path": args.path,
        "redmine_kinds": args.redmine_kinds,
        "redmine_projects": args.redmine_projects,
        "redmine_limit": args.redmine_limit,
        "redmine_since": args.redmine_since,
        "redmine_include_notes": args.redmine_notes,
    }
    if args.source == "all":
        targets = importers.all_importers(**opts)
    else:
        targets = [importers.get_importer(args.source, **opts)]

    dry = not args.commit
    print("[미리보기 — 실제로 저장하지 않습니다. 저장하려면 --commit]\n" if dry else "[가져오기 실행]\n")

    total = 0
    pending: list[int] = []
    for importer in targets:
        result = importers.run_import(
            importer, dry_run=dry, background_enrich=False, min_chars=args.min_chars
        )
        pending.extend(result.memo_ids)
        head = f"■ {importer.label} ({result.source})"
        if not result.available:
            print(f"{head}\n   건너뜀: {result.error}\n")
            continue
        if result.error:
            print(f"{head}\n   오류: {result.error}\n")
            continue
        print(f"{head}\n   {result.path}")
        detail = f"본문없음 {result.skipped_empty} · 이미있음 {result.skipped_existing}"
        if result.skipped_short:
            detail += f" · 너무짧음 {result.skipped_short}"
        print(f"   원본 {result.found}건 → 대상 {result.imported}건 ({detail})")
        if dry and result.lengths:
            lens = sorted(result.lengths)
            buckets = [(20, 0), (50, 0), (200, 0)]
            counts = [sum(1 for n in lens if n < b) for b, _ in buckets]
            print(
                f"   본문 길이: 중앙값 {lens[len(lens) // 2]}자 · 최대 {lens[-1]}자 "
                f"(20자미만 {counts[0]} · 50자미만 {counts[1]} · 200자미만 {counts[2]})"
            )
        for s in result.samples:
            print(f"     · {s}")
        print()
        total += result.imported

    if dry:
        print(f"총 {total}건을 가져올 수 있습니다.  실행: python -m memoinall import --source {args.source} --commit")
        return 0

    print(f"총 {total}건 저장 완료.")
    if pending:
        # CLI 는 곧 끝나므로 백그라운드 워커에 맡기지 않고 여기서 끝낸다.
        print(f"임베딩·추출 {len(pending)}건 처리 중…")
        for done, memo_id in enumerate(pending, 1):
            try:
                store.enrich(memo_id)
            except Exception as exc:
                print(f"  M{memo_id} 보강 실패: {exc}", file=sys.stderr)
            if done % 10 == 0 or done == len(pending):
                print(f"  {done}/{len(pending)}")
        print("완료 — 이제 검색·클러스터링에 반영됩니다.")
    return 0


def cmd_stats(args) -> int:
    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_reindex(args) -> int:
    embed.ensure_loaded_async()
    rows = db.query("SELECT id FROM memos" if args.all else "SELECT id FROM memos WHERE enriched_at IS NULL")
    print(f"{len(rows)}건 재처리…")
    for i, row in enumerate(rows, 1):
        store.enrich(row["id"])
        if i % 20 == 0:
            print(f"  {i}/{len(rows)}")
    print("완료")
    return 0


def cmd_app(args) -> int:
    from .desktop import run

    return run()


def cmd_serve(args) -> int:
    import uvicorn

    print(f"http://{args.host}:{args.port}  (DB: {config.DB_PATH})")
    uvicorn.run("memoinall.api:app", host=args.host, port=args.port, reload=args.reload, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memoinall", description="무형식으로 적고 구조화해서 꺼내 쓰는 업무 메모")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="메모 추가 ('-' 면 stdin)")
    a.add_argument("text", nargs="+")
    a.add_argument("--no-wait", action="store_true", help="보강을 기다리지 않음")
    a.set_defaults(fn=cmd_add)

    s = sub.add_parser("search", help="하이브리드 검색")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--tag")
    s.add_argument("--person")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    c = sub.add_parser("context", help="LLM 용 컨텍스트 팩 출력")
    c.add_argument("query")
    c.add_argument("--budget", type=int, default=2000)
    c.add_argument("--limit", type=int, default=12)
    c.add_argument("--full", action="store_true", help="청크 대신 메모 전문 포함")
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_context)

    k = sub.add_parser("ask", help="메모 근거로 답변 (API 키 없으면 프롬프트만)")
    k.add_argument("query")
    k.add_argument("--budget", type=int, default=3000)
    k.set_defaults(fn=cmd_ask)

    g = sub.add_parser("generate", help="지시사항 + 메모 검색 → 결과물 생성")
    g.add_argument("instruction", help="예: '연수원 AX 세미나 내용으로 교육과정 기획안 초안 써줘'")
    from .generate import FORMATS

    g.add_argument("--format", default="auto", choices=list(FORMATS))
    g.add_argument("--budget", type=int, default=None, help="근거에 쓸 토큰 예산")
    g.add_argument("--tag")
    g.add_argument("--since")
    g.add_argument("--until")
    g.set_defaults(fn=cmd_generate)

    st = sub.add_parser("settings", help="설정 보기/변경 (예: --provider ollama)")
    st.add_argument("--provider", choices=list(providers.SPECS), help="사용할 LLM 프로바이더")
    st.add_argument("--set", action="append", metavar="KEY=VALUE",
                    help="예: llm.openai.api_key=sk-...  llm.ollama.model=qwen2.5")
    st.add_argument("--test", nargs="?", const="active", metavar="PROVIDER", help="연결 테스트")
    st.add_argument("--all", action="store_true", help="모든 프로바이더의 설정 표시")
    st.set_defaults(fn=cmd_settings)

    d = sub.add_parser("digest", help="기간 브리핑")
    d.add_argument("--period", choices=["day", "week", "month"], default="week")
    d.add_argument("--anchor", help="기준 날짜 YYYY-MM-DD")
    d.set_defaults(fn=cmd_digest)

    t = sub.add_parser("todos", help="미완 할일")
    t.add_argument("--limit", type=int, default=50)
    t.set_defaults(fn=cmd_todos)

    cl = sub.add_parser("clusters", help="자동 주제 묶음")
    cl.add_argument("-k", type=int, default=None)
    cl.set_defaults(fn=cmd_clusters)

    i = sub.add_parser("import", help="외부 메모 앱에서 일괄 가져오기")
    i.add_argument("--source", default="all", choices=["all", "sticky", "samsung", "redmine", "files"])
    i.add_argument("--path", help="files 소스의 폴더 경로")
    i.add_argument("--redmine-kinds", help="issues,wiki,documents,news 중 골라서 (기본은 설정값)")
    i.add_argument("--redmine-projects", help="프로젝트 식별자, 쉼표 구분 (비우면 전체)")
    i.add_argument("--redmine-limit", type=int, help="최대 건수")
    i.add_argument("--redmine-since", help="이 날짜 이후 갱신분만 (YYYY-MM-DD)")
    i.add_argument("--redmine-notes", action="store_true", help="이슈 코멘트도 포함(느림)")
    i.add_argument("--commit", action="store_true", help="실제로 저장 (기본은 미리보기)")
    i.add_argument("--min-chars", type=int, default=0, help="이 길이보다 짧은 메모는 건너뜀 (기본 0 = 전부)")
    i.set_defaults(fn=cmd_import)

    sub.add_parser("stats", help="상태").set_defaults(fn=cmd_stats)

    r = sub.add_parser("reindex", help="파생물 재생성")
    r.add_argument("--all", action="store_true")
    r.set_defaults(fn=cmd_reindex)

    sub.add_parser("app", help="데스크톱 창으로 실행 (브라우저 없이)").set_defaults(fn=cmd_app)

    v = sub.add_parser("serve", help="웹 서버 실행")
    v.add_argument("--host", default=config.HOST)
    v.add_argument("--port", type=int, default=config.PORT)
    v.add_argument("--reload", action="store_true")
    v.set_defaults(fn=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    # 콘솔 코드페이지가 좁아도(cp949 등) 출력이 죽지 않게. 저장된 데이터는 항상 UTF-8 이다.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    config.ensure_home()
    db.init()
    settings.migrate_legacy()
    # 임베딩을 건드리는 명령만 모델을 기다린다. stats/todos 는 기다릴 이유가 없다.
    if args.cmd in NEEDS_EMBEDDINGS:
        _await_embedder()
    return args.fn(args)


NEEDS_EMBEDDINGS = {"search", "context", "ask", "digest", "clusters", "import", "reindex", "generate"}


def _await_embedder(timeout: float = 180.0) -> None:
    """DB 에 저장된 임베딩과 같은 모델이 올라올 때까지 기다린다.

    이걸 안 기다리면 조용히 0건이 나온다. 저장된 청크는 실모델 벡터인데
    폴백 해시 임베더로 조회하면 모델명이 달라 아무것도 매칭되지 않기 때문이다.
    """
    import time

    row = db.one("SELECT model, COUNT(*) n FROM chunks GROUP BY model ORDER BY n DESC LIMIT 1")
    stored = row["model"] if row else None

    embed.ensure_loaded_async()
    deadline = time.monotonic() + timeout
    warned = False
    while time.monotonic() < deadline:
        state = embed.status()
        if state["state"] in {"ready", "failed", "fallback"}:
            break
        if not warned and stored and stored != embed.HASH_MODEL:
            print(f"임베딩 모델({config.EMBED_MODEL}) 로드 중… 저장된 벡터와 맞춰야 합니다.", file=sys.stderr)
            warned = True
        time.sleep(0.25)

    current = embed.current().name
    if stored and stored != current:
        print(
            f"경고: 저장된 임베딩은 '{stored}' 인데 지금은 '{current}' 입니다. "
            f"의미 검색 결과가 비어 보일 수 있습니다 — 'python -m memoinall reindex --all' 로 맞추세요.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    raise SystemExit(main())
