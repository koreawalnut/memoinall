"""Calendar creation, ICS import, URL refresh and disconnect in the browser."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-calendar-entry-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from memoinall import calendar_import, config, db, store
from memoinall.api import app


def calendar(title="팀 회의", day="20"):
    return f"BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:one\nSUMMARY:{title}\nDTSTART;TZID=Asia/Seoul:202611{day}T090000\nDTEND;TZID=Asia/Seoul:202611{day}T100000\nLOCATION:2층 회의실\nEND:VEVENT\nEND:VCALENDAR".encode()


async def no_prepare():
    pass


def main():
    assert config.DB_PATH.resolve() == (Path(TMP.name) / "memo.db").resolve()
    errors, requests = [], []
    fail = {"create": False, "refresh": False}
    with patch.object(store, "start_worker"), patch.object(store, "enqueue"), patch("memoinall.api._prepare_search_later", new=no_prepare), patch.object(calendar_import, "fetch_calendar", return_value=calendar()) as fetch, TestClient(app) as client, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000}, timezone_id="Asia/Seoul")
        page.clock.set_fixed_time("2026-11-16T09:00:00+09:00")
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            requests.append((request.method, path))
            if fail["create"] and path == "/api/todos" and request.method == "POST":
                if fail["create"] == "after_commit":
                    assert client.post(path, json=request.post_data_json).is_success
                fail["create"] = False
                route.fulfill(status=503, content_type="application/json", body='{"detail":"일시적인 저장 오류"}')
                return
            if fail["refresh"] and path.startswith("/api/calendars/") and path.endswith("/preview"):
                fail["refresh"] = False
                route.fulfill(status=503, content_type="application/json", body='{"detail":"연결 실패"}')
                return
            response = client.request(request.method, path, content=request.post_data_buffer)
            headers = {key: value for key, value in response.headers.items() if key not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("http://127.0.0.1:8787/**", serve)
        page.goto("http://127.0.0.1:8787/")
        expect(page.locator("#capture")).to_be_enabled()
        page.locator("#moreMenu > summary").click()
        page.locator('[data-tab="todos"]').click()
        expect(page.locator('[data-todo-layout="calendar"]')).to_have_attribute("aria-pressed", "true")
        page.locator('[data-calendar-day="2026-11-20"]').click()
        page.locator("#todoCalendarDayAdd").click()
        expect(page.locator("#todoCreateDate")).to_have_value("2026-11-20")
        expect(page.locator("#todoCreateTitle")).to_be_focused()
        expect(page.locator("#todoCreateTags")).to_contain_text("설정 → 내 태그")
        page.locator("#todoCreateTitle").fill("교육계획안 보내기")
        page.locator("#todoCreateDue").fill("2026-11-21")
        fail["create"] = True
        page.locator("#todoCreateSave").click()
        expect(page.locator("#todoCreateError")).to_contain_text("일시적인 저장 오류")
        expect(page.locator("#todoCreateTitle")).to_have_value("교육계획안 보내기")
        page.locator("#todoCreateSave").click()
        expect(page.locator("#todoCreate")).not_to_be_visible()
        expect(page.locator(".todo-calendar-agenda .todo-title")).to_have_text("교육계획안 보내기")
        expect(page.locator(".todo-calendar-agenda")).to_contain_text("직접 추가")
        assert db.one("SELECT count(*) n FROM todos")["n"] == 1
        print("OK direct creation uses selected day, retains failed input, then saves once")

        page.locator('[data-todo-action="edit"]').click()
        page.locator("#todoPlanned").fill("2026-11-19")
        page.locator("#todoEditor").get_by_role("button", name="저장", exact=True).click()
        expect(page.locator("#todoEditor")).not_to_be_visible()
        expect(page.locator("#todoCalendarDayTitle")).to_contain_text("0건")
        page.locator('[data-calendar-day="2026-11-19"]').click()
        expect(page.locator(".todo-calendar-agenda")).to_contain_text("교육계획안 보내기")
        print("OK direct task can be rescheduled through existing editor")

        page.locator("#todoCalendarImport").click()
        expect(page.locator("#calendarUrlField")).not_to_be_visible()
        page.locator("#calendarFile").set_input_files({"name": "네이버.ics", "mimeType": "text/calendar", "buffer": calendar()})
        expect(page.locator("#calendarName")).to_have_value("네이버")
        page.locator("#calendarPreviewRun").click()
        expect(page.locator("#calendarPreview")).to_contain_text("추가 1")
        assert db.one("SELECT count(*) n FROM calendar_events")["n"] == 0
        page.locator("#calendarApply").click()
        expect(page.locator("#calendarImportStatus")).to_contain_text("1개 일정을 달력에 반영")
        expect(page.locator("#calendarSources")).to_contain_text("네이버")
        page.locator("#calendarImportClose").click()
        page.locator('[data-calendar-day="2026-11-20"]').click()
        expect(page.locator(".external-calendar-card")).to_contain_text("팀 회의")
        expect(page.locator(".external-calendar-card")).to_contain_text("09:00–10:00")
        expect(page.locator(".external-calendar-card input[type=checkbox]")).to_have_count(0)
        print("OK file preview writes only on apply; external event and time are distinct from tasks")

        page.locator("#todoCalendarImport").click()
        page.get_by_role("button", name="새 파일로 갱신").click()
        page.locator("#calendarFile").set_input_files({"name": "수정.ics", "mimeType": "text/calendar", "buffer": calendar("변경된 팀 회의", "21")})
        expect(page.locator("#calendarName")).to_have_value("네이버")
        page.locator("#calendarPreviewRun").click()
        expect(page.locator("#calendarPreview")).to_contain_text("변경 1")
        expect(page.locator("#calendarPreview")).to_contain_text("추가 0")
        page.locator("#calendarApply").click()
        expect(page.locator("#calendarImportStatus")).to_contain_text("반영했습니다")
        assert db.one("SELECT count(*) n FROM calendar_events")["n"] == 1
        print("OK importing revised file updates existing UID without duplicates")

        page.locator('[name="calendarKind"][value="url"]').check()
        expect(page.locator("#calendarFileField")).not_to_be_visible()
        page.locator("#calendarName").fill("구글 업무")
        page.locator("#calendarUrl").fill("https://calendar.google.com/private-TOKEN/basic.ics")
        page.locator("#calendarPreviewRun").click()
        expect(page.locator("#calendarPreview")).to_contain_text("구글 업무")
        page.locator("#calendarApply").click()
        expect(page.locator("#calendarSources")).to_contain_text("구글 업무")
        expect(page.locator("#calendarUrl")).to_have_value("")
        fetch.return_value = calendar("주간 회의", "22")
        fail["refresh"] = True
        page.get_by_role("button", name="새로고침", exact=True).click()
        expect(page.locator("#calendarImportError")).to_contain_text("연결 실패")
        assert db.one("SELECT count(*) n FROM calendar_events")["n"] == 2
        page.get_by_role("button", name="새로고침", exact=True).click()
        expect(page.locator("#calendarPreview")).to_contain_text("변경 1")
        expect(page.locator("#calendarPreview")).to_contain_text("주간 회의")
        page.locator("#calendarApply").click()
        expect(page.locator("#calendarImportStatus")).to_contain_text("반영했습니다")
        assert fetch.call_count == 2
        print("OK URL registration and explicit refresh; connection failure preserves saved events")

        artifacts = Path("artifacts/ux-review")
        artifacts.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifacts / "calendar-import-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.locator("#calendarImport").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        assert page.locator("#calendarImport .dlg-b").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        page.screenshot(path=str(artifacts / "calendar-import-mobile.png"), full_page=True)
        page.locator("#calendarImportClose").click()
        page.locator("#todoCalendarAdd").click()
        expect(page.locator("#todoCreateDate")).to_have_value("2026-11-20")
        page.locator("#todoCreateTitle").fill("회의 자료 정리")
        assert page.locator("#todoCreate .dlg-b").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        page.screenshot(path=str(artifacts / "calendar-create-mobile.png"), full_page=True)
        page.locator("#todoCreateCancel").click()
        print("OK mobile creation and import dialogs fit without horizontal overflow")

        page.locator("#todoCalendarImport").click()
        page.once("dialog", lambda d: d.accept())
        page.locator(".calendar-source").filter(has_text="구글 업무").get_by_role("button", name="연결 해제").click()
        expect(page.locator("#calendarSources")).not_to_contain_text("구글 업무")
        assert db.one("SELECT count(*) n FROM todos")["n"] == 1
        assert db.one("SELECT count(*) n FROM calendar_events")["n"] == 1
        page.locator("#calendarImportClose").click()
        page.locator('[data-calendar-day="2026-11-21"]').click()
        expect(page.locator(".todo-calendar-agenda .todo-title")).to_have_count(2)
        page.set_viewport_size({"width": 1280, "height": 1000})
        page.screenshot(path=str(artifacts / "calendar-entry-desktop.png"), full_page=True)
        assert not errors, errors
        assert not any("/ai/" in path for _, path in requests)
        print("OK disconnect removes only that source; native task remains and AI never auto-runs")

        client.post("/api/tags", json={"name": "회의"})
        page.reload()
        capture = page.locator("#capture")
        register = page.get_by_role("button", name="일정으로 등록", exact=True)
        expect(capture).to_be_enabled()
        expect(register).to_be_disabled()
        body = "분기 회의 #회의\n- [ ] 내일까지 자료 보내기\n" + "회의 자료와 참석자 확인\n" * 500
        capture.fill(body)
        register.click()
        expect(page.locator("#todoCreateTitle")).to_have_value("분기 회의 #회의")
        expect(page.locator("#todoCreateDate")).to_be_focused()
        expect(page.locator('#todoCreateTags [data-tag="회의"]')).to_have_attribute("aria-pressed", "true")
        page.keyboard.press("Escape")
        expect(capture).to_have_value(body)
        register.click()
        page.locator("#todoCreateDate").fill("2026-12-01")
        page.locator("#todoCreateTitle").fill("분기 회의")
        fail["create"] = "after_commit"
        page.locator("#todoCreateSave").click()
        expect(page.locator("#todoCreateError")).to_contain_text("일시적인 저장 오류")
        expect(capture).to_have_value(body)
        page.locator("#todoCreateSave").click()
        expect(capture).to_have_value("")
        expect(register).to_be_disabled()
        assert client.get("/api/draft").json()["body"] == ""
        assert db.one("SELECT count(*) n FROM memos WHERE body=?", (body,))["n"] == 1
        db.execute("UPDATE memos SET todo_version=0 WHERE body=?", (body,))
        page.locator('[data-tab="todos"]').click()
        expect(page.locator("#todoCalendarDayTitle")).to_contain_text("12/1")
        expect(page.locator(".todo-calendar-agenda .todo-title")).to_have_text("분기 회의")
        expect(page.locator('.todo-calendar-agenda [data-schedule-filter="회의"]')).to_be_visible()
        assert db.one("SELECT count(*) n FROM todos JOIN memos ON memos.id=todos.memo_id WHERE memos.body=?", (body,))["n"] == 1
        assert not errors, errors
        print("OK capture schedules the full memo once on the chosen day; cancel and retry keep the draft")
        browser.close()
    db.close()
    TMP.cleanup()


if __name__ == "__main__":
    main()
