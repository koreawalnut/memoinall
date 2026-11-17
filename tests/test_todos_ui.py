"""Use the task board through the existing menu; all data is temporary."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-todos-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from memoinall import db, store, todos
from memoinall.api import app


async def no_prepare():
    pass


def main():
    errors = []
    fail_patch = False
    with patch.object(store, "start_worker"), patch("memoinall.api._prepare_search_later", new=no_prepare), TestClient(app) as client, sync_playwright() as pw:
        seeds = ["오늘까지 교육계획안 보내야 함", "금요일까지 보고서 보내야 함", "견적 오면 비교해서 업체 정해야 함",
                 "로그 확인 필요함", "김대리가 금요일까지 견적 보내야 함", "11월 15일까지 회의록 정리해야 함"]
        memos = [store.add_memo(text, created_at="2026-11-16T09:00:00", enqueue_enrich=False) for text in seeds]
        todos.refresh_pending()
        ids = [db.one("SELECT id FROM todos WHERE memo_id=?", (memo["id"],))["id"] for memo in memos]
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 1000}, timezone_id="Asia/Seoul")
        page = context.new_page()
        page.clock.set_fixed_time("2026-11-16T09:00:00+09:00")
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            if fail_patch and request.method == "PATCH" and path.startswith("/api/todos/"):
                route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "저장 오류 테스트"}))
                return
            response = client.request(request.method, path, content=request.post_data_buffer)
            headers = {key: value for key, value in response.headers.items() if key not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("http://127.0.0.1:8787/**", serve)
        page.goto("http://127.0.0.1:8787/")
        expect(page.locator("#capture")).to_be_enabled()
        expect(page.locator("#tabs > button")).to_have_text(["내 메모", "일정", "AI로 정리", "가져오기·공유"])
        expect(page.locator('[data-tab="todos"]')).to_be_visible()
        page.locator('[data-tab="todos"]').click()
        expect(page.locator('[data-todo-layout="calendar"]')).to_have_attribute("aria-pressed", "true")
        page.locator('[data-todo-layout="list"]').click()
        expect(page.locator(".todo-overview")).to_contain_text("오늘 챙길 일 2건")
        expect(page.locator(".todo")).to_have_count(2)
        expect(page.locator(".todo-overdue")).to_contain_text("기한 지남")
        print("OK schedule is a primary menu; today and overdue work shown")

        def view(key):
            page.locator(f'[data-todo-view="{key}"]').click()
            expect(page.locator(f'[data-todo-view="{key}"]')).to_have_attribute("aria-pressed", "true")

        def card(index):
            return page.locator(f'[data-task-id="{ids[index]}"]')

        card(0).get_by_role("button", name="내일 다시 보기", exact=True).click()
        expect(card(0)).to_be_visible()
        expect(card(0)).to_contain_text("오늘 마감")
        expect(card(0)).to_contain_text("다시 보기 11/17")
        assert db.one("SELECT due FROM todos WHERE id=?", (ids[0],))["due"] == "2026-11-16"
        page.locator("#todoUndo").click()
        expect(card(0).locator(".badges")).not_to_contain_text("다시 보기")
        print("OK snooze preserves deadline; undo restores automatic values")

        card(0).get_by_role("checkbox").check()
        expect(card(0)).to_have_count(0)
        view("done")
        expect(card(0)).to_be_visible()
        card(0).get_by_role("button", name="다시 열기").click()
        view("today")
        expect(card(0)).to_be_visible()
        print("OK completion history and reopening")

        view("upcoming")
        card(1).get_by_role("button", name="수정·날짜").click()
        page.locator("#todoReview").fill("2026-11-16")
        page.locator("#todoEditor").get_by_role("button", name="저장", exact=True).click()
        expect(page.locator("#todoEditor")).not_to_be_visible()
        view("today")
        expect(card(1)).to_be_visible()
        expect(card(1)).to_contain_text("마감 11/20")
        print("OK separate followup date brings future deadline into today")

        view("waiting")
        expect(card(2)).to_contain_text("견적 오면 진행")
        card(2).get_by_role("button", name="오늘 보기", exact=True).click()
        view("today")
        expect(card(2)).to_contain_text("기다리는 중")
        print("OK waiting task can be scheduled for followup")

        view("unscheduled")
        card(3).get_by_role("button", name="할 일 아님", exact=True).click()
        store.enrich(memos[3]["id"])
        view("dismissed")
        expect(card(3)).to_be_visible()
        card(3).get_by_role("button", name="다시 열기").click()
        view("unscheduled")
        expect(card(3)).to_be_visible()
        print("OK exclusion survives reindex and can be restored")

        view("review")
        card(4).get_by_role("button", name="내 할 일로").click()
        view("upcoming")
        expect(card(4)).to_be_visible()
        card(4).locator("summary").click()
        expect(card(4).locator("blockquote")).to_contain_text("김대리가")
        card(4).get_by_role("button", name="원문 열기", exact=False).click()
        expect(page.locator("#detail")).to_be_visible()
        page.locator("#dClose").click()
        print("OK review acceptance and original evidence navigation")

        fail_patch = True
        card(4).get_by_role("checkbox").check()
        expect(page.locator("#todoError")).to_contain_text("저장 오류 테스트")
        expect(card(4).get_by_role("checkbox")).not_to_be_checked()
        card(4).get_by_role("button", name="수정·날짜").click()
        page.locator("#todoTitle").fill("실패해도 보존할 수정 내용")
        page.locator("#todoEditor").get_by_role("button", name="저장", exact=True).click()
        expect(page.locator("#todoEditError")).to_contain_text("저장하지 못했습니다")
        expect(page.locator("#todoTitle")).to_have_value("실패해도 보존할 수정 내용")
        fail_patch = False
        page.locator("#todoEditor").get_by_role("button", name="저장", exact=True).click()
        expect(page.locator("#todoEditor")).not_to_be_visible()
        expect(card(4)).to_contain_text("실패해도 보존할 수정 내용")
        print("OK failed checkbox and editor save preserve user state")

        view("today")
        expect(page.locator("#toast")).not_to_have_class("toast show")
        artifacts = Path(__file__).resolve().parents[1] / "artifacts" / "ux-review"
        if "--screenshots" in sys.argv:
            artifacts.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifacts / "todos-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        card(0).get_by_role("button", name="수정·날짜").click()
        expect(page.locator("#todoEditor")).to_be_visible()
        assert page.locator("#todoEditor").evaluate("el => el.scrollWidth <= el.clientWidth")
        page.locator("#todoCancel").click()
        if "--screenshots" in sys.argv:
            page.screenshot(path=str(artifacts / "todos-mobile.png"), full_page=True)
        page.clock.set_fixed_time("2026-11-17T09:00:00+09:00")
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(page.locator(".todo-overview")).to_contain_text("11/17")
        print("OK returning next day refreshes the board using the local date")
        assert not errors, errors
        print("OK narrow layout; no uncaught browser errors")
        browser.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close()
        TMP.cleanup()
