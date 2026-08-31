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
        check("일괄은 전 소스", {x["source"] for x in every["results"]} == {"sticky", "samsung", "redmine", "files"},
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
