"""고정·본문 링크·휴지통 화면 흐름.  python tests/test_trash_ui.py"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-trash-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402
from memoinall import db, store  # noqa: E402
from memoinall.api import app  # noqa: E402

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts" / "ux-review"


def main():
    errors, dialogs = [], []
    with patch.object(store, "start_worker"), TestClient(app) as client, sync_playwright() as pw:
        links = store.add_memo("회의 자료 https://example.com/docs?id=3 확인하고, 공지는 https://naver.com에서 보기.\n"
                               "참고 (https://ko.wikipedia.org/wiki/한국어) #업무", enqueue_enrich=False,
                               created_at="2026-11-20T09:00:00")
        older = store.add_memo("오래된 기획 메모 #기획", enqueue_enrich=False, created_at="2026-11-01T09:00:00")
        spare = store.add_memo("지울 메모 하나", enqueue_enrich=False, created_at="2026-11-10T09:00:00")
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul", viewport={"width": 1280, "height": 900})
        context.route("https://example.com/**", lambda route: route.fulfill(body="ok", content_type="text/plain"))
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))

        def on_dialog(dialog):
            dialogs.append(dialog.message)
            dialog.accept()
        page.on("dialog", on_dialog)

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
        cards = page.locator("#view .card[data-id]")
        expect(cards.first).to_have_attribute("data-id", str(links["id"]))

        # ---- 고정: 카드의 📌 를 누르면 열리지 않고 맨 위로 올라간다
        older_card = page.locator(f'.card[data-id="{older["id"]}"]')
        older_card.locator("[data-pin]").click()
        expect(page.locator("#detail")).not_to_be_visible()
        expect(page.locator("#toast")).to_contain_text("맨 위에 고정했습니다")
        expect(cards.first).to_have_attribute("data-id", str(older["id"]))
        expect(cards.first).to_have_class("card pinned")
        expect(cards.first.locator("[data-pin]")).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#view .status").first).to_contain_text("고정한 메모 먼저")
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.locator("#view").screenshot(path=str(ARTIFACTS / "pin-feed.png"))
        cards.first.locator("[data-pin]").press("Enter")                    # 키보드로도 풀 수 있다
        expect(cards.first).to_have_attribute("data-id", str(links["id"]))
        expect(page.locator("#detail")).not_to_be_visible()
        print("OK pin from a card keeps the list open, moves the memo to the top and can be undone by keyboard")

        # ---- 본문 링크: 칩으로 보여 주고 브라우저 새 창으로 연다. 고치면 바로 맞춰진다.
        page.locator(f'.card[data-id="{links["id"]}"]').click()
        chips = page.locator("#dLinks a.badge.link")
        expect(chips).to_have_count(3)
        hrefs = chips.evaluate_all("els => els.map(a => [a.href, a.target, a.rel, a.textContent.trim()])")
        assert hrefs == [
            ["https://example.com/docs?id=3", "_blank", "noopener noreferrer", "🔗 example.com/docs?…"],
            ["https://naver.com/", "_blank", "noopener noreferrer", "🔗 naver.com"],
            ["https://ko.wikipedia.org/wiki/%ED%95%9C%EA%B5%AD%EC%96%B4", "_blank", "noopener noreferrer",
             "🔗 ko.wikipedia.org/wiki/한국어"],
        ], hrefs
        page.locator("#detail").screenshot(path=str(ARTIFACTS / "memo-links.png"))
        with page.expect_popup() as popup:
            chips.first.click()
        assert popup.value.url == "https://example.com/docs?id=3", popup.value.url
        popup.value.close()
        page.locator("#dBody").fill("링크 없는 메모 javascript:alert(1) ftp://files.example.com")
        expect(page.locator("#dLinks")).to_be_hidden()                     # http(s) 만 링크로 연다
        page.locator("#dBody").fill(links["body"] + "\n새 링크 https://example.com/new.")
        expect(chips).to_have_count(4)
        expect(chips.last).to_have_attribute("href", "https://example.com/new")
        page.locator("#dBody").fill(links["body"])
        print("OK body links become chips that open in a new browser window and follow edits")

        # ---- 메모 화면의 고정 버튼
        expect(page.locator("#dPin")).to_have_text("📌 고정")
        page.locator("#dPin").click()
        expect(page.locator("#dPin")).to_have_text("📌 고정됨")
        expect(page.locator("#dPin")).to_have_attribute("aria-pressed", "true")
        expect(cards.first.locator("[data-pin]")).to_have_attribute("aria-pressed", "true")

        # ---- 삭제 → 휴지통, 알림의 되돌리기로 바로 되살린다(확인 창 없음)
        page.locator("#dDelete").click()
        expect(page.locator("#detail")).not_to_be_visible()
        expect(page.locator("#toast")).to_contain_text("휴지통으로 옮겼습니다")
        expect(page.locator(f'.card[data-id="{links["id"]}"]')).to_have_count(0)
        assert dialogs == [], dialogs
        page.locator("#toast .toast-action").click()
        expect(page.locator("#toast")).to_contain_text("메모를 되살렸습니다")
        expect(cards.first).to_have_attribute("data-id", str(links["id"]))       # 고정도 그대로 돌아온다
        expect(cards.first).to_have_class("card pinned")
        page.locator("#toast .toast-action").click()                            # '열기'
        expect(page.locator("#detail")).to_be_visible()
        expect(page.locator("#dTitle")).to_contain_text("회의 자료")
        # 저장하지 않은 수정이 있을 때만 묻는다
        page.locator("#dBody").fill(links["body"] + " 고친 내용")
        page.locator("#dDelete").click()
        assert dialogs == ["고친 내용은 저장되지 않습니다. 이 메모를 휴지통으로 옮길까요?"], dialogs
        expect(page.locator("#toast")).to_contain_text("휴지통으로 옮겼습니다")
        print("OK delete moves to the trash without a prompt, undo restores it with its pin, and unsaved edits are confirmed")

        # ---- 휴지통 화면: 더 보기 → 휴지통
        page.locator(f'.card[data-id="{spare["id"]}"]').click()
        page.locator("#dDelete").click()
        expect(page.locator(f'.card[data-id="{spare["id"]}"]')).to_have_count(0)
        page.locator("#moreMenu summary").click()
        page.locator('#moreMenu [data-tab="trash"]').click()
        expect(page.locator("#lead")).to_contain_text("30일 동안")
        items = page.locator("#view .trash-card")
        expect(items).to_have_count(2)
        expect(items.first).to_contain_text("지울 메모 하나")                   # 최근에 지운 것이 위
        expect(items.first).to_contain_text("30일 뒤 완전히 삭제")
        expect(page.locator(".searchbar")).to_be_hidden()
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(ARTIFACTS / "trash-desktop.png"), full_page=True)
        items.nth(1).get_by_role("button", name="되살리기").click()
        expect(items).to_have_count(1)
        expect(page.locator("#toast")).to_contain_text("메모를 되살렸습니다")
        items.first.get_by_role("button", name="완전히 삭제").click()
        assert dialogs[-1] == "‘지울 메모 하나’ 메모를 완전히 지울까요? 되돌릴 수 없습니다.", dialogs
        expect(page.locator("#view .empty")).to_contain_text("휴지통이 비어 있습니다")
        assert db.one("SELECT 1 FROM memos WHERE id=?", (spare["id"],)) is None
        assert db.one("SELECT 1 FROM memo_trash WHERE memo_id=?", (spare["id"],)) is None
        assert db.one("SELECT 1 FROM memos WHERE id=?", (links["id"],)) is not None

        # 비우기
        page.locator("#tabs [data-tab='feed']").click()
        page.locator(f'.card[data-id="{older["id"]}"]').click()
        page.locator("#dDelete").click()
        page.locator("#moreMenu summary").click()
        page.locator('#moreMenu [data-tab="trash"]').click()
        expect(items).to_have_count(1)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(ARTIFACTS / "trash-narrow.png"), full_page=True)
        page.locator("#emptyTrash").click()
        assert dialogs[-1] == "휴지통의 메모 1건을 모두 완전히 지울까요? 되돌릴 수 없습니다.", dialogs
        expect(page.locator("#view .empty")).to_contain_text("휴지통이 비어 있습니다")
        expect(page.locator("#toast")).to_contain_text("1건을 완전히 지웠습니다")
        print("OK trash lists newest first, restores, deletes permanently and empties; narrow layout fits")

        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close()
