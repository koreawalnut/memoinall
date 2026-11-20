"""AI button, preview, selective apply and errors with a fake provider."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-todo-ai-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
    os.environ.pop(key, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from memoinall import db, providers, settings, store, todos
from memoinall.api import app


async def no_prepare():
    pass


def main():
    errors, held = [], []
    provider = MagicMock()
    hold = False
    calls = 0
    with patch.object(store, "start_worker"), patch("memoinall.api._prepare_search_later", new=no_prepare), \
         patch.object(providers, "build", return_value=provider), TestClient(app) as client, sync_playwright() as pw:
        settings.set_many({"llm.provider": "openai", "llm.openai.api_key": ""})
        def add(body):
            return store.add_memo(body, created_at="2026-11-16T09:00:00", enqueue_enrich=False)
        missing = add("교육팀과 통화해서 금요일까지 설명회 자료를 보내기로 했다.")
        original = add("금요일까지 보고서 보내야 함")
        later = add("보고서 발송 완료.")
        todos.refresh_pending()
        tid = db.one("SELECT id FROM todos")["id"]
        todos.update(tid, {"title": "내 보고서 보내기"})
        provider.complete_json.return_value = {"suggestions": [
            {"todo_id": None, "memo_id": missing["id"], "title": "설명회 자료 보내기", "status": "open", "due": "2026-11-20", "review_on": None,
             "reason": "교육팀과 자료 발송을 약속했습니다.", "evidence": [{"memo_id": missing["id"], "quote": missing["body"]}]},
            {"todo_id": tid, "memo_id": original["id"], "title": "보고서 보내기", "status": "done", "due": "2026-11-20", "review_on": None,
             "reason": "후속 메모에 발송 완료가 기록되어 있습니다.", "evidence": [{"memo_id": original["id"], "quote": original["body"]}, {"memo_id": later["id"], "quote": later["body"]}]},
        ]}
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000}, timezone_id="Asia/Seoul")
        page.clock.set_fixed_time("2026-11-16T09:00:00+09:00")
        page.on("pageerror", lambda error: errors.append(str(error)))

        def respond(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            sent = {"content-type": request.headers["content-type"]} if "content-type" in request.headers else {}
            response = client.request(request.method, path, content=request.post_data_buffer, headers=sent)
            headers = {key: value for key, value in response.headers.items() if key not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        def serve(route):
            nonlocal calls
            if route.request.url.endswith("/api/todos/ai/preview"):
                calls += 1
                if hold:
                    held.append(route)
                    return
            respond(route)

        def open_todos():
            page.locator("#moreMenu > summary").click()
            page.locator('[data-tab="todos"]').click()
            expect(page.locator('[data-todo-layout="calendar"]')).to_have_attribute("aria-pressed", "true")
            page.locator('[data-todo-layout="list"]').click()
            expect(page.locator("#todoAiRun")).to_be_visible()

        page.route("http://127.0.0.1:8787/**", serve)
        page.goto("http://127.0.0.1:8787/")
        expect(page.locator("#capture")).to_be_enabled()
        open_todos()
        expect(page.locator("#todoAiRun")).to_be_disabled()
        expect(page.locator(".todo-ai-description")).to_contain_text("API 키")
        assert calls == 0 and provider.complete_json.call_count == 0
        page.locator("#todoAiSettings").click()
        expect(page.locator("#lead")).to_contain_text("설정")
        settings.set_many({"llm.openai.api_key": "fake-test-key", "llm.openai.model": "test-model"})
        open_todos()
        expect(page.locator("#todoAiRun")).to_be_enabled()
        expect(page.locator(".todo-ai-description")).to_contain_text("test-model")
        assert calls == 0
        print("OK key gating; existing settings link; no background AI calls")

        hold = True
        page.locator("#todoAiRun").click()
        expect(page.locator("#todoAiRun")).to_be_disabled()
        expect(page.locator(".todo-ai-panel")).to_contain_text("처리 중")
        page.evaluate("document.querySelector('#todoAiRun').click()")
        page.locator('[data-tab="feed"]').click()
        expect(page.locator("#capture")).to_be_visible()
        assert calls == 1 and len(held) == 1
        hold = False
        respond(held.pop())
        page.wait_for_function("!todoAi.busy")
        expect(page.locator("#capture")).to_be_visible()
        open_todos()
        expect(page.locator(".todo-ai-proposal")).to_have_count(2)
        expect(page.locator('[data-ai-select="0"]')).to_be_checked()
        expect(page.locator('[data-ai-select="1"]')).not_to_be_checked()
        expect(page.locator('[data-ai-proposal="1"]')).to_contain_text("직접 정한 할 일 유지")
        assert len(db.query("SELECT * FROM todos")) == 1
        print("OK single explicit request; delayed result survives navigation; preview is read-only")

        page.locator('[data-ai-proposal="1"] summary').click()
        expect(page.locator('[data-ai-proposal="1"]')).to_contain_text("보고서 발송 완료")
        page.locator('[data-ai-proposal="1"] [data-ai-memo]').last.click()
        expect(page.locator("#detail")).to_be_visible()
        page.locator("#dClose").click()
        page.locator("#todoAiApply").click()
        expect(page.locator("#todoAiUndo")).to_be_visible()
        assert len(db.query("SELECT * FROM todos")) == 2
        assert db.one("SELECT status FROM todos WHERE id=?", (tid,))["status"] == "open"
        page.locator("#todoAiUndo").click()
        expect(page.locator(".todo-ai-footer")).to_contain_text("적용을 취소")
        assert len(db.query("SELECT * FROM todos")) == 1
        print("OK original evidence; only selected suggestions applied; batch undo")

        page.locator("#todoAiRun").click()
        expect(page.locator("#todoAiApply")).to_be_enabled()
        page.locator('[data-ai-select="1"]').check()
        page.locator("#todoAiApply").click()
        expect(page.locator("#todoAiUndo")).to_be_visible()
        assert db.one("SELECT status FROM todos WHERE id=?", (tid,))["status"] == "done"
        assert db.one("SELECT title FROM todos WHERE id=?", (tid,))["title"] == "내 보고서 보내기"
        store.enrich(original["id"])
        store.enrich(missing["id"])
        page.locator('[data-todo-view="done"]').click()
        expect(page.locator(f'[data-task-id="{tid}"]')).to_contain_text("AI 정리")
        page.locator(f'[data-task-id="{tid}"] summary').click()
        expect(page.locator(f'[data-task-id="{tid}"]')).to_contain_text("보고서 발송 완료")
        page.locator("#todoAiUndo").click()
        expect(page.locator(".todo-ai-footer")).to_contain_text("적용을 취소")
        print("OK explicit completion selection; protected manual title; AI tasks survive reindex")

        page.locator("#todoAiRun").click()
        expect(page.locator("#todoAiApply")).to_be_enabled()
        page.locator('[data-ai-select="1"]').check()
        todos.update(tid, {"review_on": "2026-11-17"})
        page.locator("#todoAiApply").click()
        expect(page.locator("#todoAiError")).to_contain_text("할 일이 변경")
        assert len(db.query("SELECT * FROM todos")) == 1
        provider.complete_json.side_effect = providers.Unavailable("AI 서버 연결 실패 테스트")
        page.locator("#todoAiRun").click()
        expect(page.locator("#todoAiError")).to_contain_text("AI 서버 연결 실패")
        expect(page.locator(".todo-ai-proposal")).to_have_count(2)
        expect(page.locator("#todoAiRun")).to_be_enabled()
        print("OK stale apply and network failure preserve tasks and previous preview")

        provider.complete_json.side_effect = None
        page.locator("#todoAiRun").click()
        expect(page.locator("#todoAiApply")).to_be_enabled()
        expect(page.locator("#todoAiError")).to_have_count(0)
        page.locator('[data-todo-view="upcoming"]').click()
        expect(page.locator(".todo-group-title")).to_contain_text("다가오는 일")
        artifacts = Path(__file__).resolve().parents[1] / "artifacts" / "ux-review"
        if "--screenshots" in sys.argv:
            artifacts.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifacts / "todo-ai-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if "--screenshots" in sys.argv:
            page.screenshot(path=str(artifacts / "todo-ai-mobile.png"), full_page=True)
        assert not errors, errors
        expect(page.locator('[data-tab="generate"]')).to_have_text("메모로 문서 만들기")
        expect(page.locator("#todoAiRun")).to_have_text("할 일·날짜 제안받기")
        print("OK narrow preview; menu positions unchanged; no browser errors")
        browser.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close()
        TMP.cleanup()
