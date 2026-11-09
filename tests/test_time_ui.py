"""24-hour local display, including midnight, UTC rollover and imported tags."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-time-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from memoinall import db, store
from memoinall.api import app


def main():
    errors = []
    with patch.object(store, "start_worker"), TestClient(app) as client, sync_playwright() as pw:
        memo = store.add_memo("가져온 회의 메모 #업무", source="evernote", created_at="2026-11-09T14:30:00",
                              updated_at="2026-11-09T23:05:00", enqueue_enrich=False)
        browser = pw.chromium.launch(headless=True)
        for locale in ("ko-KR", "en-US"):
            context = browser.new_context(locale=locale, timezone_id="Asia/Seoul", viewport={"width": 1280, "height": 920})
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))

            def serve(route):
                request = route.request
                path = request.url.split("127.0.0.1:8787", 1)[-1]
                response = client.request(request.method, path, content=request.post_data_buffer)
                headers = {k: v for k, v in response.headers.items() if k not in {"content-length", "content-encoding"}}
                route.fulfill(status=response.status_code, headers=headers, body=response.content)

            page.route("http://127.0.0.1:8787/**", serve)
            page.goto("http://127.0.0.1:8787/")
            cases = {
                "2026-11-09T14:30:00": "2026-11-09 14:30",
                "2026-11-09T00:00:00": "2026-11-09 00:00",
                "2026-11-09T23:59:00": "2026-11-09 23:59",
                "2026-11-09T15:00:00Z": "2026-11-10 00:00",
                "2026-11-09T23:30:00-08:00": "2026-11-10 16:30",
                "2026-11-09T14:30:00+09:00": "2026-11-09 14:30",
                "2026-11-09": "2026-11-09 00:00",
                "<img src=x onerror=alert(1)>": "",
                "invalid": "",
            }
            for value, expected in cases.items():
                assert page.evaluate("value => fmt(value)", value) == expected, (locale, value)
            card = page.locator(f'.card[data-id="{memo["id"]}"]')
            expect(card.locator(".card-h .meta")).to_have_text("2026-11-09 14:30")
            expect(card.locator(".badges")).to_contain_text("#에버노트")
            card.click()
            expect(page.locator("#dMeta")).to_have_text("작성 2026-11-09 14:30 · 수정 2026-11-09 23:05")
            if locale == "ko-KR":
                artifact = Path(__file__).resolve().parents[1] / "artifacts" / "ux-review" / "time-source-tags.png"
                artifact.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(artifact))
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                expect(page.locator("#dMeta")).to_be_visible()
            context.close()
            print(f"OK {locale}: local 24-hour display, midnight, timezone conversion, detail and source tag")
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close()
