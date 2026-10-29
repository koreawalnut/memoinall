"""브라우저 사용 흐름 회귀 테스트. python tests/test_ui.py

선택 의존성: playwright 와 Chromium. 모든 요청과 저장은 임시 DB 에서 처리한다.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="memoinall-ui-")
os.environ["MEMOINALL_HOME"] = TMP
os.environ["MEMOINALL_DISABLE_ST"] = "1"
for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
    os.environ.pop(key, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from memoinall import store  # noqa: E402
from memoinall.api import app  # noqa: E402
from document_fixtures import pdf_bytes, xlsx_bytes  # noqa: E402


def main():
    errors = []
    with TestClient(app) as client, sync_playwright() as playwright:
        for i in range(85):
            memo = store.add_memo(
                f"업무 기록 {i:03d}: 결제 점검 결과. @담당{'A' if i % 2 else 'B'} #업무",
                created_at=(datetime(2026, 1, 1) + timedelta(days=i)).isoformat(),
                enqueue_enrich=False,
            )
            store.enrich(memo["id"])

        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            response = client.request(request.method, path, content=request.post_data_buffer)
            headers = {key: value for key, value in response.headers.items()
                       if key not in {"content-length", "content-encoding"}}
            if path == "/api/generate" and response.status_code == 200:
                data = response.json()
                if data["sources"]:
                    # 실제 API 의 근거를 유지하고 LLM 출력만 대체한다.
                    data["output"] = f"인수인계 요약 [M{data['sources'][0]['id']}]"
                route.fulfill(status=200, headers=headers, body=json.dumps(data, ensure_ascii=False))
            else:
                route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("http://127.0.0.1:8787/**", serve)
        page.goto("http://127.0.0.1:8787/")
        expect(page.locator("#capture")).to_be_enabled()
        expect(page.locator(".card[data-id]")).to_have_count(40)
        expect(page.locator("#tabs > button")).to_have_count(3)
        expect(page.locator("#tabs > button")).to_have_text(["내 메모", "AI로 정리", "가져오기·공유"])
        expect(page.locator('[data-tab="settings"]')).not_to_be_visible()
        expect(page.locator("#feedFilters")).not_to_be_visible()
        expect(page.locator("#selectionBar")).not_to_be_visible()
        expect(page.locator("[data-select]").first).not_to_be_visible()
        expect(page.locator("#captureTags")).not_to_be_visible()
        print("OK first screen shows three primary menus and hides optional tools")
        # 보조 메뉴는 마우스와 키보드로 열 수 있고 실제 화면에 연결돼야 한다.
        page.locator("#moreMenu > summary").focus()
        page.keyboard.press("Enter")
        expect(page.locator('[data-tab="todos"]')).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator('[data-tab="todos"]')).not_to_be_visible()
        for tab, text in [("todos", "할 일"), ("clusters", "주제별 묶음"), ("rollup", "기간 요약"), ("settings", "설정")]:
            page.locator("#moreMenu > summary").click()
            page.locator(f'[data-tab="{tab}"]').click()
            expect(page.locator("#lead h2")).to_contain_text(text)
            expect(page.locator("#view")).not_to_contain_text("불러오는 중")
            expect(page.locator("#view")).not_to_contain_text("오류:")
        page.locator('[data-tab="feed"]').click()
        expect(page.locator(".card[data-id]")).to_have_count(40)
        print("OK secondary menus stay reachable with keyboard and pointer")
        page.locator("#toggleSelection").click()
        first_id = page.locator("[data-select]").first.get_attribute("data-select")
        page.locator("[data-select]").first.check()
        expect(page.locator("#detail")).not_to_be_visible()
        page.locator("#nextPage").click()
        expect(page.locator("#view")).to_contain_text("41–80번째")
        page.locator("[data-select]").first.check()
        second_id = page.locator("[data-select]").first.get_attribute("data-select")
        page.locator("#exportSelection").click()
        expect(page.locator("#expOut")).to_contain_text("2건")
        selected = page.evaluate("exportPayload().ids")
        assert set(selected) == {int(first_id), int(second_id)}
        expect(page.locator("#expQ")).to_be_disabled()
        exported = client.post("/api/export/content", json={"ids": selected}).json()
        assert json.loads(exported["content"])["count"] == 2
        print("OK selection survives pagination; export contains only selected memos")

        page.locator('[data-tab="feed"]').click()
        page.locator("#generateSelection").click()
        expect(page.locator("#genScope")).to_contain_text("2건만 사용")
        page.locator("#genPlan").click()
        expect(page.locator("#genPlanOut")).to_contain_text("직접 선택한 근거")
        assert set(page.evaluate("gen.plan.sources.map(s => s.id)")) == set(selected)
        page.locator("#genFormat").select_option("handoff")
        page.locator("#genRun").click()
        expect(page.locator("#genPlanOut")).to_contain_text("남은 할 일")
        page.locator("[data-output-source]").click()
        expect(page.locator("#detail")).to_be_visible()
        page.locator("#dClose").click()
        print("OK selected AI scope, handoff format and clickable source citation")

        page.locator('[data-tab="feed"]').click()
        if page.locator("#clear").is_visible():
            page.locator("#clear").click()
        page.locator('#people [data-v="담당A"]').click()
        expect(page.locator("#activeFilters")).to_contain_text("@담당A")
        page.locator("#toggleFilters").click()
        page.locator("#filterSince").fill("2026-02-01")
        page.locator("#filterUntil").fill("2026-03-01")
        page.locator("#toggleFilters").click()
        expect(page.locator("#activeFilters")).to_contain_text("2026-02-01부터")
        expect(page.locator("#feedFilters")).not_to_be_visible()
        page.locator("#toggleFilters").click()
        page.locator("#exportHits").click()
        expect(page.locator("#exportScope")).to_contain_text("@담당A")
        payload = page.evaluate("exportPayload()")
        assert payload["person"] == "담당A" and payload["since"] == "2026-02-01" and payload["until"] == "2026-03-01"
        print("OK person and date filters carry over to export")

        outside = {"format": "memoinall/memos", "version": 1, "memos": [
            {"body": "동료가 보낸 인수인계 메모 #공유", "created_at": "2026-04-01T09:00:00"}]}
        page.locator("#sharedFile").set_input_files({
            "name": "handoff.json", "mimeType": "application/json",
            "buffer": json.dumps(outside, ensure_ascii=False).encode("utf-8"),
        })
        expect(page.locator("#impOut-shared")).to_contain_text("미리보기")
        expect(page.locator("#impOut-shared")).to_contain_text("아직 저장 안 함")
        page.locator('[data-src-run="shared"]').click()
        expect(page.locator("#impOut-shared")).to_contain_text("가져오기 완료")
        assert store.count_by_source("shared")["memos"] == 1
        print("OK file picker previews and imports shared memos")
        page.locator('[data-exchange-tab="import"]').click()
        expect(page.locator("#sharedFile")).not_to_be_visible()
        expect(page.locator("#expPreview")).not_to_be_visible()
        expect(page.get_by_text("어디에 적어두셨나요?", exact=True)).to_be_visible()
        expect(page.locator("#rmUrl")).not_to_be_visible()
        expect(page.locator("#impMin")).not_to_be_visible()
        # 다른 앱 일괄 가져오기가 숨겨진 받은 파일까지 가져오지 않는지 확인한다.
        called = page.evaluate("""async () => {
            const original = window.fetch;
            const called = [];
            window.fetch = async (url, opts) => {
                if (url === '/api/import/preview') {
                    const source = JSON.parse(opts.body).source;
                    called.push(source);
                    return new Response(JSON.stringify({dry_run: true, total: 0, total_updated: 0,
                        results: [{source, available: false, error: '테스트 자료 없음'}]}),
                        {headers: {'content-type': 'application/json'}});
                }
                return original(url, opts);
            };
            try { await doImport(true); return called; }
            finally { window.fetch = original; }
        }""")
        assert called == ["sticky", "samsung", "files", "redmine"], called
        page.locator('[data-exchange-tab="share"]').click()
        expect(page.locator("#sharedFileStatus")).to_contain_text("handoff.json")
        print("OK import and sharing show separate tools; batch scope excludes hidden received file")

        page.locator('[data-exchange-tab="import"]').click()
        before_document = store.stats()["memos"]
        with page.expect_file_chooser() as chooser:
            page.locator("#chooseDocument").click()
        chooser.value.set_files({"name": "인수인계.xlsx", "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "buffer": xlsx_bytes()})
        expect(page.locator("#documentPreview")).to_contain_text("인수인계.xlsx")
        expect(page.locator("[data-document-part]")).to_have_count(2)
        assert store.stats()["memos"] == before_document
        page.locator('[data-document-part="1"]').uncheck()
        page.locator('[data-document-text="0"]').fill("브라우저에서 확인한 인수인계 내용 #문서검증")
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.locator("#documentFile").set_input_files({"name": "다른파일.txt", "mimeType": "text/plain", "buffer": "새 파일".encode("utf-8")})
        expect(page.locator('[data-document-text="0"]')).to_have_value("브라우저에서 확인한 인수인계 내용 #문서검증")
        expect(page.locator("#documentFile")).to_have_value("")
        page.locator('[data-tab="feed"]').click()
        page.locator('[data-tab="import"]').click()
        expect(page.locator('[data-document-text="0"]')).to_have_value("브라우저에서 확인한 인수인계 내용 #문서검증")
        page.locator("#documentSave").click()
        expect(page.locator("#documentResult")).to_contain_text("1개 메모를 저장했습니다")
        assert store.stats()["memos"] == before_document + 1
        doc_memo = store.get_memo(page.evaluate("docImport.result.memos[0].id"))
        assert "브라우저에서 확인한 인수인계 내용" in doc_memo["body"] and "시트: 인수인계" in doc_memo["body"]
        page.locator("#documentSave").click()
        expect(page.locator("#documentResult")).to_contain_text("이미 저장된 내용 1개")
        assert store.stats()["memos"] == before_document + 1
        if "--screenshots" in sys.argv:
            artifacts = Path(__file__).resolve().parent.parent / "artifacts" / "ux-review"
            artifacts.mkdir(parents=True, exist_ok=True)
            page.locator("#toast").evaluate("el => el.classList.remove('show')")
            page.locator("#documentPanel").screenshot(path=str(artifacts / "documents.png"))
        page.locator("#documentGenerate").click()
        expect(page.locator("#genScope")).to_contain_text("1건만 사용")
        print("OK document upload previews, preserves edits, saves selected sheet, deduplicates and links to AI")
        page.locator('[data-tab="import"]').click()
        page.locator("#documentFile").set_input_files({"name": "scan.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(blank=True)})
        expect(page.locator("#documentPreview")).to_contain_text("OCR")
        expect(page.locator("#documentSave")).to_have_count(0)
        assert store.stats()["memos"] == before_document + 1
        print("OK image-only document gives OCR guidance without creating an empty memo")

        page.locator('[data-tab="feed"]').click()
        page.locator("#clear").click()
        page.locator(".card[data-id]").first.click()
        expect(page.locator("#detail")).to_be_visible()
        original = page.locator("#dBody").input_value()
        page.locator("#dBody").fill(original + " 수정 중")
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.locator("#dClose").click()
        expect(page.locator("#detail")).to_be_visible()
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.keyboard.press("Escape")
        expect(page.locator("#detail")).to_be_visible()
        page.locator("#dBody").fill(original)
        page.locator("#dPack").click()
        expect(page.locator("#genScope")).to_contain_text("1건만 사용")
        page.locator("#genPlan").click()
        expect(page.locator("#genPlanOut")).to_contain_text("직접 선택한 근거")
        assert len(page.evaluate("gen.plan.sources")) == 1
        print("OK unsaved edit protection; detail-to-AI uses exactly one memo")

        page.locator('[data-tab="feed"]').click()
        tag_body = "기존 메모 #업무 #업무/확인 #업무중\n정렬  공백\n\n다음 문단 #업무"
        tag_memo = store.add_memo(tag_body, enqueue_enrich=False)
        untouched = store.add_memo("다른 메모 #업무", enqueue_enrich=False)
        store.enrich(tag_memo["id"])
        store.enrich(untouched["id"])
        client.post("/api/tags", json={"name": "등록태그", "color": "blue"})
        page.evaluate("async id => { await loadTags(); await openMemo(id); }", tag_memo["id"])
        expect(page.locator('[data-detail-remove="업무"]')).to_have_count(1)
        page.locator('[data-detail-remove="업무"]').click()
        expect(page.locator('[data-detail-remove="업무"]')).to_have_count(0)
        expect(page.locator('[data-detail-remove="업무/확인"]')).to_be_visible()
        expect(page.locator('[data-detail-remove="업무중"]')).to_be_visible()
        assert "정렬  공백\n\n다음 문단" in page.locator("#dBody").input_value()
        page.locator("#dTagInput").fill("임시분류")
        page.locator("#dTagAdd").click()
        assert store.get_memo(tag_memo["id"])["body"] == tag_body
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.locator("#dClose").click()
        expect(page.locator("#detail")).to_be_visible()
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#dClose").click()
        page.evaluate("id => openMemo(id)", tag_memo["id"])
        expect(page.locator('[data-detail-remove="업무"]')).to_be_visible()
        expect(page.locator('[data-detail-remove="임시분류"]')).to_have_count(0)
        # 입력만 하고 추가하지 않은 태그도 닫을 때 보호한다.
        page.locator("#dTagInput").fill("입력중")
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.keyboard.press("Escape")
        expect(page.locator("#dTagInput")).to_have_value("입력중")
        page.locator("#dTagInput").fill("")
        print("OK tag removal keeps prefix tags and formatting; unsaved tag changes and pending input are protected")

        page.locator("#dTagInput").fill("잘못된!")
        page.locator("#dTagAdd").click()
        expect(page.locator("#dTagMessage")).to_contain_text("1~40자")
        expect(page.locator("#dBody")).to_have_value(tag_body)
        page.locator("#dTagInput").fill("#업무")
        page.locator("#dTagInput").press("Enter")
        expect(page.locator("#dTagMessage")).to_contain_text("이미 있습니다")
        expect(page.locator("#dBody")).to_have_value(tag_body)
        page.locator('[data-detail-remove="업무"]').click()
        page.locator("#dTagInput").fill("새 분류")
        page.locator("#dTagInput").press("Enter")
        expect(page.locator('[data-detail-remove="새_분류"]')).to_be_visible()
        page.locator("#dTagPicker > summary").click()
        page.locator('[data-detail-add="등록태그"]').click()
        expect(page.locator('[data-detail-remove="등록태그"]')).to_be_visible()
        edited = page.locator("#dBody").input_value()
        page.locator("#dBody").fill(edited + "\n#본문태그")
        expect(page.locator('[data-detail-remove="본문태그"]')).to_be_visible()
        page.locator("#dBody").fill(edited)
        expect(page.locator('[data-detail-remove="본문태그"]')).to_have_count(0)
        page.locator("#dTagInput").fill("저장할태그")
        if "--screenshots" in sys.argv:
            artifacts = Path(__file__).resolve().parent.parent / "artifacts" / "ux-review"
            artifacts.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifacts / "memo-tags.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.locator("#detail").evaluate("dialog => dialog.scrollWidth <= dialog.clientWidth")
            page.screenshot(path=str(artifacts / "memo-tags-mobile.png"))
            page.set_viewport_size({"width": 1280, "height": 900})
        page.locator("#dSave").click()
        expect(page.locator("#detail")).not_to_be_visible()
        saved = store.get_memo(tag_memo["id"])
        assert set(saved["facets"]["tag"]) == {"업무/확인", "업무중", "새_분류", "등록태그", "저장할태그"}
        assert store.get_memo(untouched["id"])["body"] == untouched["body"]
        assert [t["name"] for t in client.get("/api/tags").json()["items"]] == ["등록태그"]
        page.locator('#tags [data-v="새_분류"]').click()
        expect(page.locator(".card[data-id]")).to_have_count(1)
        assert page.locator(".card[data-id]").get_attribute("data-id") == str(tag_memo["id"])
        page.locator(".card[data-id]").click()
        expect(page.locator('[data-detail-remove="새_분류"]')).to_be_visible()
        page.locator("#dClose").click()
        page.locator("#clear").click()
        client.delete(f"/api/memos/{tag_memo['id']}")
        client.delete(f"/api/memos/{untouched['id']}")
        client.delete("/api/tags/등록태그")
        page.evaluate("async () => { await loadTags(); await loadSidebar(); await render(); }")
        print("OK existing memo tags support validation, duplicate protection, reusable tags, body sync, save and filtering")

        page.locator('[data-tab="feed"]').click()
        page.locator("#capture").fill("복원할 임시 메모 #초안")
        expect(page.locator("#captureHint")).to_contain_text("초안 보관됨")
        assert client.get("/api/draft").json()["body"] == "복원할 임시 메모 #초안"
        client.put("/api/draft", json={"body": "서버에 남은 이전 초안"})
        page.reload()
        expect(page.locator("#capture")).to_have_value("복원할 임시 메모 #초안")
        expect(page.locator("#capture")).to_be_enabled()
        assert client.get("/api/draft").json()["body"] == "복원할 임시 메모 #초안"
        page.evaluate("localStorage.clear()")
        page.reload()
        expect(page.locator("#capture")).to_have_value("복원할 임시 메모 #초안")
        expect(page.locator("#capture")).to_be_enabled()
        page.locator("#save").click()
        expect(page.locator("#capture")).to_have_value("")
        assert client.get("/api/draft").json()["body"] == ""
        print("OK server draft restores without localStorage and clears after save")

        # 두 번의 Ctrl+Enter 와 저장 응답을 기다리는 사이의 입력.
        page.locator("#capture").fill("한 번만 저장할 메모")
        before = store.stats()["memos"]
        page.evaluate("save(); save(); document.querySelector('#capture').value = '저장 중 이어 쓴 내용'; captureChanged();")
        expect(page.locator("#save")).to_be_enabled()
        expect(page.locator("#captureHint")).to_contain_text("초안 보관됨")
        assert store.stats()["memos"] == before + 1
        expect(page.locator("#capture")).to_have_value("저장 중 이어 쓴 내용")
        print("OK duplicate save blocked; typing during save preserved")

        # 검색 요청이 늦게 끝나도 이미 이동한 탭을 덮어쓰지 않는다.
        page.locator("#capture").fill("")
        expect(page.locator("#captureHint")).to_contain_text("Ctrl+Enter 저장")
        page.evaluate("""() => {
            const original = window.fetch;
            window.fetch = async (...args) => {
                if (String(args[0]).startsWith('/api/search?')) await new Promise(resolve => setTimeout(resolve, 700));
                return original(...args);
            };
        }""")
        page.locator("#q").fill("결제")
        page.wait_for_timeout(300)
        page.locator("#moreMenu > summary").click()
        page.locator('[data-tab="settings"]').click()
        expect(page.locator("#view")).to_contain_text("LLM 연결")
        page.wait_for_timeout(800)
        expect(page.locator("#view")).to_contain_text("LLM 연결")
        page.locator('[data-tab="feed"]').click()
        page.locator("#clear").click()
        expect(page.locator(".card[data-id]")).to_have_count(40)
        print("OK delayed search cannot overwrite another tab")
        if page.locator("#toggleFilters").get_attribute("aria-expanded") == "true":
            page.locator("#toggleFilters").click()
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator("aside")).not_to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if "--screenshots" in sys.argv:
            artifacts = Path(__file__).resolve().parent.parent / "artifacts" / "ux-review"
            artifacts.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifacts / "mobile.png"))
            page.set_viewport_size({"width": 1280, "height": 900})
            page.screenshot(path=str(artifacts / "desktop.png"))
        assert not errors, errors
        print("OK narrow layout; no uncaught browser errors")
        browser.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
