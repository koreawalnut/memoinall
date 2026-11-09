"""AI 응답을 기다리는 동안 진행 중임을 보여 주는지.  python tests/test_ai_wait_ui.py

AI 응답을 일부러 붙잡아 두고, 대기 표시·경과 시간·다른 화면 이동·완료 알림을 확인한다.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-ai-wait-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from memoinall import db, generate, llm, store  # noqa: E402
from memoinall.api import app  # noqa: E402


def main():
    errors, held = [], []
    with patch.object(store, "start_worker"), TestClient(app) as client, sync_playwright() as pw, \
         patch.object(llm, "available", return_value=True), \
         patch.object(llm, "complete", return_value="주간 업무 정리 결과입니다."), \
         patch.object(generate, "derive_queries", return_value={"queries": ["회의"], "method": "rules", "topic": "회의"}):
        store.add_memo("회의: 결제 점검 진행 #업무", enqueue_enrich=False)
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))

        def answer(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            sent = {"content-type": request.headers["content-type"]} if "content-type" in request.headers else {}
            response = client.request(request.method, path, content=request.post_data_buffer, headers=sent)
            headers = {k: v for k, v in response.headers.items() if k not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        def serve(route):
            if route.request.url.endswith("/api/generate") and route.request.method == "POST":
                held.append(route)  # AI 가 오래 걸리는 상황 — 테스트가 직접 풀어 준다.
                return
            answer(route)

        page.route("http://127.0.0.1:8787/**", serve)
        page.goto("http://127.0.0.1:8787/")
        page.locator('[data-tab="generate"]').click()
        page.locator("#genInput").fill("이번 주 업무를 정리해줘")
        page.locator("#genRun").click()

        wait = page.locator("#aiWait")
        expect(wait).to_be_visible()
        expect(wait.get_by_role("status")).to_have_text("AI로 정리하는 중")
        expect(wait).to_contain_text("관련 메모를 찾아 정리합니다")
        expect(page.locator("#genRun")).to_be_disabled()
        expect(page.locator("#aiWaitTime")).to_have_text("2초", timeout=4000)
        print("OK waiting panel shows what was sent and an elapsed-time counter")

        page.evaluate("aiWait.started -= 11000")
        expect(page.locator("#aiWaitHint")).to_contain_text("다른 화면으로 가도 계속 진행", timeout=3000)
        page.locator('[data-tab="feed"]').click()
        expect(wait).to_be_visible()
        print("OK long waits explain themselves and the panel stays visible on other screens")

        answer(held.pop())
        expect(wait).to_contain_text("AI 정리가 끝났습니다", timeout=10000)
        expect(wait.locator(".ai-spinner")).to_have_count(0)
        wait.get_by_role("button", name="결과 보기").click()
        expect(page.locator("#genOut")).to_contain_text("주간 업무 정리 결과입니다.")
        expect(wait).to_have_count(0)
        print("OK finishing elsewhere leaves a 'show result' notice that opens the result")

        page.locator("#genRun").click()
        expect(wait).to_be_visible()
        answer(held.pop())
        expect(page.locator("#genRun")).to_be_enabled(timeout=10000)
        expect(wait).to_have_count(0)
        print("OK finishing on the AI screen simply removes the panel")

        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#genRun").click()
        expect(wait).to_be_visible()
        box = wait.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 390, box
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        answer(held.pop())
        expect(page.locator("#genRun")).to_be_enabled(timeout=10000)
        print("OK panel fits a phone-width window")
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close()
