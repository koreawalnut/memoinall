"""Primary schedule navigation, tag edits, and file exchange through the UI."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-share-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from icalendar import Calendar
from playwright.sync_api import expect, sync_playwright
from memoinall import config, db, store
from memoinall.api import app


async def no_prepare():
    pass


def main():
    if config.DB_PATH.resolve() != (Path(TMP.name) / "memo.db").resolve():
        raise RuntimeError("공유 화면 테스트는 전용 임시 DB에서만 실행할 수 있습니다.")
    errors, fail = [], {"tags": False, "share": False}
    with patch.object(store, "start_worker"), patch.object(store, "enqueue"), patch("memoinall.api._prepare_search_later", new=no_prepare), TestClient(app) as client, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1050}, timezone_id="Asia/Seoul")
        page.clock.set_fixed_time("2026-11-16T09:00:00+09:00")
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            which = "tags" if path.endswith("/tags") and request.method == "PATCH" else "share" if path.endswith("/export") else None
            if which and fail[which]:
                fail[which] = False
                route.fulfill(status=503, content_type="application/json", body='{"detail":"일시적인 저장 오류"}')
                return
            # Set BUNDLE_STATIC to exercise precisely the packaged browser assets.
            bundle = os.environ.get("BUNDLE_STATIC")
            static_path = Path(bundle) / ("index.html" if path == "/" else path.removeprefix("/static/")) if bundle and (path == "/" or path.startswith("/static/")) else None
            if static_path and static_path.is_file():
                types = {".html": "text/html", ".js": "application/javascript", ".css": "text/css"}
                route.fulfill(status=200, content_type=types.get(static_path.suffix, "application/octet-stream"), body=static_path.read_bytes())
                return
            sent = {"content-type": request.headers["content-type"]} if "content-type" in request.headers else {}
            response = client.request(request.method, path, content=request.post_data_buffer, headers=sent)
            headers = {key: value for key, value in response.headers.items() if key not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("http://127.0.0.1:8787/**", serve)
        page.goto("http://127.0.0.1:8787/")
        expect(page.locator("#capture")).to_be_enabled()
        page.locator('[data-tab="settings"]').click()
        for name in ("업무", "팀", "개인", "공유팀", "외부팀"):
            page.locator("#newTag").fill(name)
            page.locator("#newTagColor").select_option("teal")
            page.locator("#addTag").click()
            expect(page.locator(f'[data-trename="{name}"]')).to_be_visible()
        expect(page.locator('#tabs > [data-tab="todos"]')).to_have_text("일정")
        page.locator('[data-tab="todos"]').click()
        expect(page.locator("#lead h2")).to_contain_text("일정")
        expect(page.locator('[data-todo-layout="calendar"]')).to_have_attribute("aria-pressed", "true")
        page.locator('[data-todo-layout="list"]').click()
        page.locator('[data-tab="feed"]').click()
        page.locator('[data-tab="todos"]').click()
        expect(page.locator('[data-todo-layout="calendar"]')).to_have_attribute("aria-pressed", "true")
        print("OK schedule opens directly from primary menu and returns to calendar")

        def create(title, *tags):
            page.locator("#todoCalendarAdd").click()
            page.locator("#todoCreateTitle").fill(title)
            for tag in tags:
                page.locator(f'#todoCreateTags .t-teal[data-tag="{tag}"]').click()
            page.locator("#todoCreateSave").click()
            expect(page.locator("#todoCreate")).not_to_be_visible()
            expect(page.locator(".todo-calendar-agenda")).to_contain_text(title)

        create("팀 교육계획안 보내기", "업무", "팀")
        create("개인 서류 정리", "개인")
        page.locator('.todo [data-schedule-filter="업무"]').click()
        expect(page.locator("#scheduleTagFilter")).to_have_value("업무")
        expect(page.locator(".todo-calendar-agenda .todo")).to_have_count(1)
        page.locator('[data-todo-layout="list"]').click()
        expect(page.locator(".todo")).to_have_count(1)
        expect(page.locator(".todo-overview")).to_contain_text("오늘 챙길 일 1건")
        page.locator('[data-todo-layout="calendar"]').click()
        page.locator("#todoCalendarAdd").click()
        expect(page.locator('#todoCreateTags [data-tag="업무"]')).to_have_attribute("aria-pressed", "true")
        page.locator("#todoCreateCancel").click()
        print("OK tags are saved on creation, filter both layouts, and prefill new schedules")

        page.locator("[data-schedule-tags]").click()
        page.locator('#scheduleTagsInput [data-tag="팀"]').click()
        page.locator('#scheduleTagsInput [data-tag="공유팀"]').click()
        fail["tags"] = True
        page.locator("#scheduleTagsDialog").get_by_role("button", name="저장", exact=True).click()
        expect(page.locator("#scheduleTagsError")).to_contain_text("일시적인 저장 오류")
        expect(page.locator('#scheduleTagsInput [data-tag="공유팀"]')).to_have_attribute("aria-pressed", "true")
        page.locator("#scheduleTagsDialog").get_by_role("button", name="저장", exact=True).click()
        expect(page.locator("#scheduleTagsDialog")).not_to_be_visible()
        expect(page.locator(".todo-calendar-agenda")).to_contain_text("#공유팀")
        print("OK editing tags retains failed input and saves changes")

        page.locator("#scheduleShareOpen").click()
        share_tags = page.locator("#scheduleShareTag")
        def chip(name):
            return share_tags.get_by_role("button", name=f"#{name}", exact=True)
        expect(chip("업무")).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#scheduleShareExternal")).not_to_be_checked()
        expect(page.locator("#scheduleShareDetails")).to_be_disabled()
        chip("업무").click()
        chip("외부팀").click()
        page.locator("#scheduleSharePreviewRun").click()
        expect(page.locator("#scheduleShareCount")).to_have_text("0개 선택")
        chip("업무").click()
        page.locator("#scheduleSharePreviewRun").click()
        expect(page.locator("#scheduleShareCount")).to_have_text("1개 선택")
        expect(page.locator("#scheduleSharePreview")).not_to_contain_text("개인 서류")
        chip("개인").click()
        expect(page.locator("#scheduleSharePreview")).to_be_empty()
        page.locator("#scheduleSharePreviewRun").click()
        expect(page.locator("#scheduleShareCount")).to_have_text("2개 선택")
        page.locator("#scheduleShareNone").click()
        expect(page.locator("#scheduleShareSave")).to_be_disabled()
        page.locator(".schedule-share-item").filter(has_text="팀 교육계획안").locator("input").check()
        fail["share"] = True
        page.locator("#scheduleShareSave").click()
        expect(page.locator("#scheduleShareError")).to_contain_text("일시적인 저장 오류")
        expect(page.locator("#scheduleShareCount")).to_have_text("1개 선택")
        with page.expect_download() as download_info:
            page.locator("#scheduleShareSave").click()
        download = download_info.value
        exported = Path(download.path()).read_bytes()
        event = Calendar.from_ical(exported).walk("VEVENT")
        assert len(event) == 1 and "교육계획안" in str(event[0]["SUMMARY"])
        assert "개인 서류" not in exported.decode()
        assert set(str(value) for value in event[0]["CATEGORIES"].cats) == {"업무", "공유팀"}
        expect(page.locator("#scheduleShareStatus")).to_contain_text("1개 일정을 파일로 저장")
        print("OK export previews the scope, invalidates changed options, and downloads only selected events")

        page.evaluate("window.pywebview = {api: {save: async (content, filename) => { window.savedSchedule = {content, filename}; return false; }}}")
        page.locator("#scheduleShareSave").click()
        expect(page.locator("#scheduleShareStatus")).to_contain_text("파일을 저장하지 않았습니다")
        expect(page.locator("[data-share-key]:checked")).to_have_count(1)
        assert page.evaluate("window.savedSchedule.filename.endsWith('.ics')")
        page.evaluate("window.pywebview.api.save = async () => true")
        page.locator("#scheduleShareSave").click()
        expect(page.locator("#scheduleShareStatus")).to_contain_text("1개 일정을 파일로 저장")
        print("OK desktop save cancellation keeps selections and a later save succeeds")

        artifacts = Path("artifacts/ux-review")
        artifacts.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifacts / "schedule-share-desktop.png"), full_page=True)
        page.locator("#scheduleShareClose").click()
        page.locator("#scheduleClearTag").click()
        expect(page.locator("#scheduleTagFilter")).to_have_value("")
        page.locator("#todoCalendarImport").click()
        received = Calendar.from_ical(exported)
        received.walk("VEVENT")[0].add("CATEGORIES", ["받은태그"])
        page.locator("#calendarFile").set_input_files({"name": "팀이 보낸 일정.ics", "mimeType": "text/calendar", "buffer": received.to_ical()})
        page.locator("#calendarPreviewRun").click()
        expect(page.locator("#calendarPreview")).to_contain_text("추가 1")
        page.locator("#calendarApply").click()
        expect(page.locator("#calendarImportStatus")).to_contain_text("반영했습니다")
        page.locator("#calendarImportClose").click()
        expect(page.locator(".external-calendar-card")).to_contain_text("#공유팀")
        page.locator(".external-calendar-card [data-schedule-tags]").click()
        expect(page.locator('#scheduleTagsInput [data-tag="받은태그"]')).to_have_attribute("aria-pressed", "true")
        page.locator('#scheduleTagsInput [data-tag="외부팀"]').click()
        page.locator("#scheduleTagsDialog").get_by_role("button", name="저장", exact=True).click()
        expect(page.locator("#scheduleTagsDialog")).not_to_be_visible()
        page.locator('.external-calendar-card [data-schedule-filter="외부팀"]').click()
        expect(page.locator(".todo-calendar-agenda .todo")).to_have_count(0)
        expect(page.locator(".external-calendar-card")).to_have_count(1)
        expect(page.locator(".external-calendar-card")).to_contain_text("#받은태그")
        page.locator('.external-calendar-card [data-schedule-filter="받은태그"]').click()
        expect(page.locator("#scheduleTagFilter")).to_have_value("받은태그")
        page.locator("#scheduleShareOpen").click()
        expect(page.locator("#scheduleShareWholePeriod")).not_to_be_checked()
        page.locator("#scheduleSharePreviewRun").click()
        expect(page.locator("#scheduleSharePreview")).to_be_empty()
        page.locator("#scheduleShareClose").click()
        print("OK receiving the ICS preserves tags; imported events can be retagged and filtered")

        page.locator("#scheduleClearTag").click()
        expect(page.locator(".todo-calendar-agenda .todo")).to_have_count(2)
        page.screenshot(path=str(artifacts / "schedule-desktop.png"), full_page=True)
        page.emulate_media(color_scheme="dark")
        page.screenshot(path=str(artifacts / "schedule-dark.png"), full_page=True)
        page.emulate_media(color_scheme="light")
        for width in (390, 320):
            page.set_viewport_size({"width": width, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            expect(page.locator('#tabs > [data-tab="todos"]')).to_be_visible()
            page.locator("#scheduleShareOpen").click()
            expect(chip("받은태그")).to_have_count(0)
            expect(page.locator("#scheduleShareWholePeriod")).not_to_be_checked()
            page.locator("#scheduleShareWholePeriod").check()
            page.locator("#scheduleShareExternal").check()
            expect(page.locator("#scheduleShareDetails")).to_be_enabled()
            page.locator("#scheduleSharePreviewRun").click()
            expect(page.locator("#scheduleShareCount")).to_have_text("3개 선택")
            expect(page.locator("#scheduleSharePreview")).not_to_contain_text("#받은태그")
            assert page.locator("#scheduleShareDialog .dlg-b").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            page.screenshot(path=str(artifacts / f"schedule-share-{width}.png"), full_page=True)
            page.locator("#scheduleShareClose").click()
        page.screenshot(path=str(artifacts / "schedule-mobile.png"), full_page=True)
        assert not errors, errors
        print("OK primary navigation, calendar and sharing fit narrow screens; no browser errors")
        browser.close()
    db.close()
    TMP.cleanup()


if __name__ == "__main__":
    main()
