"""엑셀 폴더 취합 화면 흐름.  python tests/test_collection_ui.py

선택 의존성: playwright 와 Chromium. 요청은 임시 DB 의 TestClient 로 처리하고, AI 출력만 대체한다.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="memoinall-collect-ui-")
os.environ["MEMOINALL_HOME"] = TMP
os.environ["MEMOINALL_DISABLE_ST"] = "1"
for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
    os.environ.pop(key, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import Workbook, load_workbook  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from memoinall.api import app  # noqa: E402

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def workbook(rows, title="신청내역") -> bytes:
    book = Workbook()
    ws = book.active
    ws.title = title
    for row in rows:
        ws.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


FILES = [
    ("영업1팀.xlsx", workbook([["부서", "성명", "신청금액(원)", "처리상태"],
                             ["영업1팀", "김가람", 120000, "처리 완료"], ["영업1팀", "이나래", 80000, "미처리"],
                             ["영업1팀", "박다온", 45000, "처리 완료"]])),
    ("영업2팀.xlsx", workbook([["11월 신청 내역"], [], ["처리상태", "성명", "부서", "신청금액(원)"],
                             ["처리 완료", "최라온", "영업2팀", 90000], ["미처리", "정마루", "영업2팀", 110000],
                             ["", "합계", "", 200000]])),
    ("지원팀.xlsx", workbook([["부서", "성명", "요청액(원)", "처리상태"],
                            ["지원팀", "윤새봄", 60000, "미처리"], ["지원팀", "서아람", 40000, "처리 완료"]])),
    ("~$영업1팀.xlsx", b"lock"),
    ("readme.txt", b"note"),
]


def main():
    errors = []
    with TestClient(app) as client, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900}, accept_downloads=True)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            sent = {"content-type": request.headers["content-type"]} if "content-type" in request.headers else {}
            response = client.request(request.method, path, content=request.post_data_buffer, headers=sent)
            headers = {k: v for k, v in response.headers.items() if k not in {"content-length", "content-encoding"}}
            if path == "/api/generate" and response.status_code == 200:
                data = response.json()
                if data.get("collection"):
                    data["output"] = f"신청금액 합계는 545,000원입니다 [C{data['collection']['id']}]. 미처리 999,999건은 확인 필요."
                    from memoinall import ai_history

                    data.update({k: v for k, v in ai_history.create(data).items()
                                 if k in ("history_id", "version", "latest_version", "draft_body", "memo_id", "versions")})
                route.fulfill(status=200, headers=headers, body=json.dumps(data, ensure_ascii=False))
            else:
                route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("http://127.0.0.1:8787/**", serve)
        page.goto("http://127.0.0.1:8787/")
        expect(page.locator("#capture")).to_be_enabled()
        expect(page.locator("#tabs > button")).to_have_count(3)
        page.locator("#moreMenu > summary").click()
        expect(page.locator('.more-items [data-tab="collect"]')).to_have_count(0)
        page.keyboard.press("Escape")

        page.locator('[data-tab="import"]').click()
        page.locator("#openCollect").click()
        expect(page.locator(".col-steps [aria-current=step]")).to_have_text("1. 폴더 선택")
        expect(page.locator('[data-tab="import"]')).to_have_class("tab on")
        expect(page.locator("#colPickFolder")).to_be_visible()
        print("OK collection screen opens from document import")

        page.locator("#colFiles").set_input_files([{"name": n, "mimeType": XLSX, "buffer": b} for n, b in FILES])
        expect(page.locator(".col-steps [aria-current=step]")).to_have_text("2. 취합 확인", timeout=20000)
        expect(page.locator(".col-summary")).to_contain_text("확인 필요")
        expect(page.locator("#colCommit")).to_be_disabled()
        expect(page.locator("#view")).to_contain_text("엑셀 임시 파일(~$) 1개는 제외했습니다")
        expect(page.locator("#view")).to_contain_text("엑셀이 아닌 파일 1개는 찾지 않았습니다")
        print("OK review blocks saving until issues are resolved")

        page.locator("#colIssuesOnly").check()
        expect(page.locator(".col-file")).to_have_count(2)
        page.locator("#colIssuesOnly").uncheck()

        page.get_by_role("button", name="이 행을 항목 이름으로 사용").click()
        page.get_by_role("button", name="이 행 빼고 취합").click()
        page.get_by_role("button", name="‘신청금액(원)’과 같은 항목으로 사용").click()
        expect(page.locator("#colCommit")).to_be_enabled()
        expect(page.locator("#colCommit")).to_have_text("3개 파일 · 7행 취합하기")
        page.locator("#colTitle").fill("11월 신청 내역")
        page.locator("#colTags").fill("11월")
        print("OK header, total row, and similar column are confirmed by the user")

        page.locator("#colCommit").click()
        expect(page.locator(".col-steps [aria-current=step]")).to_have_text("3. 결과 활용", timeout=20000)
        expect(page.locator("#view h3")).to_have_text("11월 신청 내역")
        expect(page.locator(".col-main tbody tr")).to_have_count(7)
        expect(page.locator("#view")).to_contain_text("신청금액(원) 합계 545,000")
        expect(page.locator(".col-issues")).to_contain_text("합계·반복 머리글로 확인한 1행")
        page.locator("[data-col-source]").nth(3).click()
        expect(page.locator("#colSource")).to_contain_text("원본 위치: 영업2팀.xlsx / 신청내역 시트 / 4행")
        page.locator("#colCriteria > summary").click()
        expect(page.locator('#colCriteria table[aria-label="부서별 집계"]')).to_contain_text("영업1팀")
        print("OK result shows rows, totals, issues, and row sources")

        with page.expect_download() as info:
            page.locator("#colExport").click()
        download = info.value
        assert download.suggested_filename == "11월 신청 내역_1차_취합.xlsx", download.suggested_filename
        saved = load_workbook(io.BytesIO(Path(download.path()).read_bytes()))
        assert saved.sheetnames == ["취합표", "집계", "확인 사항", "출처"], saved.sheetnames
        assert saved["취합표"].max_row == 8, saved["취합표"].max_row
        print("OK result downloads as an Excel workbook")

        page.locator("#colAi").click()
        expect(page.locator("#genScope")).to_contain_text("사용할 자료: 11월 신청 내역 · 파일 3개 · 7행", timeout=10000)
        expect(page.locator("[data-gen-starter]")).to_have_count(3)
        expect(page.locator("#genTagFilter")).to_be_hidden()
        expect(page.locator("#genPlan")).to_be_hidden()
        page.get_by_role("button", name="보고서 초안").click()
        assert "보고서 초안" in page.locator("#genInput").input_value()
        assert page.locator("#genFormat").input_value() == "report"
        page.locator("#genRun").click()
        expect(page.locator("#genOut")).to_contain_text("신청금액 합계는 545,000원입니다", timeout=10000)
        expect(page.locator("#genNumberCheck")).to_contain_text("999,999")
        expect(page.locator("#genNumberCheck")).not_to_contain_text("545,000")
        expect(page.locator("#genOut")).to_contain_text("사용할 자료: 11월 신청 내역")
        print("OK AI uses the fixed collection version and flags unknown numbers")

        page.locator("[data-output-collection]").click()
        expect(page.locator(".col-steps [aria-current=step]")).to_have_text("3. 결과 활용", timeout=10000)
        expect(page.locator("#view h3")).to_have_text("11월 신청 내역")

        page.locator('[data-tab="feed"]').click()
        card = page.locator(".card", has=page.locator(".badge", has_text="엑셀 취합"))
        expect(card).to_have_count(1)
        expect(card).to_contain_text("엑셀 취합: 11월 신청 내역")
        card.click()
        expect(page.locator("#dCollection")).to_be_visible()
        expect(page.locator("#dPack")).to_have_text("이 자료를 AI로 정리")
        page.locator("#dCollection").click()
        expect(page.locator(".col-main tbody tr")).to_have_count(7, timeout=10000)
        print("OK representative memo opens the collection")

        page.set_viewport_size({"width": 390, "height": 800})
        page.wait_for_timeout(200)
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, overflow
        print("OK result fits a phone-width window without page scrolling")

        page.locator("#colMore > summary").click()
        page.once("dialog", lambda d: d.accept())
        page.locator("#colDelete").click()
        expect(page.locator(".col-steps [aria-current=step]")).to_have_text("1. 폴더 선택", timeout=10000)
        expect(page.locator("#colSaved")).to_contain_text("아직 저장한 취합 자료가 없습니다")
        print("OK collection can be deleted")

        browser.close()
    assert not errors, errors
    print("ALL OK")


if __name__ == "__main__":
    try:
        main()
    finally:
        from memoinall import db

        db.close()
        shutil.rmtree(TMP, ignore_errors=True)
