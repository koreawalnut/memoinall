"""Redmine 임포터 테스트.  python tests/test_redmine.py

진짜 Redmine 없이도 검증할 수 있게, 실제 응답 모양을 흉내낸 HTTP 서버를 띄워
그걸 상대로 돌린다. 페이지네이션·오류코드·본문 조립까지 실제 소켓으로 확인한다.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

warnings.filterwarnings("ignore")

TMP = tempfile.mkdtemp(prefix="memoinall-rm-")
os.environ["MEMOINALL_HOME"] = TMP
os.environ["MEMOINALL_DISABLE_ST"] = "1"
os.environ.pop("REDMINE_URL", None)
os.environ.pop("REDMINE_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memoinall import db, importers, settings, store  # noqa: E402
from memoinall.importers.redmine import RedmineError, RedmineImporter, _tag  # noqa: E402

PASS = FAIL = 0
GOOD_KEY = "secret-key-123"


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {extra}")


def section(name):
    print(f"\n[{name}]")


# --------------------------------------------------------------------------- 가짜 서버

ISSUES = [
    {"id": 100 + i, "subject": f"이슈 제목 {i}",
     "description": f"이슈 본문 {i} 입니다. 상세 설명이 여기 들어갑니다.",
     "project": {"name": "포털 개편"}, "tracker": {"name": "결함"},
     "status": {"name": "진행중"}, "author": {"name": "홍길동"},
     "assigned_to": {"name": "김철수"},
     "created_on": "2026-03-04T05:06:07Z", "updated_on": "2026-04-01T00:00:00Z"}
    for i in range(250)
]


class Handler(BaseHTTPRequestHandler):
    fail_with = None  # 테스트가 상태코드를 강제할 때

    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if Handler.fail_with:
            self._send(Handler.fail_with, {"errors": ["강제 오류"]})
            return
        if self.headers.get("X-Redmine-API-Key") != GOOD_KEY:
            self._send(401, {"errors": ["Unauthorized"]})
            return

        u = urlparse(self.path)
        q = parse_qs(u.query)
        offset = int(q.get("offset", [0])[0])
        limit = int(q.get("limit", [25])[0])
        path = u.path

        if path == "/users/current.json":
            return self._send(200, {"user": {"login": "tester", "firstname": "테", "lastname": "스터"}})
        if path == "/projects.json":
            projects = [{"id": 1, "identifier": "portal", "name": "포털 개편"},
                        {"id": 2, "identifier": "infra", "name": "인프라"}]
            return self._send(200, {"projects": projects[offset:offset + limit],
                                    "total_count": len(projects), "offset": offset, "limit": limit})
        if path == "/issues.json":
            items = ISSUES
            if q.get("project_id") == ["infra"]:
                items = []
            page = items[offset:offset + limit]
            return self._send(200, {"issues": page, "total_count": len(items),
                                    "offset": offset, "limit": limit})
        if path.startswith("/issues/") and path.endswith(".json"):
            iid = int(path.split("/")[2].split(".")[0])
            return self._send(200, {"issue": {**ISSUES[iid - 100], "journals": [
                {"user": {"name": "김철수"}, "created_on": "2026-04-02T00:00:00Z", "notes": "확인했습니다."},
                {"user": {"name": "홍길동"}, "created_on": "2026-04-03T00:00:00Z", "notes": ""},
            ]}})
        if path.endswith("/wiki/index.json"):
            proj = path.split("/")[2]
            if proj == "infra":
                return self._send(404, {"errors": ["not found"]})
            return self._send(200, {"wiki_pages": [{"title": "개발규칙"}, {"title": "빈페이지"}]})
        if "/wiki/" in path:
            title = path.split("/wiki/")[1].replace(".json", "")
            from urllib.parse import unquote

            title = unquote(title)
            text = "" if title == "빈페이지" else "코딩 규칙은 다음과 같습니다. 들여쓰기는 4칸."
            return self._send(200, {"wiki_page": {"title": title, "text": text,
                                                  "created_on": "2026-01-02T00:00:00Z",
                                                  "updated_on": "2026-02-02T00:00:00Z"}})
        if path.endswith("/documents.json"):
            proj = path.split("/")[2]
            if proj != "portal":
                return self._send(403, {"errors": ["forbidden"]})
            docs = [{"id": 7, "title": "설계 문서", "description": "아키텍처 설명입니다.",
                     "category": {"name": "설계"}, "created_on": "2026-02-01T00:00:00Z"}]
            return self._send(200, {"documents": docs, "total_count": 1, "offset": 0, "limit": limit})
        if path.endswith("news.json"):
            news = [{"id": 3, "title": "정기 점검 안내", "description": "토요일 02시에 점검합니다.",
                     "project": {"name": "포털 개편"}, "created_on": "2026-03-01T00:00:00Z"}]
            return self._send(200, {"news": news, "total_count": 1, "offset": 0, "limit": limit})
        self._send(404, {"errors": ["no route"]})


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def main() -> int:
    db.init()
    srv, base = start_server()
    print(f"가짜 Redmine: {base}")

    def imp(**kw):
        kw.setdefault("url", base)
        kw.setdefault("api_key", GOOD_KEY)
        return RedmineImporter(**kw)

    section("가용성")
    check("주소·키 없으면 불가", RedmineImporter().available() is False)
    check("주소 없을 때 안내", "주소" in RedmineImporter(api_key="k").unavailable_reason())
    check("키 없을 때 안내", "키" in RedmineImporter(url=base).unavailable_reason())
    check("둘 다 있으면 가용", imp().available() is True)

    section("연결 테스트")
    t = imp().test_connection()
    check("성공 보고", t["ok"] and "tester" in t["message"], t)
    bad = imp(api_key="wrong-key-999").test_connection()
    check("키 틀리면 실패", not bad["ok"], bad)
    check("키 오류 안내가 구체적", "API 키" in bad["message"], bad["message"])
    # 회귀: 한글 키를 넣으면 urllib 이 latin-1 인코딩에서 죽어 원인을 알 수 없었다
    kr = imp(api_key="한글키").test_connection()
    check("한글 키도 친절히 거부", not kr["ok"] and "한글" in kr["message"], kr["message"])
    gone = RedmineImporter(url="http://127.0.0.1:9", api_key="k").test_connection()
    check("서버 없으면 연결 안내", not gone["ok"] and "연결할 수 없습니다" in gone["message"], gone["message"])

    section("오류 코드별 안내")
    for code, word in ((403, "권한"), (404, "주소"), (422, "거부")):
        Handler.fail_with = code
        r = imp().test_connection()
        check(f"HTTP {code} → '{word}'", word in r["message"], r["message"])
    Handler.fail_with = None

    section("이슈 가져오기")
    notes = imp(kinds=["issues"], limit=10).read()
    check("상한 준수", len(notes) == 10, len(notes))
    n = notes[0]
    check("제목에 이슈번호", n.body.startswith("[#100]"), n.body[:20])
    check("본문 포함", "이슈 본문 0" in n.body)
    check("메타 줄(트래커·상태·담당)", "결함" in n.body and "진행중" in n.body and "김철수" in n.body,
          n.body.splitlines()[1] if len(n.body.splitlines()) > 1 else "")
    check("external_id 형식", n.external_id == "issue:100", n.external_id)
    check("작성시각 변환", n.created_at == "2026-03-04T05:06:07", n.created_at)
    check("수정시각 변환", n.updated_at == "2026-04-01T00:00:00", n.updated_at)
    check("프로젝트/트래커 태그", "포털개편" in n.tags or "포털_개편" in n.tags, n.tags)

    section("페이지네이션")
    many = imp(kinds=["issues"], limit=250).read()
    check("100건 넘겨 받음(페이지 3장)", len(many) == 250, len(many))
    check("중복 없음", len({x.external_id for x in many}) == 250)
    check("total_count 넘어가면 멈춤", len(imp(kinds=["issues"], limit=400).read()) == 250)

    section("코멘트 포함 옵션")
    plain = imp(kinds=["issues"], limit=1).read()[0]
    check("기본은 코멘트 없음", "확인했습니다" not in plain.body)
    withnotes = imp(kinds=["issues"], limit=1, include_notes=True).read()[0]
    check("옵션 켜면 코멘트 포함", "확인했습니다" in withnotes.body, withnotes.body[-60:])
    check("빈 코멘트는 제외", withnotes.body.count("—") == 1, withnotes.body.count("—"))

    section("위키")
    wiki = imp(kinds=["wiki"], projects="portal", limit=10).read()
    check("본문 있는 페이지만", len(wiki) == 1, [w.external_id for w in wiki])
    check("제목 + 본문", wiki[0].body.startswith("개발규칙") and "들여쓰기" in wiki[0].body)
    check("external_id 에 프로젝트", wiki[0].external_id == "wiki:portal/개발규칙", wiki[0].external_id)
    check("위키 태그", "위키" in wiki[0].tags, wiki[0].tags)
    check("위키 없는 프로젝트는 건너뜀", imp(kinds=["wiki"], projects="infra").read() == [])

    section("문서 · 공지")
    docs = imp(kinds=["documents"], projects="portal", limit=10).read()
    check("문서 1건", len(docs) == 1 and docs[0].external_id == "document:7", [d.external_id for d in docs])
    check("제목+설명 결합", "설계 문서" in docs[0].body and "아키텍처" in docs[0].body)
    check("카테고리 태그", "설계" in docs[0].tags, docs[0].tags)
    check("권한 없는 프로젝트는 건너뜀", imp(kinds=["documents"], projects="infra").read() == [])
    news = imp(kinds=["news"], limit=5).read()
    check("공지 가져옴", len(news) == 1 and "정기 점검" in news[0].body)

    section("종류 조합 · 상한 배분")
    # 회귀: 전체 상한을 순서대로 소진시키면 이슈가 다 먹고 위키·문서가 0건이 됐다.
    mixed = imp(kinds=["issues", "wiki", "documents"], projects="portal", limit=5).read()
    kinds_got = {n.origin for n in mixed}
    check("상한은 종류별로 적용", kinds_got == {"issue", "wiki", "document"}, kinds_got)
    check("이슈는 종류별 상한까지", sum(1 for n in mixed if n.origin == "issue") == 5,
          sum(1 for n in mixed if n.origin == "issue"))
    check("문서도 함께 옴", any(n.origin == "document" for n in mixed))
    check("잘못된 종류는 무시", imp(kinds=["없는것"], limit=3).read() != [], "기본값으로 대체돼야 함")

    section("제목 추출")
    # 회귀: title_from 이 문자 단위 lstrip 이라 "[#100] 이슈" 가 "100] 이슈" 로 잘렸다
    from memoinall.textutil import title_from

    check("이슈 번호 유지", title_from("[#100] 이슈 제목") == "[#100] 이슈 제목",
          title_from("[#100] 이슈 제목"))
    check("체크박스는 제거", title_from("[ ] 할일 항목") == "할일 항목", title_from("[ ] 할일 항목"))
    check("목록 표시는 제거", title_from("- 항목") == "항목" and title_from("* 항목") == "항목")
    check("마크다운 제목 제거", title_from("## 제목") == "제목", title_from("## 제목"))
    check("해시태그는 유지", title_from("#결제 관련") == "#결제 관련", title_from("#결제 관련"))

    section("프로젝트 필터")
    check("빈 결과 프로젝트", imp(kinds=["issues"], projects="infra", limit=10).read() == [])
    check("프로젝트 목록 조회", [p["id"] for p in imp().list_projects()] == ["portal", "infra"])

    section("run_import 연동")
    result = importers.run_import(imp(kinds=["issues"], limit=3), dry_run=True)
    check("미리보기 건수", result.imported == 3 and result.found == 3, (result.found, result.imported))
    check("샘플 생성", len(result.samples) == 3, result.samples)
    result = importers.run_import(imp(kinds=["issues"], limit=3), dry_run=False, background_enrich=False)
    check("실제 저장", result.imported == 3 and store.stats()["memos"] == 3)
    again = importers.run_import(imp(kinds=["issues"], limit=3), dry_run=False, background_enrich=False)
    check("재실행 멱등", again.imported == 0 and again.skipped_existing == 3,
          (again.imported, again.skipped_existing))

    saved = store.get_memo(result.memo_ids[0])
    check("원본 작성시각 유지", saved["created_at"] == "2026-03-04T05:06:07", saved["created_at"])
    check("source 기록", saved["source"] == "redmine", saved["source"])
    store.enrich(saved["id"])
    tags = {t["value"] for t in store.top_facets("tag")}
    check("태그가 메모에 반영", tags & {"포털개편", "결함"}, tags)

    section("오류가 run_import 를 뚫지 않음")
    broken = importers.run_import(imp(api_key="틀린키", kinds=["issues"]), dry_run=True)
    check("예외 대신 error 필드", broken.error and broken.imported == 0, broken.error)
    check("오류에 클래스명 안 붙음", not broken.error.startswith("RedmineError"), broken.error)

    section("설정 연동")
    settings.set_many({"import.redmine.url": base, "import.redmine.api_key": GOOD_KEY,
                       "import.redmine.kinds": "issues", "import.redmine.limit": "2"})
    built = importers.build_redmine()
    check("설정에서 주소·키", built.url == base and built.api_key == GOOD_KEY)
    check("설정에서 종류·상한", built.kinds == ["issues"] and built.limit == 2, (built.kinds, built.limit))
    override = importers.build_redmine(redmine_limit=7, redmine_kinds="wiki,news")
    check("인자가 설정을 덮어씀", override.limit == 7 and override.kinds == ["wiki", "news"],
          (override.limit, override.kinds))
    check("all_importers 에 포함", any(i.name == "redmine" for i in importers.all_importers()))
    check("get_importer 로 획득", importers.get_importer("redmine").name == "redmine")
    view = settings.public_view()
    check("API 키는 마스킹", GOOD_KEY not in str(view), "누출!")

    section("태그 정규화")
    check("공백 → 밑줄", _tag("포털 개편") in ("포털개편", "포털_개편"), _tag("포털 개편"))
    check("특수문자 제거", "/" not in _tag("a/b") and "!" not in _tag("x!"), _tag("a/b"))
    check("빈 값 안전", _tag("") == "")

    srv.shutdown()
    print(f"\n통과 {PASS} · 실패 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
