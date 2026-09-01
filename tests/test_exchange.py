"""메모 주고받기(내보내기/받은 파일 가져오기).  python tests/test_exchange.py

핵심은 두 가지다.
  - 남에게 보내는 파일이므로 '고른 것만' 나가야 한다.
  - 돌고 돌아 자기 메모를 다시 받아도 두 벌이 되면 안 된다.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

TMP = tempfile.mkdtemp(prefix="memoinall-exch-")
os.environ["MEMOINALL_HOME"] = str(Path(TMP) / "home")
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memoinall import db, exchange, importers, store  # noqa: E402
from memoinall.importers.shared import SharedFileImporter  # noqa: E402

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


def main() -> int:
    db.init()

    section("내보낼 메모 고르기")
    a = store.add_memo("결제 모듈 타임아웃 재현됨 #결제", created_at="2026-06-01T09:00:00",
                       enqueue_enrich=False)
    b = store.add_memo("온보딩 3단계로 축소 결정 #기획", created_at="2026-07-10T09:00:00",
                       enqueue_enrich=False)
    c = store.add_memo("스티커에서 온 메모 #결제", source="sticky", external_id="s-1",
                       created_at="2026-08-20T09:00:00", enqueue_enrich=False)
    for m in (a, b, c):
        store.enrich(m["id"])

    check("조건 없으면 전체", len(exchange.select()) == 3, len(exchange.select()))
    check("시간순 정렬", [m["id"] for m in exchange.select()] == [a["id"], b["id"], c["id"]],
          [m["id"] for m in exchange.select()])
    check("태그로 고르기", {m["id"] for m in exchange.select(tag="결제")} == {a["id"], c["id"]},
          [m["id"] for m in exchange.select(tag="결제")])
    check("소스로 고르기", [m["id"] for m in exchange.select(source="sticky")] == [c["id"]])
    check("직접 쓴 것만", {m["id"] for m in exchange.select(source="web")} == {a["id"], b["id"]})
    check("기간으로 고르기",
          [m["id"] for m in exchange.select(since="2026-07-01", until="2026-07-31")] == [b["id"]])
    check("id 로 고르기", [m["id"] for m in exchange.select(ids=[c["id"], a["id"]])] == [a["id"], c["id"]])
    check("없는 id 는 무시", exchange.select(ids=[a["id"], 99999])[0]["id"] == a["id"])
    check("조건이 겹치면 AND",
          exchange.select(tag="결제", source="sticky")[0]["id"] == c["id"])
    check("limit 적용", len(exchange.select(limit=2)) == 2)
    check("맞는 게 없으면 빈 목록", exchange.select(tag="없는태그") == [])

    section("꾸러미 형식")
    pack = exchange.build(exchange.select(), note="자료 공유합니다")
    check("형식 표시", pack["format"] == "memoinall/memos" and pack["version"] == 1, pack.get("format"))
    check("건수", pack["count"] == 3 and len(pack["memos"]) == 3)
    check("전할 말 포함", pack["note"] == "자료 공유합니다")
    first = pack["memos"][0]
    check("본문·작성시각 포함", first["body"].startswith("결제 모듈") and first["created_at"] == "2026-06-01T09:00:00")
    check("태그 포함", "결제" in first["tags"], first["tags"])
    check("uid 부여", len(first["uid"]) == 16, first["uid"])
    # 남에게 가는 파일이다. 임베딩·API 키·내부 id 가 섞여 나가면 안 된다.
    text = exchange.dumps(pack)
    check("임베딩 미포함", "embedding" not in text and "chunks" not in text)
    check("내부 id 미포함", '"id"' not in text, text[:200])
    check("한글이 그대로", "결제 모듈" in text)
    check("사람이 읽는 JSON", json.loads(text)["count"] == 3)

    section("파일로 저장")
    out = Path(TMP) / "out"
    saved = exchange.save(pack, out / "pack.json")
    check("파일 생성", saved.exists() and saved.name == "pack.json")
    check("UTF-8 로 저장", "결제 모듈" in saved.read_text(encoding="utf-8"))
    folder_save = exchange.save(pack, out)
    check("폴더를 주면 기본 이름", folder_save.parent == out and folder_save.name.endswith(".json"),
          folder_save.name)
    check("기본 이름에 건수", "3건" in folder_save.name, folder_save.name)
    ext = exchange.save(pack, out / "확장자없음")
    check("확장자 보정", ext.suffix == ".json", ext.name)

    section("받은 파일 읽기")
    imp = SharedFileImporter(saved)
    check("가용", imp.available())
    notes = imp.read()
    check("건수 일치", len(notes) == 3)
    check("본문 복원", notes[0].body.startswith("결제 모듈"))
    check("작성시각 복원", notes[0].created_at == "2026-06-01T09:00:00", notes[0].created_at)
    check("uid 를 external_id 로", notes[0].external_id == first["uid"])
    info = imp.describe()
    check("겉면 확인", info["count"] == 3 and info["note"] == "자료 공유합니다", info)

    section("잘못된 파일")
    check("경로 미지정", not SharedFileImporter().available())
    check("미지정 사유", "지정" in SharedFileImporter().unavailable_reason())
    bad = Path(TMP) / "bad.json"
    bad.write_text("{이건 JSON 이 아님", encoding="utf-8")
    check("깨진 JSON", raises(lambda: SharedFileImporter(bad).read(), "JSON"))
    other = Path(TMP) / "other.json"
    other.write_text(json.dumps({"format": "다른툴/노트", "memos": []}), encoding="utf-8")
    check("다른 프로그램 파일", raises(lambda: SharedFileImporter(other).read(), "다른 프로그램"))
    nomemos = Path(TMP) / "nomemos.json"
    nomemos.write_text(json.dumps({"format": exchange.FORMAT, "hello": 1}), encoding="utf-8")
    check("memos 없음", raises(lambda: SharedFileImporter(nomemos).read(), "memos"))
    future = Path(TMP) / "future.json"
    future.write_text(json.dumps({"format": exchange.FORMAT, "version": 99, "memos": []}),
                      encoding="utf-8")
    check("더 새로운 형식", raises(lambda: SharedFileImporter(future).read(), "최신"))
    check("없는 파일", raises(lambda: SharedFileImporter(Path(TMP) / "없다.json").read(), "찾을 수 없"))
    check("폴더 지정", raises(lambda: SharedFileImporter(out).read(), "파일을 지정"))
    # 손으로 고친 파일도 최대한 받아준다 — uid 가 없으면 내용으로 다시 만든다
    handmade = Path(TMP) / "handmade.json"
    handmade.write_text(json.dumps({
        "memos": [{"body": "손으로 적은 메모입니다", "created_at": "2026-05-01T00:00:00"},
                  {"body": "   "}, "쓰레기", {"body": "두 번째 손메모입니다"}]
    }, ensure_ascii=False), encoding="utf-8")
    hand = SharedFileImporter(handmade).read()
    check("uid 없어도 읽힘", len(hand) == 2 and len(hand[0].external_id) == 16, len(hand))
    check("빈 본문·이상한 항목 제외", all(n.body.strip() for n in hand))

    section("받아 넣기")
    home2 = Path(TMP) / "home2"
    _switch_home(home2)
    mine = store.add_memo("받는 쪽이 원래 갖고 있던 메모", enqueue_enrich=False)
    r = importers.run_import(SharedFileImporter(saved), dry_run=True, background_enrich=False)
    check("미리보기는 저장 안 함", r.imported == 3 and store.stats()["memos"] == 1, store.stats()["memos"])
    r = importers.run_import(SharedFileImporter(saved), dry_run=False, background_enrich=False)
    check("실제 저장", r.imported == 3 and store.stats()["memos"] == 4, store.stats()["memos"])
    got = [m for m in store.list_memos(limit=50) if m["source"] == "shared"]
    check("소스가 shared", len(got) == 3, len(got))
    check("작성시각 유지", any(m["created_at"] == "2026-06-01T09:00:00" for m in got),
          [m["created_at"] for m in got])
    check("원래 메모 무사", store.get_memo(mine["id"])["body"].startswith("받는 쪽이"))

    r2 = importers.run_import(SharedFileImporter(saved), dry_run=False, background_enrich=False)
    check("같은 파일 재실행 멱등", r2.imported == 0 and store.stats()["memos"] == 4,
          (r2.imported, store.stats()["memos"]))

    section("돌고 돌아온 내 메모")
    # 받은 쪽이 다시 내보내 원 주인에게 돌려주는 경우. 소스가 달라(shared vs web)
    # external_id 로는 못 걸러서, 내용으로 걸러야 두 벌이 안 된다.
    back = exchange.build(exchange.select())
    back_file = exchange.save(back, Path(TMP) / "back.json")
    _switch_home(Path(TMP) / "home")  # 원 주인에게로
    before = store.stats()["memos"]
    r3 = importers.run_import(SharedFileImporter(back_file), dry_run=False, background_enrich=False)
    check("내 메모는 중복으로 안 들어옴", r3.skipped_duplicate == 3, r3.skipped_duplicate)
    check("상대가 새로 적은 것만 들어옴", r3.imported == 1, r3.imported)
    check("총 건수", store.stats()["memos"] == before + 1, store.stats()["memos"])
    check("원본 소스 보존", store.count_by_source("web")["memos"] == 2,
          store.count_by_source("web")["memos"])

    r4 = importers.run_import(SharedFileImporter(back_file), dry_run=False, background_enrich=False)
    check("한 번 더 돌려도 그대로", r4.imported == 0 and store.stats()["memos"] == before + 1)

    section("내용 기준 식별자")
    uid = exchange.content_uid
    check("같은 내용 같은 uid", uid("2026-01-01", "안녕하세요") == uid("2026-01-01", "안녕하세요"))
    check("내용 다르면 다른 uid", uid("2026-01-01", "안녕") != uid("2026-01-01", "안녕하세요"))
    check("시각 다르면 다른 uid", uid("2026-01-01", "안녕") != uid("2026-01-02", "안녕"))
    # clean() 을 거치며 달라지는 공백은 같은 것으로 봐야 한다 — 안 그러면 왕복 때 중복된다
    check("빈 줄 차이는 무시", uid("2026-01-01", "가\n\n\n\n나") == uid("2026-01-01", "가\n\n나"),
          (uid("2026-01-01", "가\n\n\n\n나"), uid("2026-01-01", "가\n\n나")))
    check("앞뒤 공백 무시", uid("2026-01-01", "  안녕  ") == uid("2026-01-01", "안녕"))
    check("줄끝 공백 무시", uid("2026-01-01", "안녕   \n반가워") == uid("2026-01-01", "안녕\n반가워"))

    section("주고받기도 초기화 대상")
    _switch_home(Path(TMP) / "home2")
    check("shared 도 초기화 가능", "shared" in importers.SOURCE_NAMES)
    n = store.delete_by_source("shared")
    check("받은 메모만 삭제", n == 3 and store.count_by_source("shared")["memos"] == 0, n)
    check("내 메모는 남음", store.stats()["memos"] >= 1)

    print(f"\n통과 {PASS} · 실패 {FAIL}")
    return 0 if FAIL == 0 else 1


def _switch_home(path: Path) -> None:
    """다른 사용자의 PC 를 흉내낸다 — DB 를 통째로 갈아끼운다."""
    import importlib

    from memoinall import config

    path.mkdir(parents=True, exist_ok=True)
    os.environ["MEMOINALL_HOME"] = str(path)
    importlib.reload(config)
    db.config = config
    store.config = config
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn
    db.init()
    store.invalidate_cache()


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
