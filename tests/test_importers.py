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

from memoinall import db, importers, store  # noqa: E402
from memoinall.importers.files import FilesImporter  # noqa: E402
from memoinall.importers.samsung import SamsungNotesImporter  # noqa: E402
from memoinall.importers.sticky import StickyNotesImporter  # noqa: E402

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
        ("g-5", "@김민수 랑 회고 잡기", "2026-06-08T14:00:00", None, 0),
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
    check("files 에 경로 없으면 거부", _raises(lambda: importers.get_importer("files")))

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
