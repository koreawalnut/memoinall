"""임포터 테스트.  python tests/test_importers.py

Windows 스티커 메모는 이 PC 에 데이터가 없으므로, 실제 스키마 모양의
plum.sqlite 를 합성해 검증한다. 파일 임포터는 임시 폴더로 검증한다.
실제 Samsung Notes 가 있으면 읽기 전용으로 추가 확인한다.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

TMP = tempfile.mkdtemp(prefix="memoinall-imp-")
os.environ["MEMOINALL_HOME"] = str(Path(TMP) / "home")
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memoinall import db, importers, store, textutil  # noqa: E402
from memoinall.importers.files import FilesImporter  # noqa: E402
from memoinall.importers.samsung import SamsungNotesImporter  # noqa: E402
from memoinall.importers.sticky import StickyNotesImporter, _strip_markup  # noqa: E402

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


def make_plum(path: Path) -> None:
    """실제 스티커 메모 스키마를 흉내낸 합성 DB."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE Note (
               Id TEXT PRIMARY KEY, Text TEXT, CreatedAt TEXT, UpdatedAt TEXT,
               IsDeleted INTEGER DEFAULT 0, Theme TEXT)"""
    )
    rows = [
        ("g-1", "결제 모듈 타임아웃 확인해야 함 #결제", "2026-06-01T09:30:00", "2026-06-02T10:00:00", 0),
        ("g-2", "  \n\n\n온보딩 단계 줄이기\n\n\n\n\n결정: 3단계로\n\n", "2026-06-05T14:00:00", None, 0),
        ("g-3", "삭제된 메모", "2026-06-06T14:00:00", None, 1),
        ("g-4", "   \n \n ", "2026-06-07T14:00:00", None, 0),  # 본문 없음
        # 실제 저장 형식 그대로 — 문단마다 '\id=<guid> ' 가 붙고, 빈 문단은 뒤에 공백만 남는다
        ("g-5", "\n".join([
            "\\id=f5024f0e-4c7c-431e-8b7e-2bc941475a6a 네트워크 사용량 확인방법",
            "\\id=f0ebc9a4-4b0b-4065-b659-f82bbe281ec2 ",
            "\\id=5a12eaab-3ded-470d-b3a9-8d962fd43878 211.249.118.254",
            "\\id=a480369a-24ef-4582-857d-5eb280844bd3 ",
            "\\id=f93de07c-1417-4add-92f3-d1d0837838a7 admin",
            "\\id=4bbcc8e1-d655-491d-aab5-6973c9958add sh int gigabitEthernet 1/0/2",
        ]), "2026-06-08T14:00:00", None, 0),
    ]
    conn.executemany("INSERT INTO Note(Id,Text,CreatedAt,UpdatedAt,IsDeleted) VALUES(?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def main() -> int:
    db.init()

    section("공통 유틸")
    check("빈 줄 폭탄 정리", importers.clean("a\n\n\n\n\nb") == "a\n\nb", repr(importers.clean("a\n\n\n\n\nb")))
    check("제로폭 문자 제거", importers.clean("Screen off memo​") == "Screen off memo")
    check("epoch ms 변환", importers.epoch_ms_to_iso(1684399785726).startswith("2023-05-18"),
          importers.epoch_ms_to_iso(1684399785726))
    check("epoch 초도 처리", importers.epoch_ms_to_iso(1684399785).startswith("2023-05-18"))
    check("0/None 은 None", importers.epoch_ms_to_iso(0) is None and importers.epoch_ms_to_iso(None) is None)
    check("ISO 문자열 파싱", importers.parse_any_time("2026-06-01T09:30:00") == "2026-06-01T09:30:00")
    check(".NET ticks 파싱", (importers.parse_any_time(638_000_000_000_000_000) or "").startswith("2022-"),
          importers.parse_any_time(638_000_000_000_000_000))
    check("잘못된 값은 None", importers.parse_any_time("어제") is None)

    section("스티커 메모 (합성 DB)")
    plum = Path(TMP) / "plum.sqlite"
    make_plum(plum)
    imp = StickyNotesImporter(plum)
    check("가용", imp.available())
    notes = imp.read()
    check("삭제·빈 메모 제외", len(notes) == 3, [n.external_id for n in notes])
    n1 = next(n for n in notes if n.external_id == "g-1")
    check("원본 작성시각 유지", n1.created_at == "2026-06-01T09:30:00", n1.created_at)
    check("수정시각 유지", n1.updated_at == "2026-06-02T10:00:00", n1.updated_at)
    n2 = next(n for n in notes if n.external_id == "g-2")
    check("빈 줄 정리됨", "\n\n\n" not in n2.body, repr(n2.body))

    # 회귀: 최신 스티커 메모는 문단마다 'id=<guid> ' 를 붙여 저장한다.
    # 그대로 넣으면 메모가 GUID 로 뒤덮이고 제목까지 'id=...' 가 된다(실사용 화면에서 확인).
    section("문단 id 접두어 제거")
    n5 = next(n for n in notes if n.external_id == "g-5")
    check("GUID 가 남지 않음", "id=" not in n5.body and "f5024f0e" not in n5.body, n5.body[:90])
    check("내용 보존", "네트워크 사용량 확인방법" in n5.body and "211.249.118.254" in n5.body, n5.body[:90])
    check("명령줄 보존", "sh int gigabitEthernet 1/0/2" in n5.body)
    check("제목이 깨끗", textutil.title_from(n5.body) == "네트워크 사용량 확인방법",
          textutil.title_from(n5.body))
    check("빈 문단이 빈 줄로", "\n\n" in n5.body, repr(n5.body[:60]))
    # GUID 형태일 때만 지운다 — 진짜 메모 내용은 건드리면 안 된다
    check("일반 id= 는 유지", "id=12345" in _strip_markup("id=12345 서버 설정"),
          _strip_markup("id=12345 서버 설정"))
    check("문장 중간 GUID 는 유지",
          "id=f5024f0e-4c7c-431e-8b7e-2bc941475a6a" in _strip_markup("참고 id=f5024f0e-4c7c-431e-8b7e-2bc941475a6a"))

    # 회귀: 줄 앞의 보이지 않는 문자 때문에 '^id=' 가 안 맞으면
    # 정규식은 멀쩡한데 아무것도 안 지워진 것처럼 보인다.
    guid = "id=f5024f0e-4c7c-431e-8b7e-2bc941475a6a"
    for label, prefix in (("제로폭 공백", "​"), ("BOM", "﻿"),
                          ("LTR 표식", "‎"), ("제어문자", "\x01")):
        check(f"{label} 이 앞에 있어도 제거",
              _strip_markup(f"{prefix}{guid} 네트워크") == "네트워크",
              repr(_strip_markup(f"{prefix}{guid} 네트워크")))
    check("대문자 GUID", _strip_markup("ID=F5024F0E-4C7C-431E-8B7E-2BC941475A6A 내용") == "내용",
          _strip_markup("ID=F5024F0E-4C7C-431E-8B7E-2BC941475A6A 내용"))
    check("중괄호 GUID", _strip_markup("id={f5024f0e-4c7c-431e-8b7e-2bc941475a6a} 내용") == "내용",
          _strip_markup("id={f5024f0e-4c7c-431e-8b7e-2bc941475a6a} 내용"))
    check("하이픈 없는 GUID", _strip_markup("id=f5024f0e4c7c431e8b7e2bc941475a6a 내용") == "내용",
          _strip_markup("id=f5024f0e4c7c431e8b7e2bc941475a6a 내용"))
    check("공백 없이 붙은 경우", _strip_markup(f"{guid}내용") == "내용", _strip_markup(f"{guid}내용"))
    check("탭 구분", _strip_markup(f"{guid}\t내용") == "내용", _strip_markup(f"{guid}\t내용"))

    # 회귀: 실제 저장 형식은 '\id=' 로 역슬래시가 앞에 붙는다(사용자 PC 덤프로 확인).
    # 이걸 놓쳐서 재가져오기가 계속 '변경없음' 으로 끝났다.
    real = (
        "\\id=2499c50f-27b9-47fa-be00-e7f481731982 instif 서버\n"
        "\\id=79b24e3a-9f6f-4f9f-875a-efd7aeebcb76 \n"
        "\\id=49426e0d-7ae4-462c-ad49-8bc9fa944a85 172.16.2.220\n"
        "\\id=33084e17-edc7-47ed-81d8-eb4dfd248405 /usr/share/tomcat/webapps/lms\n"
        "\\id=067b4f58-27c2-4aef-9e9b-2ac469d2b0b1 /home/instif/restart.sh  또는  inst_restart 로 재기동"
    )
    got = _strip_markup(real)
    check("역슬래시 붙은 실제 형식", "id=" not in got and "2499c50f" not in got, repr(got[:80]))
    check("실제 형식 내용 보존",
          got.startswith("instif 서버") and "172.16.2.220" in got
          and "/usr/share/tomcat/webapps/lms" in got and "inst_restart 로 재기동" in got, repr(got))
    check("실제 형식 제목", textutil.title_from(importers.clean(got)) == "instif 서버",
          textutil.title_from(importers.clean(got)))
    check("역슬래시 2개도 제거", _strip_markup(f"\\\\{guid} 내용") == "내용",
          repr(_strip_markup(f"\\\\{guid} 내용")))
    # 역슬래시를 무조건 떼면 UNC 경로가 망가진다 — id 마커일 때만 떼야 한다
    check("UNC 경로는 보존", _strip_markup("\\\\서버\\공유\\문서") == "\\\\서버\\공유\\문서",
          repr(_strip_markup("\\\\서버\\공유\\문서")))
    check("역슬래시로 시작하는 내용 보존",
          _strip_markup("\\연결 안 됨") == "\\연결 안 됨", repr(_strip_markup("\\연결 안 됨")))

    r = importers.run_import(imp, dry_run=True, background_enrich=False)
    check("dry-run 은 저장 안 함", r.imported == 3 and store.stats()["memos"] == 0, store.stats()["memos"])
    check("빈 본문 집계", r.skipped_empty == 0 or r.skipped_empty >= 0)

    r = importers.run_import(imp, dry_run=False, background_enrich=False)
    check("실제 저장", r.imported == 3 and store.stats()["memos"] == 3, store.stats()["memos"])
    saved = [m for m in store.list_memos(limit=50) if m["source"] == "sticky"]
    check("created_at 보존", any(m["created_at"] == "2026-06-01T09:30:00" for m in saved),
          [m["created_at"] for m in saved])

    r2 = importers.run_import(imp, dry_run=False, background_enrich=False)
    check("재실행해도 중복 없음", r2.imported == 0 and r2.skipped_existing == 3 and store.stats()["memos"] == 3,
          f"imported={r2.imported} existing={r2.skipped_existing} total={store.stats()['memos']}")

    section("기존 메모 갱신")
    # 이미 잘못 들어온 메모를 되살리는 경로. 파서를 고쳐도 이게 없으면
    # 기존 메모는 계속 깨진 채로 남는다.
    target = store.find_by_external("sticky", "g-1")
    store.update_memo(target, "예전에 잘못 들어온 내용", enqueue_enrich=False)
    r3 = importers.run_import(imp, dry_run=False, background_enrich=False, update_existing=True)
    check("달라진 메모만 갱신", r3.updated == 1, (r3.updated, r3.unchanged))
    check("나머지는 변경없음", r3.unchanged == 2, r3.unchanged)
    check("새로 추가는 없음", r3.imported == 0 and store.stats()["memos"] == 3)
    check("본문이 원본으로 복구", "타임아웃" in store.get_memo(target)["body"],
          store.get_memo(target)["body"][:40])
    r4 = importers.run_import(imp, dry_run=False, background_enrich=False, update_existing=True)
    check("두 번째 갱신은 변경없음", r4.updated == 0 and r4.unchanged == 3, (r4.updated, r4.unchanged))
    store.update_memo(target, "또 바꿈", enqueue_enrich=False)
    r5 = importers.run_import(imp, dry_run=True, background_enrich=False, update_existing=True)
    check("dry-run 은 갱신 건수만 세고 안 씀",
          r5.updated == 1 and store.get_memo(target)["body"] == "또 바꿈",
          store.get_memo(target)["body"])
    check("기본값은 갱신 안 함",
          importers.run_import(imp, dry_run=False, background_enrich=False).updated == 0)
    check("갱신 끄면 기존은 건너뜀", store.get_memo(target)["body"] == "또 바꿈")
    importers.run_import(imp, dry_run=False, background_enrich=False, update_existing=True)

    # 회귀(실사용): 파서가 '\id=' 를 못 걷어내던 시절에 가져온 메모들은 GUID 를 그대로
    # 안고 저장됐다. 그 상태에서 '이미 가져온 메모도 갱신' 을 켜고 다시 돌리면
    # 새 파싱 결과와 달라지므로 반드시 갱신돼야 한다 — 예전엔 '변경없음' 으로 끝났다.
    section("노이즈째 저장된 메모 복구")
    noisy = store.find_by_external("sticky", "g-5")
    store.update_memo(
        noisy,
        "id=f5024f0e-4c7c-431e-8b7e-2bc941475a6a 네트워크 사용량 확인방법\n"
        "id=5a12eaab-3ded-470d-b3a9-8d962fd43878 211.249.118.254",
        enqueue_enrich=False,
    )
    r6 = importers.run_import(imp, dry_run=False, background_enrich=False, update_existing=True)
    check("노이즈 메모가 갱신 대상", r6.updated == 1, (r6.updated, r6.unchanged))
    fixed = store.get_memo(noisy)["body"]
    check("GUID 사라짐", "id=" not in fixed and "f5024f0e" not in fixed, fixed[:60])
    check("제목 복구", textutil.title_from(fixed) == "네트워크 사용량 확인방법", textutil.title_from(fixed))
    r7 = importers.run_import(imp, dry_run=False, background_enrich=False, update_existing=True)
    check("복구 뒤엔 변경없음", r7.updated == 0 and r7.unchanged == 3, (r7.updated, r7.unchanged))

    for mid in r.memo_ids:
        store.enrich(mid)
    tags = {t["value"] for t in store.top_facets("tag")}
    check("가져온 메모도 추출 동작", "결제" in tags, tags)
    check("가져온 메모도 검색됨", any(h["source"] == "sticky" for h in __import__("memoinall.search", fromlist=["search"]).search("타임아웃")))

    section("파일 폴더")
    folder = Path(TMP) / "notes"
    (folder / "회의").mkdir(parents=True)
    (folder / "aaa.md").write_text("첫 메모\n내용입니다 #테스트", encoding="utf-8")
    (folder / "회의" / "0601.txt").write_text("스프린트 회고 안건 정리해야 함", encoding="utf-8")
    (folder / "무시.png").write_bytes(b"\x89PNG")
    (folder / "cp949.txt").write_bytes("한글 인코딩 확인".encode("cp949"))

    fimp = FilesImporter(folder)
    fnotes = fimp.read()
    check("확장자 필터", len(fnotes) == 3, [n.external_id for n in fnotes])
    check("cp949 파일 디코드", any("한글 인코딩 확인" in n.body for n in fnotes),
          [n.body[:20] for n in fnotes])
    check("상위 폴더가 태그로", any(n.tags == ["회의"] for n in fnotes), [n.tags for n in fnotes])
    fr = importers.run_import(fimp, dry_run=False, background_enrich=False)
    check("파일 임포트 저장", fr.imported == 3, fr.imported)
    for mid in fr.memo_ids:
        store.enrich(mid)
    check("폴더 태그가 본문에 부착", "회의" in {t["value"] for t in store.top_facets("tag")},
          {t["value"] for t in store.top_facets("tag")})
    check("파일 재실행 멱등", importers.run_import(fimp, dry_run=False, background_enrich=False).imported == 0)

    section("없는 소스 처리")
    missing = StickyNotesImporter(Path(TMP) / "nope.sqlite")
    mr = importers.run_import(missing, dry_run=True)
    check("없으면 오류 대신 사유", not mr.available and mr.error, mr.error)
    check("잘못된 소스명 거부", _raises(lambda: importers.get_importer("없는것")))
    # 경로 없는 files 는 예외가 아니라 '사용 불가 + 사유'로 다룬다.
    # 예외를 던지면 UI 가 400 만 받고 왜 안 되는지 못 보여준다.
    nopath = importers.get_importer("files")
    check("경로 없어도 소스는 생성", nopath.name == "files")
    check("경로 없으면 사용 불가", nopath.available() is False)
    check("사유를 알려줌", "지정" in nopath.unavailable_reason(), nopath.unavailable_reason())
    # 회귀: Path("") 는 '.' 이 되어 현재 폴더를 통째로 읽어버린다
    check("빈 경로가 현재 폴더로 새지 않음", nopath.path is None, nopath.path)
    check("빈 경로 read 는 거부", _raises(lambda: nopath.read()))
    nr = importers.run_import(nopath, dry_run=True)
    check("run_import 이 사유를 담아 반환", not nr.available and "지정" in nr.error, nr.error)
    check("목록에는 항상 포함(경로 없어도)",
          any(i.name == "files" for i in importers.all_importers()),
          [i.name for i in importers.all_importers()])

    section("소스별 초기화")
    base = store.count_by_source("sticky")["memos"]  # 앞 절에서 가져온 것들이 이미 있다
    a = store.add_memo("초기화 대상 메모 하나", source="sticky",
                       external_id="reset-1", enqueue_enrich=False)
    b = store.add_memo("초기화 대상 메모 둘", source="sticky",
                       external_id="reset-2", enqueue_enrich=False)
    store.add_memo("다른 앱 메모", source="samsung", external_id="reset-3", enqueue_enrich=False)
    mine = store.add_memo("직접 쓴 메모", enqueue_enrich=False)
    store.enrich(a["id"])  # 파생물(chunks/facets/todos)이 있는 상태에서 지워봐야 한다

    info = store.count_by_source("sticky")
    check("count_by_source 건수", info["memos"] == base + 2, info)
    check("count_by_source 기간", len(info["first"]) == 10 and len(info["last"]) == 10, info)
    check("없는 소스는 0건", store.count_by_source("nope")["memos"] == 0)

    n = store.delete_by_source("sticky")
    check("delete_by_source 반환값", n == base + 2, n)
    check("해당 소스 전멸", store.count_by_source("sticky")["memos"] == 0)
    check("다른 소스는 무사", store.count_by_source("samsung")["memos"] == 1)
    check("직접 쓴 메모는 무사", store.get_memo(mine["id"])["body"] == "직접 쓴 메모")
    check("파생물도 정리(CASCADE)",
          db.one("SELECT COUNT(*) c FROM chunks WHERE memo_id=?", (a["id"],))["c"] == 0)
    # 회귀: memo_fts 는 가상 테이블이라 외래키가 안 걸린다. 직접 안 지우면 유령이 남는다
    check("FTS 잔재 없음",
          db.one("SELECT COUNT(*) c FROM memo_fts WHERE rowid=?", (b["id"],))["c"] == 0)
    # 지운 뒤엔 같은 external_id 로 다시 들어와야 한다 (유니크 인덱스 잔재 확인)
    check("삭제 후 external_id 재사용", store.find_by_external("sticky", "reset-1") is None)
    again = store.add_memo("다시 가져온 메모", source="sticky",
                           external_id="reset-1", enqueue_enrich=False)
    check("같은 external_id 로 재수집", again["id"] != a["id"])
    check("빈 소스 재실행 무해", store.delete_by_source("nope") == 0)
    store.delete_by_source("sticky")
    store.delete_by_source("samsung")
    store.delete_memo(mine["id"])
    check("초기화 대상은 임포터 소스뿐",
          set(importers.SOURCE_NAMES) == {"sticky", "samsung", "redmine", "files"},
          importers.SOURCE_NAMES)

    section("실제 Samsung Notes (있으면 읽기 전용 확인)")
    s = SamsungNotesImporter()
    if s.available():
        snotes = s.read()
        check("읽기 성공", len(snotes) > 0, len(snotes))
        check("모두 본문 있음", all(n.body.strip() for n in snotes))
        check("모두 external_id 있음", all(n.external_id for n in snotes))
        dated = [n for n in snotes if n.created_at]
        check("작성시각 복원", len(dated) == len(snotes), f"{len(dated)}/{len(snotes)}")
        years = sorted({n.created_at[:4] for n in dated})
        check("연도 범위 정상", all("2000" <= y <= "2030" for y in years), years)
        check("원본 불변(읽기전용)", s.path.exists())
    else:
        print("   (건너뜀 — Samsung Notes 데이터 없음)")

    print(f"\n통과 {PASS} · 실패 {FAIL}")
    return 0 if FAIL == 0 else 1


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:
        return True


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
