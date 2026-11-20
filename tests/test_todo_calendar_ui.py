"""Month navigation, date agenda and edits through the real calendar UI."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-calendar-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from memoinall import calendar_import, db, store, todos
from memoinall.api import app


async def no_prepare():
    pass


def main():
    errors = []
    with patch.object(store, "start_worker"), patch("memoinall.api._prepare_search_later", new=no_prepare), TestClient(app) as client, sync_playwright() as pw:
        seeds = ["금요일까지 보고서 보내야 함", "오늘까지 교육계획안 보내야 함", "다음 주쯤 자료 검토해야 함",
                 "예산 확인 필요함", "견적 요청함. 목요일까지 받기로 함", "11월 15일까지 회의록 정리해야 함",
                 "내일 안건 작성하기", "금요일까지 계약서 검토해야 함", "금요일까지 교육 자료 보내야 함"]
        memos = [store.add_memo(body, created_at="2026-11-16T09:00:00", enqueue_enrich=False) for body in seeds]
        todos.refresh_pending()
        ids = [db.one("SELECT id FROM todos WHERE memo_id=?", (memo["id"],))["id"] for memo in memos]
        todos.update(ids[0], {"review_on": "2026-11-18"})
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000}, timezone_id="Asia/Seoul")
        page.clock.set_fixed_time("2026-11-16T09:00:00+09:00")
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            sent = {"content-type": request.headers["content-type"]} if "content-type" in request.headers else {}
            response = client.request(request.method, path, content=request.post_data_buffer, headers=sent)
            headers = {key: value for key, value in response.headers.items() if key not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("http://127.0.0.1:8787/**", serve)
        page.goto("http://127.0.0.1:8787/")
        expect(page.locator("#capture")).to_be_enabled()
        page.locator('[data-tab="todos"]').click()
        expect(page.locator("#todoCalendarMonth")).to_have_text("2026년 11월")
        expect(page.locator('[data-todo-layout]').first).to_have_attribute("data-todo-layout", "calendar")
        expect(page.locator('[data-todo-layout="calendar"]')).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#todoCalendarDayTitle")).to_contain_text("11/16(월) · 오늘")
        expect(page.locator('[data-calendar-day="2026-11-16"]')).to_have_attribute("aria-current", "date")
        expect(page.locator('[data-calendar-day="2026-11-20"] .todo-calendar-more')).to_have_text("+1건")
        expect(page.locator("#todoAiRun")).to_be_visible()
        month_box = page.locator(".todo-calendar-month-panel").bounding_box()
        agenda_box = page.locator(".todo-calendar-agenda").bounding_box()
        assert agenda_box["x"] >= month_box["x"] + month_box["width"]
        assert abs(agenda_box["y"] - month_box["y"]) < 2
        page.locator('[data-todo-layout="list"]').click()
        page.locator('[data-tab="feed"]').click()
        page.locator('[data-tab="todos"]').click()
        expect(page.locator('[data-todo-layout="calendar"]')).to_have_attribute("aria-pressed", "true")
        print("OK calendar opens first on entry/reentry; desktop date agenda sits beside month")

        def select(day):
            page.locator(f'[data-calendar-day="{day}"]').click()
            expect(page.locator(f'[data-calendar-day="{day}"]')).to_have_attribute("aria-pressed", "true")
            expect(page.locator(f'[data-calendar-day="{day}"]')).to_be_focused()
            expect(page.locator("#todoCreate")).not_to_be_visible()

        select("2026-11-16")
        expect(page.locator("#calendarMemoTitle")).to_contain_text("9건")
        page.locator("#calendarMemoNext").click()
        expect(page.locator(".calendar-day-memos [data-todo-memo]")).to_have_count(4)
        page.locator(f'.calendar-day-memos [data-todo-memo="{memos[0]["id"]}"]').click()
        expect(page.locator("#dBody")).to_have_value(seeds[0])
        page.locator("#dClose").click()
        expect(page.locator("#todoCalendarDayTitle")).to_contain_text("11/16")
        print("OK the writing day opens the original memo across pages and preserves the selected date")

        page.evaluate("window.scrollTo(0, 450)")
        before_scroll = page.evaluate("window.scrollY")
        select("2026-11-18")
        report = page.locator(f'[data-task-id="{ids[0]}"]')
        expect(report).to_be_visible()
        expect(page.locator(".todo-calendar-agenda-kinds")).to_have_text("다시 보기")
        expect(report).to_contain_text("마감 11/20")
        expect(report).to_contain_text("다시 보기 11/18")
        assert abs(page.evaluate("window.scrollY") - before_scroll) < 4
        page.locator('[data-calendar-day="2026-11-18"]').focus()
        page.keyboard.press("ArrowRight")
        expect(page.locator('[data-calendar-day="2026-11-19"]')).to_be_focused()
        expect(page.locator(".todo-calendar-agenda")).to_contain_text("견적 회신 확인")
        print("OK followup versus deadline labels; arrow-key date selection")

        page.locator("#todoMonthPrev").click()
        expect(page.locator("#todoCalendarMonth")).to_have_text("2026년 10월")
        page.locator("#todoMonthNext").click()
        expect(page.locator("#todoCalendarMonth")).to_have_text("2026년 11월")
        page.locator("#todoMonthNext").click()
        expect(page.locator("#todoCalendarMonth")).to_have_text("2026년 12월")
        page.locator("#todoMonthNext").click()
        expect(page.locator("#todoCalendarMonth")).to_have_text("2027년 1월")
        page.locator("#todoCalendarToday").click()
        expect(page.locator("#todoCalendarMonth")).to_have_text("2026년 11월")
        expect(page.locator("#todoCalendarDayTitle")).to_contain_text("오늘")
        print("OK month and year transitions; return to today")

        select("2026-11-20")
        expect(page.locator(".todo-calendar-agenda .todo")).to_have_count(3)
        report.get_by_role("checkbox").check()
        expect(page.locator("#todoCalendarDayTitle")).to_contain_text("2건")
        expect(page.locator('[data-calendar-day="2026-11-20"]')).to_have_attribute("aria-label", "2026-11-20, 마감 2건, 작성한 메모 0건, Enter로 일정 추가")
        page.locator("#todoUndo").click()
        expect(page.locator("#todoCalendarDayTitle")).to_contain_text("3건")
        report.get_by_role("button", name="수정·날짜").click()
        page.locator("#todoDue").fill("2026-11-21")
        page.locator("#todoEditor").get_by_role("button", name="저장", exact=True).click()
        expect(page.locator("#todoEditor")).not_to_be_visible()
        expect(page.locator("#todoCalendarDayTitle")).to_contain_text("2건")
        select("2026-11-21")
        expect(report).to_be_visible()
        expect(report).to_contain_text("마감 11/21")
        assert db.one("SELECT review_on FROM todos WHERE id=?", (ids[0],))["review_on"] == "2026-11-18"
        print("OK complete, undo and reschedule update calendar immediately; followup stays separate")

        select("2026-11-25")
        expect(page.locator(".todo-calendar-agenda-kinds")).to_have_text("기간 중")
        expect(page.locator(".todo-calendar-agenda")).to_contain_text("11/23(월)~11/29(일) 중")
        page.locator('.todo-calendar-notices [data-todo-view="unscheduled"]').click()
        expect(page.locator('[data-todo-layout="list"]')).to_have_attribute("aria-pressed", "true")
        expect(page.locator(".todo-group-title")).to_contain_text("일정 미정")
        expect(page.locator(f'[data-task-id="{ids[3]}"]')).to_be_visible()
        page.locator('[data-todo-layout="calendar"]').click()
        expect(page.locator("#todoCalendarMonth")).to_be_visible()
        page.locator('.todo-calendar-notices [data-todo-view="today"]').click()
        expect(page.locator(f'[data-task-id="{ids[5]}"]')).to_contain_text("기한 지남")
        print("OK week ranges stay approximate; undated and overdue items remain reachable")

        page.locator('[data-todo-layout="calendar"]').click()
        select("2026-11-18")
        artifacts = Path(__file__).resolve().parents[1] / "artifacts" / "ux-review"
        if "--screenshots" in sys.argv:
            artifacts.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifacts / "todo-calendar-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator('[data-calendar-day="2026-11-20"] .todo-calendar-dots')).to_contain_text("마감 2")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        select("2026-11-19")
        expect(page.locator(".todo-calendar-agenda")).to_contain_text("견적 회신 확인")
        if "--screenshots" in sys.argv:
            page.screenshot(path=str(artifacts / "todo-calendar-mobile.png"), full_page=True)
        page.locator("#todoCalendarToday").click()
        expect(page.locator("#todoCalendarDayTitle")).to_contain_text("11/16")
        page.clock.set_fixed_time("2026-11-17T09:00:00+09:00")
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(page.locator("#todoCalendarDayTitle")).to_contain_text("11/17(화) · 오늘")
        print("OK narrow calendar, tap selection and next-day refresh")

        # Five event types on a narrow date cell must not spill into the next week.
        store.add_memo("11월 26일까지 결산 보고서 보내야 함", created_at="2026-11-16T09:00:00", enqueue_enrich=False)
        store.add_memo("11월 26일 행사 준비하기", created_at="2026-11-16T09:00:00", enqueue_enrich=False)
        todos.update(ids[0], {"review_on": "2026-11-26"})
        plan = calendar_import.preview({"name": "공유 일정", "kind": "file", "range_start": "2026-11-01", "range_end": "2026-11-30",
            "content": "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:design-density\nSUMMARY:팀 회의\nDTSTART;TZID=Asia/Seoul:20261126T140000\nDTEND;TZID=Asia/Seoul:20261126T150000\nEND:VEVENT\nEND:VCALENDAR"})
        calendar_import.apply(plan["plan_id"])
        select("2026-11-26")
        dense = page.locator('[data-calendar-day="2026-11-26"]')
        expect(dense.locator(".todo-calendar-dots-more")).to_have_text("+3건")
        expect(page.locator("#todoCalendarDayTitle")).to_contain_text("5건")
        assert dense.evaluate("el => el.scrollHeight <= el.clientHeight + 1")
        expect(page.locator(".external-calendar-card")).to_contain_text("14:00–15:00")
        for width in (320, 768, 1190, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width
        page.emulate_media(color_scheme="dark", reduced_motion="reduce")
        expect(page.locator('[data-calendar-day="2026-11-26"]')).to_have_attribute("aria-pressed", "true")
        assert page.locator(".todo-calendar-month-panel").evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(22, 26, 33)"
        if "--screenshots" in sys.argv:
            page.screenshot(path=str(artifacts / "todo-calendar-dark.png"), full_page=True)
        assert not errors, errors
        print("OK dense mobile cells, external time labels, 320–1440px widths and dark theme; no browser errors")
        browser.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close()
        TMP.cleanup()
