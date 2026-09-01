"""HTTP 계층 스모크.  python tests/test_api.py

라우트를 실제로 호출해 상태코드와 응답 형태를 확인한다.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

TMP = tempfile.mkdtemp(prefix="memoinall-api-")
os.environ["MEMOINALL_HOME"] = TMP
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from memoinall.api import app  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {extra}")


def wait_idle(c, timeout=20.0):
    """백그라운드 보강 워커가 큐를 비울 때까지."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = c.get("/api/stats").json()
        if s["pending"] == 0 and s["queue"] == 0:
            return True
        time.sleep(0.2)
    return False


def main() -> int:
    with TestClient(app) as c:
        r = c.post("/api/memos", json={"body": "결제 모듈 타임아웃 @김민수 확인해야 함 #결제\n[ ] 로그 확인"})
        check("POST /api/memos", r.status_code == 200, r.status_code)
        mid = r.json()["id"]
        c.post("/api/memos", json={"body": "온보딩 3단계로 줄이자. 결정: 3단계 축소 #기획"})
        c.post("/api/memos", json={"body": "PG사 지연이 결제 실패 원인인 듯. 재시도 정책 다시 볼 것 #결제"})

        check("POST 빈 본문 400", c.post("/api/memos", json={"body": ""}).status_code == 400)
        check("백그라운드 보강 완료", wait_idle(c))

        check("GET /api/memos", len(c.get("/api/memos").json()["items"]) == 3)
        d = c.get(f"/api/memos/{mid}").json()
        check("파셋 추출", "결제" in d["facets"].get("tag", []), d["facets"].get("tag"))
        check("similar 포함", "similar" in d)
        check("404", c.get("/api/memos/99999").status_code == 404)

        s = c.get("/api/search", params={"q": "결제 지연"}).json()
        check("GET /api/search", s["count"] > 0, s["count"])
        check("why 배지", s["items"][0].get("why"), s["items"][0].get("why"))

        ctx = c.get("/api/context", params={"q": "결제 뭐가 문제였지", "budget": 800}).json()
        check("GET /api/context", len(ctx["sources"]) > 0 and ctx["used_tokens"] <= 800, ctx["used_tokens"])
        check("GET /api/context.txt", "[M" in c.get("/api/context.txt", params={"q": "결제"}).text)

        # --- 내 태그 ---
        check("GET /api/tags 빈 목록", c.get("/api/tags").json()["items"] == [])
        check("색 목록 제공", len(c.get("/api/tags").json()["colors"]) >= 4)
        check("이미 쓰는 태그 제안", "결제" in
              [t["value"] for t in c.get("/api/tags").json()["suggested"]])
        made = c.post("/api/tags", json={"name": "#결제", "color": "blue"})
        check("POST /api/tags (# 도 받음)", made.json()["name"] == "결제", made.json())
        check("중복 400", c.post("/api/tags", json={"name": "결제"}).status_code == 400)
        check("빈 이름 400", c.post("/api/tags", json={"name": " "}).status_code == 400)
        check("못 쓰는 문자 400", c.post("/api/tags", json={"name": "결제!"}).status_code == 400)
        c.post("/api/tags", json={"name": "기획"})
        check("건수 포함",
              [t for t in c.get("/api/tags").json()["items"] if t["name"] == "결제"][0]["count"] == 2)
        check("순서 바꾸기", [t["name"] for t in
              c.post("/api/tags/order", json={"order": ["기획", "결제"]}).json()["items"]] == ["기획", "결제"])
        check("order 가 목록이 아니면 400", c.post("/api/tags/order", json={"order": "결제"}).status_code == 400)
        check("색 바꾸기", c.put("/api/tags/결제", json={"color": "green"}).json()["color"] == "green")

        # 조회: 태그 여러 개는 AND
        both = c.get("/api/memos", params={"tags": "결제,기획"}).json()["items"]
        one = c.get("/api/memos", params={"tags": "결제"}).json()["items"]
        check("tags= 로 AND 조회", len(both) < len(one) and len(one) == 2, (len(both), len(one)))
        check("검색에도 tags=", all("결제" in m["body"] for m in
              c.get("/api/search", params={"q": "", "tags": "결제"}).json()["items"]))
        check("태그로 내보내기 좁히기",
              c.post("/api/export/preview", json={"tags": ["결제"]}).json()["count"] == 2)
        check("두 태그면 더 좁아짐",
              c.post("/api/export/preview", json={"tags": ["결제", "기획"]}).json()["count"] < 2)

        # 메모 만들 때 태그 붙이기
        tagged = c.post("/api/memos", json={"body": "태그 붙여 저장", "tags": ["결제"]}).json()
        check("POST /api/memos tags", tagged["body"].endswith("#결제"), tagged["body"])
        c.delete(f"/api/memos/{tagged['id']}")

        # 이름 바꾸기·purge 는 본문을 고치므로 전용 메모로만 시험한다 —
        # 공용 메모를 건드리면 뒤따르는 검사들이 엉뚱하게 깨진다.
        c.post("/api/tags", json={"name": "임시태그"})
        tmp_memo = c.post("/api/memos", json={"body": "이름 바꾸기 시험용 메모", "tags": ["임시태그"]}).json()
        wait_idle(c)
        ren = c.put("/api/tags/임시태그", json={"name": "임시태그2"}).json()
        check("PUT 이름 바꾸기", ren["name"] == "임시태그2" and ren["memos_changed"] == 1, ren)
        check("본문이 바뀜", "#임시태그2" in c.get(f"/api/memos/{tmp_memo['id']}").json()["body"])
        check("옛 이름 조회 안 됨", c.get("/api/memos", params={"tags": "임시태그"}).json()["items"] == [])
        check("없는 태그 400", c.put("/api/tags/없음", json={"color": "red"}).status_code == 400)

        gone = c.delete("/api/tags/임시태그2").json()
        check("DELETE 는 목록만", gone["purged"] is False and gone["memos_changed"] == 0, gone)
        check("메모의 태그는 남음",
              len(c.get("/api/memos", params={"tags": "임시태그2"}).json()["items"]) == 1)
        check("없는 태그 삭제 400", c.delete("/api/tags/없음").status_code == 400)
        check("데려오기", c.post("/api/tags/adopt",
              json={"names": ["임시태그2"]}).json()["added"][0]["name"] == "임시태그2")
        purged = c.delete("/api/tags/임시태그2", params={"purge": "true"}).json()
        check("purge 는 본문도", purged["purged"] and purged["memos_changed"] == 1, purged)
        check("purge 뒤엔 조회 안 됨",
              c.get("/api/memos", params={"tags": "임시태그2"}).json()["items"] == [])
        check("본문 내용은 남음", "이름 바꾸기 시험용"
              in c.get(f"/api/memos/{tmp_memo['id']}").json()["body"])
        c.delete(f"/api/memos/{tmp_memo['id']}")
        c.delete("/api/tags/결제")
        c.delete("/api/tags/기획")

        check("GET /api/facets/tag", len(c.get("/api/facets/tag").json()["items"]) >= 2)
        check("GET /api/facets 잘못된 종류 400", c.get("/api/facets/nope").status_code == 400)

        todos = c.get("/api/todos").json()["items"]
        check("GET /api/todos", len(todos) > 0, len(todos))
        check("PATCH /api/todos", c.patch(f"/api/todos/{todos[0]['id']}", json={"done": True}).status_code == 200)

        check("GET /api/clusters", "clusters" in c.get("/api/clusters").json())
        ro = c.get("/api/rollup", params={"period": "month"}).json()
        check("GET /api/rollup", ro["memo_count"] == 3 and "prompt" in ro, ro["memo_count"])

        ask = c.post("/api/ask", json={"q": "결제 이슈 정리해줘"}).json()
        check("POST /api/ask (키 없으면 팩만)", ask["answer"] is None and ask["prompt"])
        check("POST /api/ask 빈 질문 400", c.post("/api/ask", json={"q": ""}).status_code == 400)
        check("POST /api/digest", c.post("/api/digest", json={"period": "month"}).json()["prompt"])

        check("PUT /api/memos", c.put(f"/api/memos/{mid}", json={"body": "수정됨 #결제"}).status_code == 200)
        check("PUT 빈 본문 400", c.put(f"/api/memos/{mid}", json={"body": "  "}).status_code == 400)
        check("PATCH pinned", c.patch(f"/api/memos/{mid}", json={"pinned": True}).json()["pinned"] == 1)
        check("POST /api/reindex", "queued" in c.post("/api/reindex", json={"all": True}).json())
        check("DELETE", c.delete(f"/api/memos/{mid}").status_code == 200)

        # --- 가져오기 ---
        srcs = c.get("/api/import/sources").json()["sources"]
        check("GET /api/import/sources", {s["name"] for s in srcs} >= {"sticky", "samsung"},
              [s["name"] for s in srcs])
        check("가용여부/사유 포함", all("available" in s and ("path" in s or "reason" in s) for s in srcs))

        import tempfile
        folder = Path(tempfile.mkdtemp(prefix="memoinall-apiimp-"))
        (folder / "a.md").write_text("가져오기 테스트 메모입니다 #임포트", encoding="utf-8")
        before = c.get("/api/stats").json()["memos"]
        prev = c.post("/api/import/preview", json={"source": "files", "path": str(folder)}).json()
        check("POST /api/import/preview", prev["dry_run"] and prev["total"] == 1, prev["total"])
        check("미리보기는 저장 안 함", c.get("/api/stats").json()["memos"] == before)
        run = c.post("/api/import/run", json={"source": "files", "path": str(folder)}).json()
        check("POST /api/import/run", run["dry_run"] is False and run["total"] == 1)
        check("실제 저장됨", c.get("/api/stats").json()["memos"] == before + 1)
        again = c.post("/api/import/run", json={"source": "files", "path": str(folder)}).json()
        check("재실행 멱등", again["total"] == 0, again["total"])
        check("min_chars 필터", c.post("/api/import/preview",
              json={"source": "files", "path": str(folder), "min_chars": 500}).json()["total"] == 0)
        check("잘못된 소스 400", c.post("/api/import/preview", json={"source": "없음"}).status_code == 400)

        # 항목별 가져오기 — 지정한 소스만 돌아야 한다
        one = c.post("/api/import/preview", json={"source": "samsung"}).json()
        check("개별 실행은 한 소스만", [x["source"] for x in one["results"]] == ["samsung"],
              [x["source"] for x in one["results"]])
        every = c.post("/api/import/preview", json={"source": "all"}).json()
        check("일괄은 전 소스",
              {x["source"] for x in every["results"]} == {"sticky", "samsung", "redmine", "files", "shared"},
              [x["source"] for x in every["results"]])
        check("결과에 source 키 존재", all("source" in x for x in every["results"]))
        # 회귀: 경로 없는 files 가 400 이면 UI 가 사유를 못 보여준다
        nofolder = c.post("/api/import/preview", json={"source": "files"})
        check("경로 없는 files 는 200 + 사유", nofolder.status_code == 200
              and "지정" in nofolder.json()["results"][0]["error"], nofolder.status_code)
        check("경로 없는 files 는 0건", nofolder.json()["total"] == 0)

        # --- 소스별 초기화 ---
        files_src = [s for s in c.get("/api/import/sources").json()["sources"] if s["name"] == "files"][0]
        check("sources 에 저장 건수", files_src["stored"]["memos"] == 1, files_src.get("stored"))
        check("저장 기간도 함께", bool(files_src["stored"]["first"]), files_src["stored"])

        before_reset = c.get("/api/stats").json()["memos"]
        # 손으로 쓴 메모는 절대 지워지면 안 된다 — 초기화 대상은 임포터 소스뿐
        keep = c.post("/api/memos", json={"body": "손으로 쓴 메모는 남아야 합니다"}).json()["id"]
        check("web 소스 초기화 거부", c.post("/api/import/reset", json={"source": "web"}).status_code == 400)
        check("빈 소스 초기화 거부", c.post("/api/import/reset", json={}).status_code == 400)

        dryr = c.post("/api/import/reset", json={"source": "files"}).json()
        check("confirm 없으면 미리보기", dryr["deleted"] == 0 and dryr["confirmed"] is False, dryr)
        check("미리보기는 안 지움", c.get("/api/stats").json()["memos"] == before_reset + 1)

        gone = c.post("/api/import/reset", json={"source": "files", "confirm": True}).json()
        check("초기화 실행", gone["deleted"] == 1 and gone["stored"]["memos"] == 0, gone)
        check("메모 수 감소", c.get("/api/stats").json()["memos"] == before_reset)
        check("직접 쓴 메모는 생존", c.get(f"/api/memos/{keep}").status_code == 200)
        # 회귀: FTS 는 외래키가 안 걸린다 — 안 지우면 유령이 검색에 남는다
        hits = c.get("/api/search", params={"q": "가져오기 테스트 메모"}).json()["items"]
        check("지운 메모는 검색에 안 잡힘",
              all("가져오기 테스트" not in h["body"] for h in hits), len(hits))
        empty = c.post("/api/import/reset", json={"source": "files", "confirm": True}).json()
        check("빈 소스 재실행 무해", empty["deleted"] == 0, empty)
        c.delete(f"/api/memos/{keep}")

        # 초기화 뒤 다시 가져올 수 있어야 한다 (external_id 유니크 인덱스 잔재 확인)
        readd = c.post("/api/import/run", json={"source": "files", "path": str(folder)}).json()
        check("초기화 후 재수집 가능", readd["total"] == 1, readd["total"])
        c.post("/api/import/reset", json={"source": "files", "confirm": True})
        shutil.rmtree(folder, ignore_errors=True)

        # --- 내보내기 · 받은 파일 넣기 ---
        import json as _json

        out = Path(tempfile.mkdtemp(prefix="memoinall-exp-"))
        pv = c.post("/api/export/preview", json={}).json()
        total_now = c.get("/api/stats").json()["memos"]
        check("POST /api/export/preview 전체", pv["count"] == total_now, (pv["count"], total_now))
        check("미리보기에 목록·파일명", pv["items"] and pv["filename"].endswith(".json"), pv.get("filename"))
        narrow = c.post("/api/export/preview", json={"tag": "결제"}).json()
        check("태그로 좁히기", 0 < narrow["count"] < pv["count"], (narrow["count"], pv["count"]))
        check("맞는 게 없으면 0건", c.post("/api/export/preview", json={"tag": "없는태그"}).json()["count"] == 0)

        ex = c.post("/api/export", json={"path": str(out), "note": "공유합니다"}).json()
        check("POST /api/export", ex["count"] == total_now and Path(ex["path"]).exists(), ex)
        pack = _json.loads(Path(ex["path"]).read_text(encoding="utf-8"))
        check("파일 형식", pack["format"] == "memoinall/memos" and pack["note"] == "공유합니다")
        check("비밀값이 안 섞임", "api_key" not in Path(ex["path"]).read_text(encoding="utf-8"))
        check("빈 결과는 400", c.post("/api/export", json={"tag": "없는태그"}).status_code == 400)
        cp = c.post("/api/export/content", json={"limit": 1}).json()
        check("POST /api/export/content", _json.loads(cp["content"])["count"] == 1, cp["count"])

        desc = c.post("/api/import/shared/describe", json={"path": ex["path"]}).json()
        check("받은 파일 겉면", desc["count"] == total_now and desc["note"] == "공유합니다", desc)
        check("경로 없으면 400", c.post("/api/import/shared/describe", json={}).status_code == 400)
        junk = out / "junk.json"
        junk.write_text("not json at all", encoding="utf-8")
        check("깨진 파일 400", c.post("/api/import/shared/describe",
              json={"path": str(junk)}).status_code == 400)

        # 자기 메모를 자기가 다시 받으면 전부 '같은 내용'으로 걸러져야 한다
        back = c.post("/api/import/run", json={"source": "shared", "shared_path": ex["path"]}).json()
        res = back["results"][0]
        check("자기 파일은 전부 중복", res["skipped_duplicate"] == total_now and res["importable"] == 0, res)
        check("메모 수 그대로", c.get("/api/stats").json()["memos"] == total_now)
        # 밖에서 온 새 메모는 들어와야 한다
        outside = out / "outside.json"
        outside.write_text(_json.dumps({"format": "memoinall/memos", "version": 1, "memos": [
            {"body": "동료가 보낸 새 메모입니다 #공유", "created_at": "2026-04-01T09:00:00"}]},
            ensure_ascii=False), encoding="utf-8")
        got = c.post("/api/import/run", json={"source": "shared", "shared_path": str(outside)}).json()
        check("새 메모는 들어옴", got["total"] == 1, got["total"])
        check("소스가 shared",
              [s for s in c.get("/api/import/sources").json()["sources"]
               if s["name"] == "shared"][0]["stored"]["memos"] == 1)
        c.post("/api/import/reset", json={"source": "shared", "confirm": True})
        shutil.rmtree(out, ignore_errors=True)

        # --- 설정 · 프로바이더 ---
        cfg = c.get("/api/settings").json()
        check("GET /api/settings", "llm.anthropic.model" in cfg["settings"] and "providers" in cfg)
        check("비밀값 마스킹 구조", cfg["settings"]["llm.anthropic.api_key"]["secret"] is True)
        check("프로바이더 4종", len(cfg["providers"]) == 4, [p["name"] for p in cfg["providers"]])
        upd = c.put("/api/settings", json={"values": {"llm.anthropic.effort": "max"}}).json()
        check("PUT /api/settings", upd["settings"]["llm.anthropic.effort"]["value"] == "max")
        check("effort 는 프로바이더별", upd["settings"]["llm.ollama.effort"]["value"] == "none",
              upd["settings"]["llm.ollama.effort"]["value"])
        check("알 수 없는 키 400", c.put("/api/settings", json={"values": {"nope": "1"}}).status_code == 400)
        check("알 수 없는 프로바이더 400",
              c.put("/api/settings", json={"values": {"llm.provider": "없음"}}).status_code == 400)

        provs = c.get("/api/providers").json()
        check("GET /api/providers", provs["active"] == "anthropic" and len(provs["providers"]) == 4)
        check("Ollama 는 키 없이 ready", next(p for p in provs["providers"] if p["name"] == "ollama")["ready"] is True)

        sw = c.put("/api/settings", json={"values": {"llm.provider": "ollama", "llm.ollama.model": "qwen2.5"}}).json()
        check("프로바이더 전환", sw["provider"] == "ollama" and sw["llm_ready"] is True)
        check("전환이 stats 에 반영", c.get("/api/stats").json()["llm_provider"] == "ollama")
        check("전환이 생성 탭에 반영", "Ollama" in c.get("/api/generate/formats").json()["provider"])
        c.put("/api/settings", json={"values": {"llm.provider": "anthropic"}})

        check("POST /api/settings/test (키 없음)", c.post("/api/settings/test").json()["ok"] is False)
        check("특정 프로바이더 테스트", c.post("/api/settings/test", json={"provider": "gemini"}).json()["ok"] is False)

        # --- 생성 ---
        fmts = c.get("/api/generate/formats").json()
        check("GET /api/generate/formats", any(f["key"] == "report" for f in fmts["formats"]))
        plan = c.post("/api/generate/plan", json={"instruction": "결제 이슈 정리해줘", "budget": 900}).json()
        check("POST /api/generate/plan", plan["queries"] and plan["used_tokens"] <= 900, plan["used_tokens"])
        check("plan 에 근거 목록", isinstance(plan["sources"], list))
        check("빈 지시사항 400", c.post("/api/generate/plan", json={"instruction": " "}).status_code == 400)
        gen = c.post("/api/generate", json={"instruction": "결제 이슈 정리해줘"}).json()
        check("POST /api/generate (키 없음 → 프롬프트)", gen["output"] is None and gen["prompt"])
        check("질의 지정 반영", c.post("/api/generate/plan",
              json={"instruction": "x", "queries": ["온보딩"]}).json()["queries"] == ["온보딩"])

        idx = c.get("/")
        check("GET / (웹 UI)", idx.status_code == 200 and "memoinall" in idx.text, idx.status_code)

    print(f"\n통과 {PASS} · 실패 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
