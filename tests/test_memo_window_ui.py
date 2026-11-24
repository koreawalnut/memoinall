"""항상 위에 띄우기(데스크톱): 앱이 다 그려진 뒤 한 번만 다시 띄우기를 부르고, 10개 제한을 보여 준다.

데스크톱 연결(window.pywebview.api)은 가짜로 넣는다. 창을 실제로 띄우는 일은 tests/test_memo_windows.py 가 본다.
python tests/test_memo_window_ui.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-memo-window-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402
from memoinall import db, memo_windows, store  # noqa: E402
from memoinall.api import app  # noqa: E402

BRIDGE = """
window.memoCalls = {restore: [], open: []};
window.pywebview = {api: {
  restore_memo_windows: async () => {
    // 부른 순간 메인 화면이 다 그려져 있었는지 적어 둔다
    memoCalls.restore.push({cards: document.querySelectorAll('#view .card').length,
                            sidebar: !!document.querySelector('#status .stat')});
    return true;
  },
  open_memo_window: async (id) => {
    memoCalls.open.push(id);
    if (window.memoLimit) { const e = new Error(window.memoLimit); e.name = 'LimitReached'; throw e; }
  },
}};
"""


def main():
    errors = []
    with patch.object(store, "start_worker"), TestClient(app) as client, sync_playwright() as pw:
        client.put("/api/starter", json={"visible": False})
        notes = [store.add_memo(f"띄울 메모 {i}", enqueue_enrich=False)["id"] for i in range(11)]
        for memo_id in notes[:3]:
            memo_windows.pin(memo_id)
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(locale="ko-KR", viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(BRIDGE)

        def serve(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            sent = {"content-type": request.headers["content-type"]} if "content-type" in request.headers else {}
            response = client.request(request.method, path, content=request.post_data_buffer, headers=sent)
            headers = {k: v for k, v in response.headers.items() if k not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("http://127.0.0.1:8787/**", serve)
        page.goto("http://127.0.0.1:8787/")

        # ---- 앱이 다 그려진 뒤 한 번만
        page.wait_for_function("memoCalls.restore.length === 1")
        first = page.evaluate("memoCalls.restore[0]")
        assert first == {"cards": 11, "sidebar": True}, first
        page.evaluate("window.dispatchEvent(new Event('pywebviewready'))")        # 연결이 다시 알려 와도
        page.wait_for_timeout(300)
        assert page.evaluate("memoCalls.restore.length") == 1
        print("OK the desktop is asked to reopen pinned windows once, only after the memo list and sidebar are drawn")

        # ---- 버튼에 몇 개 띄웠는지
        button = page.locator("#dMemoWindow")
        page.locator(f'.card[data-id="{notes[5]}"]').click()
        expect(button).to_have_text("항상 위에 띄우기 · 3/10")
        button.click()
        assert page.evaluate("memoCalls.open") == [notes[5]]
        page.locator("#dClose").click()
        page.locator(f'.card[data-id="{notes[0]}"]').click()
        expect(button).to_have_text("띄운 창 보기")                                 # 이미 띄운 메모
        page.locator("#dClose").click()

        # ---- 10개가 차면: 표시하고, 누르면 안내
        for memo_id in notes[3:10]:
            memo_windows.pin(memo_id)
        page.locator(f'.card[data-id="{notes[10]}"]').click()
        expect(button).to_have_text("항상 위에 띄우기 · 10/10")
        expect(button).to_have_attribute("title", "항상 위에 띄우기는 10개까지입니다. 띄워 둔 창 하나를 닫으면 띄울 수 있습니다.")
        page.evaluate("message => { window.memoLimit = message; }", memo_windows.LIMIT_MESSAGE)
        button.click()
        expect(page.locator("#toast")).to_have_text(memo_windows.LIMIT_MESSAGE)
        page.evaluate("() => { window.memoLimit = null; window.pywebview.api.open_memo_window = async () => { throw new Error('System.Reflection.TargetInvocationException'); }; }")
        button.click()
        expect(page.locator("#toast")).to_have_text("메모 창을 열지 못했습니다. 메모가 있는지 확인하고 다시 시도하세요.")
        print("OK the button shows how many are pinned, '띄운 창 보기' for a pinned memo, and the 10-window limit message")

        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close()
