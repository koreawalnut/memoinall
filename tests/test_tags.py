"""정해 두고 쓰는 태그.  python tests/test_tags.py

태그의 원본은 메모 본문이고 tag_defs 는 목록일 뿐이다 — 그 관계가 깨지지
않는지가 핵심이다. 특히 이름을 바꿀 때 본문과 facets 가 같이 따라와야 한다.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

TMP = tempfile.mkdtemp(prefix="memoinall-tags-")
os.environ["MEMOINALL_HOME"] = str(Path(TMP) / "home")
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memoinall import db, exchange, importers, search, store, tags  # noqa: E402

PASS = FAIL = 0


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


def raises(fn, want=""):
    try:
        fn()
        return False
    except Exception as exc:
        return want in str(exc) if want else True


def add(body, **kw):
    m = store.add_memo(body, enqueue_enrich=False, **kw)
    store.enrich(m["id"])
    return m


def main() -> int:
    db.init()

    section("이름 규칙")
    check("# 을 떼어냄", tags.normalize("#결제") == "결제")
    check("앞뒤 공백 정리", tags.normalize("  결제 ") == "결제")
    check("가운데 공백은 _", tags.normalize("결제 시스템") == "결제_시스템")
    check("영문·숫자·기호 허용", tags.normalize("v1/api-2") == "v1/api-2")
    check("빈 이름 거부", raises(lambda: tags.normalize("  "), "입력"))
    check("# 만 있어도 거부", raises(lambda: tags.normalize("#"), "입력"))
    check("너무 길면 거부", raises(lambda: tags.normalize("가" * 41), "40자"))
    check("못 쓰는 문자 안내", raises(lambda: tags.normalize("결제!"), "!"))
    check("본문 추출 규칙과 같음",
          all(tags.NAME_RE.match(n) for n in ["결제", "v1/api-2", "a_b"]))

    section("만들고 고치기")
    t = tags.add("결제", color="blue")
    check("만들기", t["name"] == "결제" and t["color"] == "blue", t)
    check("중복 거부", raises(lambda: tags.add("결제"), "이미 있는"))
    check("# 붙여도 같은 태그로 봄", raises(lambda: tags.add("#결제"), "이미 있는"))
    auto = tags.add("기획")
    check("색을 안 고르면 자동", auto["color"] in tags.COLORS, auto["color"])
    check("같은 이름은 늘 같은 색", tags.COLORS[sum(ord(c) for c in "기획") % len(tags.COLORS)] == auto["color"])
    check("잘못된 색은 자동으로", tags.add("임시", color="무지개")["color"] in tags.COLORS)
    check("설명 저장", tags.update("결제", note="결제 도메인 전반")["note"] == "결제 도메인 전반")
    check("색 바꾸기", tags.update("결제", color="red")["color"] == "red")
    check("없는 태그 수정 거부", raises(lambda: tags.update("없음", color="red"), "없는 태그"))
    check("목록 순서", [x["name"] for x in tags.all_tags()] == ["결제", "기획", "임시"],
          [x["name"] for x in tags.all_tags()])
    tags.reorder(["임시", "결제"])
    check("순서 바꾸기", [x["name"] for x in tags.all_tags()] == ["임시", "결제", "기획"],
          [x["name"] for x in tags.all_tags()])
    check("모르는 이름은 무시", [x["name"] for x in tags.reorder(["없음", "기획"])][0] == "기획")

    section("본문에 붙이기")
    check("없으면 붙임", tags.append_to_body("PG사 지연", ["결제"]) == "PG사 지연\n#결제")
    check("이미 있으면 안 붙임", tags.append_to_body("PG사 지연 #결제", ["결제"]) == "PG사 지연 #결제")
    check("대소문자 무시", tags.append_to_body("hi #Pay", ["pay"]) == "hi #Pay")
    check("여러 개", tags.append_to_body("본문", ["결제", "기획"]) == "본문\n#결제 #기획")
    check("중복 지정은 한 번만", tags.append_to_body("본문", ["결제", "결제"]) == "본문\n#결제")
    check("빈 본문", tags.append_to_body("", ["결제"]) == "#결제")
    check("못 쓰는 이름은 건너뜀", tags.append_to_body("본문", ["결제!", "기획"]) == "본문\n#기획")

    section("사용 건수")
    m1 = add("PG사 지연 확인 필요 #결제")
    m2 = add("온보딩 축소 결정 #결제 #기획")
    add("관계없는 메모")
    counts = {x["name"]: x["count"] for x in tags.all_tags()}
    check("건수 집계", counts["결제"] == 2 and counts["기획"] == 1, counts)
    check("안 쓴 태그는 0", counts["임시"] == 0)

    section("조회 조건")
    check("태그 하나", len(store.list_memos(tags=["결제"])) == 2)
    check("여러 개는 AND", [m["id"] for m in store.list_memos(tags=["결제", "기획"])] == [m2["id"]],
          [m["id"] for m in store.list_memos(tags=["결제", "기획"])])
    check("겹치는 게 없으면 0", store.list_memos(tags=["기획", "임시"]) == [])
    check("# 을 붙여 넘겨도 동작", len(store.list_memos(tags=["#결제"])) == 2)
    check("tag 와 tags 를 같이", len(store.list_memos(tag="결제", tags=["기획"])) == 1)
    check("검색에도 적용", [h["id"] for h in search.search("", tags=["결제", "기획"])] == [m2["id"]])
    check("검색어 + 태그", all("결제" in store.get_memo(h["id"])["body"]
                             for h in search.search("지연", tags=["결제"])))

    section("내보내기 조건")
    check("태그로 고르기", len(exchange.select(tags=["결제"])) == 2)
    check("여러 태그는 AND", [m["id"] for m in exchange.select(tags=["결제", "기획"])] == [m2["id"]])
    pack = exchange.build(exchange.select(tags=["결제"]))
    check("꾸러미에 태그 포함", all("결제" in m["tags"] for m in pack["memos"]), pack["memos"][0]["tags"])
    check("본문에도 남아 있음", all("#결제" in m["body"] for m in pack["memos"]))

    section("가져올 때 태그 붙이기")
    folder = Path(TMP) / "notes"
    folder.mkdir()
    (folder / "a.md").write_text("가져온 문서 하나입니다. 내용이 좀 있습니다.", encoding="utf-8")
    from memoinall.importers.files import FilesImporter

    r = importers.run_import(FilesImporter(folder), dry_run=False, background_enrich=False,
                             extra_tags=["결제"])
    check("가져오기 성공", r.imported == 1, r.imported)
    got = [m for m in store.list_memos(limit=50) if m["source"] == "files"][0]
    store.enrich(got["id"])
    check("태그가 붙음", "#결제" in store.get_memo(got["id"])["body"],
          store.get_memo(got["id"])["body"])
    check("태그로 찾아짐", got["id"] in [m["id"] for m in store.list_memos(tags=["결제"])])

    section("이름 바꾸기")
    edge = add("결제팀 회의 #결제팀 #결제")
    out = tags.rename("결제", "결제시스템")
    check("이름 바뀜", out["name"] == "결제시스템")
    check("본문도 바뀜", "#결제시스템" in store.get_memo(m1["id"])["body"],
          store.get_memo(m1["id"])["body"])
    # 회귀: facets 는 백그라운드 재추출에 맡기면 그 사이 조회가 옛 태그로 걸린다.
    # CLI 처럼 워커가 없는 환경에서는 영영 안 고쳐진다.
    check("조회가 즉시 새 이름으로", len(store.list_memos(tags=["결제시스템"])) == out["count"],
          (len(store.list_memos(tags=["결제시스템"])), out["count"]))
    check("옛 이름으로는 안 나옴", store.list_memos(tags=["결제"]) == [])
    # 회귀: `#결제` 를 갈아끼우다 `#결제팀` 까지 건드리면 남의 태그를 망가뜨린다
    body = store.get_memo(edge["id"])["body"]
    check("이어붙은 다른 태그는 무사", "#결제팀" in body, body)
    check("그 메모도 새 이름을 가짐", "#결제시스템" in body, body)
    check("있는 이름으로는 못 바꿈", raises(lambda: tags.rename("기획", "결제시스템"), "이미 있는"))
    check("없는 태그는 못 바꿈", raises(lambda: tags.rename("없음", "새것"), "없는 태그"))
    check("같은 이름이면 그대로", tags.rename("기획", "기획")["name"] == "기획")

    section("목록에서 빼기")
    before = len(store.list_memos(tags=["기획"]))
    tags.remove("기획")
    check("목록에서만 빠짐", "기획" not in tags.names())
    check("메모의 태그는 그대로", len(store.list_memos(tags=["기획"])) == before, before)
    check("없는 태그는 못 뺌", raises(lambda: tags.remove("없음"), "없는 태그"))

    tags.add("기획")
    r = tags.remove("기획", purge=True)
    check("purge 는 본문도 고침", r["memos_changed"] == before, (r["memos_changed"], before))
    check("purge 뒤엔 조회 안 됨", store.list_memos(tags=["기획"]) == [])
    check("다른 태그는 살아 있음", "#결제시스템" in store.get_memo(m2["id"])["body"],
          store.get_memo(m2["id"])["body"])
    check("본문 내용은 남음", "온보딩" in store.get_memo(m2["id"])["body"])

    section("이미 쓰던 태그 데려오기")
    rest = [x["value"] for x in tags.unregistered()]
    check("미등록 태그를 알려줌", "결제팀" in rest, rest)
    check("등록된 것은 빠짐", "결제시스템" not in rest, rest)
    added = tags.adopt(["결제팀", "결제시스템", "못쓰는!이름"])
    check("새것만 들어옴", [t["name"] for t in added] == ["결제팀"], [t["name"] for t in added])
    check("데려온 뒤엔 목록에", "결제팀" in tags.names())
    check("건수도 따라옴", tags.get("결제팀")["count"] == 1, tags.get("결제팀")["count"])

    section("태그만 있는 메모는 안 건드림")
    only = add("#결제시스템")
    tags.remove("결제시스템", purge=True)
    check("본문이 통째로 사라지지 않음", store.get_memo(only["id"])["body"].strip() != "",
          repr(store.get_memo(only["id"])["body"]))

    print(f"\n통과 {PASS} · 실패 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
