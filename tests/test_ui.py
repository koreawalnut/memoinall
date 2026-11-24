"""브라우저 사용 흐름 회귀 테스트. python tests/test_ui.py

선택 의존성: playwright 와 Chromium. 모든 요청과 저장은 임시 DB 에서 처리한다.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from unittest.mock import patch
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="memoinall-ui-")
os.environ["MEMOINALL_HOME"] = TMP
os.environ["MEMOINALL_DISABLE_ST"] = "1"
for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
    os.environ.pop(key, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from memoinall import exchange, store  # noqa: E402
from memoinall.api import app  # noqa: E402
from document_fixtures import pdf_bytes, xlsx_bytes  # noqa: E402


def check_feed_search_tools(page, client):
    page.locator("#q").fill("결제")
    page.locator('#tags [data-v="업무"]').click()
    expect(page.locator("#view")).to_contain_text("뜻이 비슷한")
    page.locator("#nextPage").click()
    expect(page.locator("#view")).to_contain_text("41–80")
    page.locator("#documentFromSearch").click()
    expect(page.locator("#genScope")).to_contain_text("‘결제’ 검색 결과")
    expect(page.locator("#genScope")).to_contain_text("85건만 사용")
    page.locator("#genPlan").click()
    expect(page.locator("#genRefCount")).to_have_text("85건")
    expect(page.locator("#genAddPanel")).not_to_be_visible()
    expected = set()
    for offset in (0, 40, 80):
        response = client.get("/api/search", params={"q": "결제", "tags": "업무", "limit": 40, "offset": offset})
        expected.update(m["id"] for m in response.json()["items"])
    assert set(page.evaluate("genPayload().memo_ids")) == expected
    page.locator("#genPicker [data-pick]").first.uncheck()
    page.locator("#genRun").click()
    page.wait_for_function("!gen.busy && gen.plan?.scope?.feed_query === '결제'")
    assert len(page.evaluate("gen.plan.memo_ids")) == 84
    page.locator('[data-tab="feed"]').click()
    page.locator('#people [data-v="담당A"]').click()
    page.locator("#toggleFilters").click()
    page.locator("#filterSince").fill("2026-02-01")
    page.locator("#filterUntil").fill("2026-02-28")
    page.locator("#saveSearch").click()
    page.locator("#presetName").fill("2월 결제 점검")
    page.locator("#saveSearchForm [type=submit]").click()
    expect(page.locator("#saveSearchDialog")).not_to_be_visible()
    page.locator("#saveSearch").click()
    page.locator("#presetName").fill("이번 주 결제")
    page.locator("#presetPeriod").select_option("this_week")
    page.locator("#saveSearchForm [type=submit]").click()
    expect(page.locator("#saveSearchDialog")).not_to_be_visible()
    page.reload()
    page.locator("#savedSearches > summary").click()
    page.get_by_role("button", name="2월 결제 점검", exact=True).click()
    expect(page.locator("#q")).to_have_value("결제")
    for token in ("#업무", "@담당A", "2026-02-01", "2026-02-28"):
        expect(page.locator("#activeFilters")).to_contain_text(token)
    page.locator('[data-tab="generate"]').click()
    page.locator("#genScopeOptions > summary").click()
    page.locator("#genFiltered").click()
    expect(page.locator("#genScope")).to_contain_text("14건만 사용")
    expect(page.locator("#genScope")).to_contain_text("‘결제’")
    page.locator('[data-tab="feed"]').click()
    for monday in (date(2026, 2, 2), date(2026, 2, 9)):
        with patch("memoinall.workflows.today", return_value=monday):
            page.locator("#savedSearches > summary").click()
            page.get_by_role("button", name="이번 주 결제", exact=True).click()
            expect(page.locator("#activeFilters")).to_contain_text(monday.isoformat())
    page.locator("#savedSearches > summary").click()
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="이번 주 결제 저장된 검색 삭제").click()
    expect(page.get_by_role("button", name="이번 주 결제", exact=True)).to_have_count(0)
    assert client.post("/api/saved-searches", json={"name":"2월 결제 점검", "q":"다른 검색"}).status_code == 409
    assert client.post("/api/generate/plan", json={"instruction":"정리", "feed_query":"결제"}).status_code == 400
    if not page.locator("#filterSince").is_visible():
        page.locator("#toggleFilters").click()
    page.locator("#filterSince").fill("2040-01-01")
    page.locator("#filterUntil").fill("2040-01-31")
    expect(page.locator("#view")).to_contain_text("조건에 맞는 메모가 없습니다")
    page.locator('[data-tab="generate"]').click()
    page.locator("#genScopeOptions > summary").click()
    page.locator("#genFiltered").click()
    expect(page.locator("#genScope")).to_contain_text("0건만 사용")
    expect(page.locator("#genRun")).to_be_disabled()
    expect(page.locator("#genProgress")).to_contain_text("메모가 없습니다")
    page.reload()
    expect(page.locator(".card[data-id]")).to_have_count(40)
    print("OK paginated feed results stay fixed through AI selection; named and relative searches survive reload")


def check_sidebar_tag_groups(page):
    memo = store.add_memo("현장 방문 기록 #업무 #현장방문", enqueue_enrich=False)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.evaluate("loadSidebar()")
    mine = page.get_by_role("group", name="내 태그", exact=True)
    automatic = page.get_by_role("group", name="자동 태그", exact=True)
    expect(automatic.locator('[data-v="업무"]')).to_be_visible()
    page.get_by_role("button", name="내 태그 설정", exact=True).click()
    expect(page.locator("#newTag")).to_be_focused()
    page.locator("#newTag").fill("사용예정")
    page.locator("#newTagColor").select_option("blue")
    page.locator("#addTag").click()
    expect(mine.locator('.t-blue[data-v="사용예정"] .n')).to_have_text("0")
    page.locator('#suggestRow [data-adopt="업무"]').click()
    expect(mine.locator("button")).to_have_count(2)
    expect(automatic.locator('[data-v="업무"]')).to_have_count(0)
    page.locator('#tagList [data-tup="1"]').click()
    expect(mine.locator("button").first).to_have_attribute("data-v", "업무")
    assert mine.bounding_box()["y"] < automatic.bounding_box()["y"]
    page.reload()
    expect(mine.locator("button").first).to_have_attribute("data-v", "업무")
    mine.locator('[data-v="업무"]').click()
    automatic.locator('[data-v="현장방문"]').press("Enter")
    expect(mine.locator('[data-v="업무"]')).to_have_attribute("aria-pressed", "true")
    expect(automatic.locator('[data-v="현장방문"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".card[data-id]")).to_have_count(1)
    expect(page.locator(".card[data-id]")).to_have_attribute("data-id", str(memo["id"]))
    print("OK sidebar shows registered tags first in saved order; adopting moves the tag and both groups filter together")


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
            sent = {"content-type": request.headers["content-type"]} if "content-type" in request.headers else {}
            response = client.request(request.method, path, content=request.post_data_buffer, headers=sent)
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
        tour = page.locator("#starterTour")
        expect(tour).to_be_visible()
        expect(page.locator("#tourTitle")).to_have_text("기존 메모 가져오기")
        def check_spotlight(selector):
            frame = page.locator("#tourTarget").bounding_box()
            target = page.locator(selector).bounding_box()
            tip = page.locator("#tourTip").bounding_box()
            assert abs(frame["x"] - target["x"]) <= 5 and abs(frame["width"] - target["width"]) <= 10
            assert abs(frame["y"] - target["y"]) <= 5 and abs(frame["height"] - target["height"]) <= 10
            assert tip["y"] >= frame["y"] + frame["height"] or tip["y"] + tip["height"] <= frame["y"]
            assert page.locator(".tour-shade").evaluate("e => getComputedStyle(e).backdropFilter !== 'none' && getComputedStyle(e).clipPath !== 'none'")
        expect(page.locator('[data-tab="import"]')).to_have_attribute("aria-current", "page")
        check_spotlight("#chooseDocument")
        expect(page.locator("#chooseDocument")).to_be_focused()
        with page.expect_file_chooser() as file_chooser:
            page.keyboard.press("Enter")
        file_chooser.value.set_files([])
        page.locator("#tourImportSwitch").click()
        check_spotlight("#stickySource")
        assert page.locator("#stickySource").evaluate("e => e.contains(document.activeElement)")
        page.locator("#tourImportSwitch").click()
        expect(page.locator("#chooseDocument")).to_be_focused()
        page.locator("#tourNext").click()
        expect(page.locator("#tourTitle")).to_have_text("내 태그 1개 정하기")
        check_spotlight("#tourTagField")
        expect(page.locator("#newTag")).to_be_focused()
        page.keyboard.type("tutorial-check")
        page.keyboard.press("Enter")
        expect(page.locator("#tagList")).to_contain_text("tutorial-check")
        expect(page.locator("#newTag")).to_be_focused()
        page.locator("#tourNext").click()
        expect(page.locator("#tourTitle")).to_have_text("AI 연결하기")
        page.set_viewport_size({"width": 390, "height": 780})
        page.wait_for_timeout(150)
        check_spotlight("#tourProviders")
        expect(page.locator("#tourProviders .on")).to_be_focused()
        page.locator('[data-prov="openai"]').click()
        expect(page.locator("#setKey")).to_be_focused()
        page.keyboard.type("example-test-input")
        expect(page.locator("#setKey")).to_have_value("example-test-input")
        page.locator('[data-prov="ollama"]').click()
        expect(page.locator("#setModel")).to_be_focused()
        page.locator("#tourBack").click()
        expect(page.locator("#tourTitle")).to_have_text("내 태그 1개 정하기")
        expect(page.locator("#newTag")).to_be_focused()
        page.locator("#tourClose").click()
        expect(tour).to_be_hidden()
        expect(page.locator("#newTag")).to_be_focused()
        page.set_viewport_size({"width": 1280, "height": 900})
        client.delete("/api/tags/tutorial-check").raise_for_status()
        page.reload()
        expect(page.locator("#capture")).to_be_enabled()
        assert client.get("/api/starter").json() == {"visible": False}
        expect(tour).to_be_hidden()
        expect(page.locator(".card[data-id]")).to_have_count(40)
        expect(page.locator("#tabs > button")).to_have_count(5)
        expect(page.locator("#tabs > button")).to_have_text(["내 메모", "일정", "메모로 문서 만들기", "가져오기·공유", "설정"])
        # 설정은 '더 보기' 안이 아니라 가져오기·공유 바로 옆에 있다. '더 보기'는 맨 오른쪽.
        expect(page.locator('[data-tab="settings"]')).to_be_visible()
        assert page.evaluate("document.querySelector('[data-tab=\"settings\"]').previousElementSibling.dataset.tab") == "import"
        assert page.evaluate("document.querySelector('#tabs').lastElementChild.id") == "moreMenu"
        assert page.locator('.more-items [data-tab="settings"]').count() == 0
        expect(page.locator("#feedFilters")).not_to_be_visible()
        expect(page.locator("#selectionBar")).not_to_be_visible()
        expect(page.locator("[data-select]").first).not_to_be_visible()
        # 태그 넣기 칸은 내 태그가 없어도 늘 보인다(접힌 채로)
        expect(page.locator("#captureTags")).to_be_visible()
        expect(page.locator("#captureTags")).not_to_have_attribute("open", "")
        print("OK first screen shows four primary menus with settings next to 가져오기·공유 and hides optional tools")
        check_feed_search_tools(page, client)
        # 보조 메뉴는 마우스와 키보드로 열 수 있고 실제 화면에 연결돼야 한다.
        page.locator("#moreMenu > summary").focus()
        page.keyboard.press("Enter")
        expect(page.locator('[data-tab="tagboard"]')).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator('[data-tab="tagboard"]')).not_to_be_visible()
        page.locator('[data-tab="todos"]').click()
        expect(page.locator("#lead h2")).to_contain_text("일정")
        expect(page.locator('[data-todo-layout="calendar"]')).to_have_attribute("aria-pressed", "true")
        for tab, text in [("tagboard", "태그별 메모"), ("rollup", "기간 요약"), ("settings", "설정")]:
            if tab != "settings":
                page.locator("#moreMenu > summary").click()
            page.locator(f'[data-tab="{tab}"]').click()
            expect(page.locator("#lead h2")).to_contain_text(text)
            expect(page.locator("#view")).not_to_contain_text("불러오는 중")
            expect(page.locator("#view")).not_to_contain_text("오류:")
        expect(page.locator('[data-tab="settings"]')).to_have_class(re.compile(r"\bon\b"))
        expect(page.locator("#moreMenu > summary")).to_have_text(re.compile("더 보기"))   # 설정을 열어도 '더 보기'는 그대로
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
        expect(page.locator("#genPlanOut")).to_contain_text("참고할 메모")
        assert set(page.evaluate("gen.plan.sources.map(s => s.id)")) == set(selected)
        page.locator("#genFormat").select_option("handoff")
        page.locator("#genRun").click()
        expect(page.locator("#genPlanOut")).to_contain_text("남은 할 일")
        page.locator("[data-output-source]").click()
        expect(page.locator("#detail")).to_be_visible()
        memo_window = page.get_by_role("button", name="항상 위에 띄우기")
        expect(memo_window).to_be_hidden()
        page.evaluate("""() => {
            window.pywebview = {api: {open_memo_window: id => { window.openedMemo = id; }}};
            window.dispatchEvent(new Event('pywebviewready'));
        }""")
        memo_window.click()
        assert page.evaluate("window.openedMemo === state.memo.id")
        page.evaluate("() => { delete window.pywebview; window.dispatchEvent(new Event('pywebviewready')); }")
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
        assert called == ["sticky", "samsung", "onenote_local", "files", "redmine"], called
        page.locator('[data-exchange-tab="share"]').click()
        expect(page.locator("#sharedFileStatus")).to_contain_text("handoff.json")
        print("OK import and sharing show separate tools; batch scope excludes hidden received file")

        # 암호화된 파일: 고르면 비밀번호를 기다리고, 틀리면 알려주고, 맞으면 미리보기로 간다.
        locked = exchange.seal({"format": "memoinall/memos", "version": 1, "count": 1, "memos": [
            {"body": "암호화로 받은 인수인계 메모 #공유", "created_at": "2026-04-02T09:00:00"}]}, "같이쓰는암호")
        page.locator("#sharedFile").set_input_files({
            "name": "locked.json", "mimeType": "application/json",
            "buffer": json.dumps(locked).encode("utf-8"),
        })
        expect(page.locator("#sharedFileStatus")).to_contain_text("암호화된 파일입니다")
        expect(page.locator("#impSharedPw")).to_be_focused()
        page.locator("#impSharedPw").fill("틀린암호")
        page.locator("#impSharedPw").press("Enter")
        expect(page.locator("#sharedFileStatus")).to_contain_text("비밀번호가 맞지 않")
        page.locator("#impSharedPw").fill("같이쓰는암호")
        page.locator("#impSharedPw").press("Enter")
        expect(page.locator("#impOut-shared")).to_contain_text("아직 저장 안 함")
        page.locator('[data-src-run="shared"]').click()
        expect(page.locator("#impOut-shared")).to_contain_text("가져오기 완료")
        assert store.count_by_source("shared")["memos"] == 2
        print("OK encrypted shared file asks for password and imports")

        # 내보내기 비밀번호가 서로 다르면 아무것도 만들지 않는다.
        page.locator("#expPw").fill("하나")
        page.locator("#expPw2").fill("둘")
        page.locator("#expSave").click()
        expect(page.locator("#expOut")).to_contain_text("두 비밀번호가 다릅니다")
        # 비워두고 내보내려 하면 평문 경고를 띄우고, 취소하면 멈춘다.
        page.locator("#expPw").fill("")
        page.locator("#expPw2").fill("")
        warned = []
        page.once("dialog", lambda dialog: (warned.append(dialog.message), dialog.dismiss()))
        page.locator("#expSave").click()
        page.wait_for_timeout(200)
        assert warned and "암호화하지 않고" in warned[0], warned
        print("OK export password must match; plain export needs confirmation")

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
        expect(page.locator("#genPlanOut")).to_contain_text("참고할 메모")
        assert len(page.evaluate("gen.plan.sources")) == 1
        print("OK unsaved edit protection; detail-to-AI uses exactly one memo")

        concurrent = store.add_memo("동시 편집 원문", enqueue_enrich=False)["id"]
        page.evaluate("id => openMemo(id)", concurrent)
        store.update_memo(concurrent, "작은 창에서 먼저 저장", enqueue_enrich=False)
        page.evaluate("id => dispatchEvent(new CustomEvent('quick-memo-saved', {detail: id}))", concurrent)
        expect(page.locator("#dBody")).to_have_value("작은 창에서 먼저 저장")
        page.locator("#dBody").fill("메인 창의 편집본")
        store.update_memo(concurrent, "작은 창의 최신 본문", enqueue_enrich=False)
        page.evaluate("id => dispatchEvent(new CustomEvent('quick-memo-saved', {detail: id}))", concurrent)
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.locator("#dSave").click()
        expect(page.locator("#dSave")).to_be_enabled()
        expect(page.locator("#dBody")).to_have_value("메인 창의 편집본")
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#dSave").click()
        expect(page.locator("#detail")).to_be_hidden()
        assert store.get_memo(concurrent)["body"] == "작은 창의 최신 본문"
        copies = [m["id"] for m in store.list_memos() if m["body"] == "메인 창의 편집본"]
        assert len(copies) == 1
        for memo_id in [concurrent, *copies]:
            client.delete(f"/api/memos/{memo_id}").raise_for_status()
        print("OK main editor refreshes a clean note and preserves both bodies after a concurrent save")

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
        reference = store.add_memo("관련 이슈 #5102 확인 #배포", source="redmine", enqueue_enrich=False)
        page.evaluate("id => openMemo(id)", reference["id"])
        expect(page.locator('[data-detail-remove="5102"]')).to_have_count(0)
        expect(page.locator('[data-detail-remove="배포"]')).to_be_visible()
        page.locator("#dTagInput").fill("5102")
        page.locator("#dTagAdd").click()
        expect(page.locator('[data-detail-remove="5102"]')).to_be_visible()
        page.locator("#dSave").click()
        expect(page.locator("#detail")).not_to_be_visible()
        assert "5102" in store.facets_of(reference["id"])["tag"]
        page.evaluate("id => openMemo(id)", reference["id"])
        page.locator('[data-detail-remove="5102"]').click()
        page.locator("#dSave").click()
        expect(page.locator("#detail")).not_to_be_visible()
        saved = store.get_memo(reference["id"])
        assert saved["body"] == reference["body"] and "5102" not in saved["facets"]["tag"]
        print("OK imported issue numbers stay in the original text when explicitly tagging and untagging")
        check_sidebar_tag_groups(page)
        page.locator('[data-tab="settings"]').click()
        page.locator('[data-prov="ollama"]').click()
        page.locator("#saveLlm").click()
        expect(page.locator("#view")).to_contain_text("Ollama (로컬) 사용 중")
        page.locator("#reopenStarter").click()
        expect(tour).to_be_visible()
        page.keyboard.press("Escape")
        expect(tour).to_be_hidden()
        expect(page.locator("#chooseDocument")).to_be_focused()
        page.locator('[data-tab="feed"]').click()
        page.locator("#capture").fill("튜토리얼 전 초안")
        page.locator("#capture").press("End")
        page.locator('[data-tab="settings"]').click()
        page.locator("#reopenStarter").click()
        page.locator("#tourNext").click()
        page.locator("#tourNext").click()
        page.get_by_role("button", name="마치기", exact=True).click()
        expect(tour).to_be_hidden()
        expect(page.locator('[data-tab="feed"]')).to_have_attribute("aria-current", "page")
        expect(page.locator("#capture")).to_be_in_viewport()
        expect(page.locator("#capture")).to_be_focused()
        page.keyboard.insert_text(" 이어쓰기")
        expect(page.locator("#capture")).to_have_value("튜토리얼 전 초안 이어쓰기")
        page.locator('[data-tab="settings"]').click()
        page.locator("#reopenStarter").click()
        expect(page.locator("#chooseDocument")).to_be_focused()
        with page.expect_file_chooser() as file_chooser:
            page.locator("#chooseDocument").click()
        file_chooser.value.set_files({"name": "tour.txt", "mimeType": "text/plain", "buffer": b"preview the selected file"})
        expect(page.locator("#documentPreview")).to_contain_text("tour.txt")
        expect(tour).to_be_visible()
        expect(page.locator("[data-document-part]").first).to_be_focused()
        page.locator("#documentSave").click()
        expect(page.locator("#documentResult")).to_contain_text("메모를 저장했습니다")
        page.locator("#tourNext").click()
        expect(page.locator("#newTag")).to_be_focused()
        page.keyboard.press("Escape")
        print("OK tutorial navigates, focuses and operates the real import, tag and AI settings controls")
        assert not errors, errors
        print("OK narrow layout; no uncaught browser errors")
        browser.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
