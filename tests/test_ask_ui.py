"""검색창에서 AI에게 묻기 — AI를 연결했을 때만 보이고, 답·근거·조건·실패를 보여 준다.  python tests/test_ask_ui.py"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-ask-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402
from memoinall import db, llm, settings, store  # noqa: E402
from memoinall.api import app  # noqa: E402
from memoinall.providers import ProviderError  # noqa: E402

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts" / "ux-review"


def main():
    errors, prompts = [], []
    with patch.object(store, "start_worker"), TestClient(app) as client, sync_playwright() as pw:
        pay = store.add_memo("결제 재시도는 멱등키를 붙일 때만 하기로 결정 #결제", enqueue_enrich=False)
        plan = store.add_memo("온보딩 화면을 3단계로 줄이기로 함 #기획", enqueue_enrich=False)
        answers = [f"멱등키를 붙였을 때만 재시도하기로 했습니다 [M{pay['id']}]. 다른 근거는 없습니다 [M999]."]

        def complete(system, prompt, **kw):
            prompts.append(prompt)
            if isinstance(answers[0], Exception):
                raise answers[0]
            return answers[0]

        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(locale="ko-KR", viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))

        def serve(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            sent = {"content-type": request.headers["content-type"]} if "content-type" in request.headers else {}
            response = client.request(request.method, path, content=request.post_data_buffer, headers=sent)
            headers = {k: v for k, v in response.headers.items() if k not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("http://127.0.0.1:8787/**", serve)
        client.put("/api/starter", json={"visible": False})
        page.goto("http://127.0.0.1:8787/")
        button, panel, q = page.locator("#askAi"), page.locator("#askPanel"), page.locator("#q")

        # ---- AI 미설정: 버튼이 없고, Ctrl+Enter 도 아무 일 없다
        expect(page.locator("#status")).to_contain_text("미설정")
        expect(button).to_be_hidden()
        q.fill("결제 재시도 어떻게 하기로 했지?")
        q.press("Control+Enter")
        expect(panel).to_be_hidden()
        expect(q).to_have_attribute("placeholder", "기억나는 단어나 내용으로 메모 찾기")
        print("OK without an AI connection the ask button is hidden and the shortcut does nothing")

        # ---- AI 연결: 다음 상태 갱신 때 나타난다
        ready = patch.object(settings, "provider_ready", return_value=True)
        ready.start()
        with patch.object(llm, "complete", side_effect=complete):
            page.evaluate("loadSidebar()")
            expect(button).to_be_visible()
            expect(button).to_be_enabled()
            expect(q).to_have_attribute("placeholder", "단어로 찾거나, 궁금한 것을 적고 ✨ AI에게 묻기")
            q.fill("")
            expect(button).to_be_disabled()                                     # 질문이 없으면 못 누른다
            q.fill("결제 재시도 어떻게 하기로 했지?")
            button.click()
            expect(panel.locator(".ask-answer")).to_contain_text("멱등키를 붙였을 때만")
            expect(panel).to_contain_text("결제 재시도 어떻게 하기로 했지?")
            expect(panel).to_contain_text("전체 메모에서")
            cite = panel.locator(f'.ask-answer a[data-ask-source="{pay["id"]}"]')
            expect(cite).to_have_text(f"[M{pay['id']}]")
            expect(panel.locator(".ask-answer a")).to_have_count(1)             # 근거에 없는 [M999] 는 링크가 아니다
            expect(panel.locator(f'.ask-foot .chip[data-ask-source="{pay["id"]}"]')).to_contain_text("결제 재시도는")
            expect(page.locator("#view .status").first).to_contain_text("뜻이 비슷한 메모까지")   # 아래엔 같은 질문의 검색 결과
            expect(page.locator(f'#view .card[data-id="{pay["id"]}"]')).to_be_visible()
            ARTIFACTS.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(ARTIFACTS / "ask-desktop.png"))
            cite.click()
            expect(page.locator("#detail")).to_be_visible()
            expect(page.locator("#dTitle")).to_contain_text("결제 재시도는")
            page.locator("#dClose").click()
            print("OK answer appears above the results with clickable citations and source chips")

            # ---- 검색 조건 안에서만: #기획 으로 좁히고 Ctrl+Enter
            page.locator('#sidebarAutoTags [data-v="기획"]').click()
            q.fill("무엇을 줄이기로 했지?")
            q.press("Control+Enter")
            expect(panel).to_contain_text("조건: #기획")
            expect(panel.locator(".ask-foot .chip")).to_have_count(1)
            expect(panel.locator(".ask-foot .chip")).to_have_attribute("data-ask-source", str(plan["id"]))
            assert "온보딩" in prompts[-1] and "멱등키" not in prompts[-1], prompts[-1]

            # ---- AI 호출이 실패하면 이유를 보여 주고 다시 물을 수 있다
            answers[0] = ProviderError("요청 한도를 넘었습니다. 잠시 뒤 다시 시도하세요.")
            button.click()
            expect(panel.locator("[role=alert]")).to_contain_text("요청 한도를 넘었습니다")
            expect(button).to_be_enabled()
            page.locator("#askClose").click()
            expect(panel).to_be_hidden()
            answers[0] = "온보딩을 3단계로 줄이기로 했습니다."
            button.click()
            expect(panel.locator(".ask-answer")).to_contain_text("3단계")
            page.locator("#clear").click()                                      # 검색 초기화는 답도 닫는다
            expect(panel).to_be_hidden()
            expect(q).to_have_value("")
            print("OK questions follow the active filters; failures explain themselves; close and clear hide the answer")

            # ---- 좁은 화면
            q.fill("결제 재시도 어떻게 하기로 했지?")
            button.click()
            expect(panel.locator(".ask-answer")).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            expect(button).to_be_visible()
            page.screenshot(path=str(ARTIFACTS / "ask-narrow.png"), full_page=True)
            page.set_viewport_size({"width": 1280, "height": 900})
            page.locator("#askClose").click()
        ready.stop()

        # ---- 설정이 풀리면: 서버가 거절하고 버튼이 사라진다
        page.evaluate("state.llmReady = true; paintAskButton()")              # 화면이 아직 모르는 사이에 누른 경우
        button.click()
        expect(panel.locator("[role=alert]")).to_contain_text("AI가 연결되지 않았습니다")
        expect(button).to_be_hidden()
        expect(panel.get_by_role("link", name="설정에서 AI 연결하기")).to_be_visible()
        print("OK when the AI connection goes away the server refuses and the button disappears")

        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close()
