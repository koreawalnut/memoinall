"""문서 파일 추출·미리보기·저장·검색 회귀 테스트. python tests/test_documents.py"""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.mkdtemp(prefix="memoinall-docs-")
os.environ["MEMOINALL_HOME"] = TMP
os.environ["MEMOINALL_DISABLE_ST"] = "1"
for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
    os.environ.pop(key, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from memoinall import db, documents, store  # noqa: E402
from memoinall.api import app  # noqa: E402
from document_fixtures import hwp_bytes, hwpx_bytes, pdf_bytes, xls_bytes, xlsx_bytes  # noqa: E402

PASS = FAIL = 0


def check(label, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("OK", label)
    else:
        FAIL += 1
        print("FAIL", label)


def rejects(fn, message=""):
    try:
        fn()
    except documents.DocumentError as exc:
        return not message or message in str(exc)
    return False


def main():
    with TestClient(app) as client:
        xlsx = xlsx_bytes()
        p = documents.extract("업무.xlsx", xlsx)
        check("엑셀의 보이는 시트별 추출", len(p["parts"]) == 2)
        check("시트와 행, 셀 순서 보존", "2행: 결제 점검\t김민수\t1200" in p["parts"][0]["text"])
        check("저장된 계산 값 없는 수식 보존", "=SUM(C2:C2)" in p["parts"][0]["text"] and any("수식" in w for w in p["warnings"]))
        check("숨김 시트 제외 안내", "숨긴 자료" not in str(p["parts"]) and any("숨겨진" in w for w in p["warnings"]))
        old = documents.extract("업무.xls", xls_bytes())
        check("구형 XLS 추출", "구형 엑셀 인수인계" in old["parts"][0]["text"])
        pdf = documents.extract("project.pdf", pdf_bytes())
        check("PDF 텍스트 추출", "Project handoff" in pdf["parts"][0]["text"])
        check("PDF 쪽 번호와 읽지 못한 페이지 안내", pdf["parts"][0]["label"] == "1쪽" and any("2쪽" in w for w in pdf["warnings"]))
        check("암호 PDF 거부", rejects(lambda: documents.extract("x.pdf", pdf_bytes(encrypted=True)), "암호"))
        check("이미지만 있는 PDF 빈 결과 안내", rejects(lambda: documents.extract("scan.pdf", pdf_bytes(blank=True)), "OCR"))
        xml = documents.extract("업무.hwpx", hwpx_bytes())
        check("HWPX 본문 순서 사용", xml["parts"][0]["text"] == "먼저 읽을 구역")
        check("HWPX 텍스트 중복 없이 줄 구분", xml["parts"][1]["text"] == "한글 업무 인수인계 자료\n다음 확인")
        check("HWPX XML 외부·내부 엔티티 거부", rejects(lambda: documents.extract("x.hwpx", hwpx_bytes(malicious=True))))
        check("암호 HWPX 거부", rejects(lambda: documents.extract("x.hwpx", hwpx_bytes(encrypted=True)), "암호"))
        hwp = documents.extract("업무.hwp", hwp_bytes())
        check("HWP 5 본문 압축·문자·표 제어 처리", "한글 본문 표 내용 😀\t다음" in hwp["parts"][0]["text"])
        check("HWP 암호 플래그 거부", rejects(lambda: documents.extract("x.hwp", hwp_bytes(flags=3)), "암호"))
        check("HWP 배포용 플래그 거부", rejects(lambda: documents.extract("x.hwp", hwp_bytes(flags=5)), "배포"))
        check("손상된 HWP 레코드 거부", rejects(lambda: documents.extract("x.hwp", hwp_bytes(broken=True))))
        check("구형 HWP 대안 안내", rejects(lambda: documents.extract("x.hwp", b"HWP Document File V3.00"), "HWPX"))
        for encoding in ("utf-8-sig", "utf-16", "cp949"):
            check(f"텍스트 {encoding}", documents.extract("업무.txt", "업무 메모".encode(encoding))["parts"][0]["text"] == "업무 메모")
        check("지원하지 않는 확장자 거부", rejects(lambda: documents.extract("x.exe", b"x")))
        check("빈 파일 거부", rejects(lambda: documents.extract("x.txt", b"")))
        with patch.object(documents, "MAX_BYTES", 1):
            check("파일 크기 제한", rejects(lambda: documents.extract("x.pdf", b"xx")))
            check("HTTP 업로드 크기 제한", client.post("/api/import/documents/preview?filename=x.txt", content=b"xx").status_code == 413)
        with patch.object(documents, "MAX_UNPACKED", 1):
            check("압축 해제 크기 제한", rejects(lambda: documents.extract("x.xlsx", xlsx)))
        with patch.object(documents, "MAX_CELLS", 1):
            check("엑셀 셀 수 제한", rejects(lambda: documents.extract("x.xlsx", xlsx)))
        with patch.object(documents, "MAX_TEXT", 1):
            check("추출 텍스트 길이 제한", rejects(lambda: documents.extract("x.hwpx", hwpx_bytes())))

        before = store.stats()["memos"]
        preview = client.post("/api/import/documents/preview", params={"filename": r"C:\자료\업무.xlsx"},
                              content=xlsx, headers={"content-type": "application/octet-stream"})
        check("파일 업로드 API", preview.status_code == 200)
        check("미리보기는 메모 저장 안 함", store.stats()["memos"] == before)
        check("원본 경로 제외", preview.json()["filename"] == "업무.xlsx")
        part = {**preview.json()["parts"][0], "text": "수정한 인수인계 내용\n[ ] 로그 확인 #추출검증"}
        payload = {"filename": preview.json()["filename"], "parts": [part]}
        saved = client.post("/api/import/documents/save", json=payload).json()
        check("선택·편집한 부분만 저장", saved["imported"] == 1 and store.stats()["memos"] == before + 1)
        memo = store.get_memo(saved["memos"][0]["id"])
        check("파일명·시트·편집 내용 보존", "업무.xlsx" in memo["body"] and "시트: 인수인계" in memo["body"] and "수정한 인수인계" in memo["body"])
        check("문서 출처 구분", memo["source"] == "document" and "#문서 #엑셀" in memo["body"])
        again = client.post("/api/import/documents/save", json=payload).json()
        check("재저장 멱등", again["imported"] == 0 and again["skipped_duplicate"] == 1 and store.stats()["memos"] == before + 1)
        hits = client.get("/api/search", params={"q": "수정한 인수인계 추출검증"}).json()["items"]
        check("저장 즉시 검색 가능", any(m["id"] == memo["id"] for m in hits))
        for parts in ([], [{**part, "text": " "}], [part, part], [{"key": 1}], "all"):
            check("잘못된 저장 요청 거부", client.post("/api/import/documents/save", json={"filename": "업무.xlsx", "parts": parts}).status_code == 400)
        check("저장 요청 전체 검증 후 반영", store.stats()["memos"] == before + 1)
        fresh = [{"key": "a", "label": "첫 번째", "text": "트랜잭션 확인 첫 번째"},
                 {"key": "b", "label": "두 번째", "text": "트랜잭션 확인 두 번째"}]
        rolled_back = False
        with patch.object(documents.textutil, "ngram_index_text", side_effect=["first", RuntimeError("index failure")]):
            try:
                documents.save("rollback.txt", fresh)
            except RuntimeError:
                rolled_back = True
        check("중간 오류 시 메모 전체 롤백", rolled_back and store.stats()["memos"] == before + 1)
        check("롤백한 검색 색인도 제거", not db.query("SELECT rowid FROM memo_fts WHERE raw LIKE '%트랜잭션 확인%'"))
        check("저장 원본은 디스크에 남기지 않음", not list(Path(TMP).glob("*.xlsx")))
    print(f"통과 {PASS} · 실패 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
