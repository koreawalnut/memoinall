"""Notion 가져오기 화면: 안내 · 연결 테스트 · 토큰 저장 · 미리보기 · 가져오기.  python tests/test_notion_ui.py

Notion 호출은 가짜로 바꾼다(실제 API 동작은 tests/test_notion.py 가 가짜 서버로 본다).
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-notion-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
os.environ.pop("NOTION_TOKEN", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402
from memoinall import db, settings, store  # noqa: E402
from memoinall.api import app  # noqa: E402
from memoinall.importers import Note  # noqa: E402
from memoinall.importers.notion import NotionImporter  # noqa: E402

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts" / "ux-review"
TOKEN = "ntn_test_token_1234567890"


def fake_test(self):
    if self.token != TOKEN:
        return {"ok": False, "message": "토큰이 올바르지 않습니다. 확인하세요."}
    return {"ok": True, "pages": 2, "message": "연결 성공 — ‘memoinall’ · 우리 팀 · 읽을 수 있는 페이지 2개"}


def fake_pages(self):
    assert self.token == TOKEN and self.limit == 50 and self.since == "2026-11-01", (self.token, self.limit, self.since)
    return [
        Note(external_id="page:a", body="2026 교육 기획\n상태: 진행 중\n\n## 목표\n연수원 AX 세미나 기획안",
             created_at="2026-11-01T09:30:00", updated_at="2026-11-20T12:00:00", tags=["교육_과제"]),
        Note(external_id="page:b", body="회의록\n\n결정: 매주 화요일 정기 회의",
             created_at="2026-11-02T10:00:00", updated_at="2026-11-10T10:00:00"),
    ]


def main():
    errors, reads = [], []
    with patch.object(store, "start_worker"), TestClient(app) as client, sync_playwright() as pw, \
         patch.object(NotionImporter, "test_connection", fake_test), \
         patch.object(NotionImporter, "_read_pages", lambda self: reads.append(1) or fake_pages(self)):
        client.put("/api/starter", json={"visible": False})
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
        page.goto("http://127.0.0.1:8787/")
        page.locator("#tabs [data-tab='import']").click()
        panel = page.locator("#notionPanel")
        expect(panel.locator("summary").first).to_contain_text("○ 미설정")
        panel.locator("summary").first.click()

        # ---- 처음이면 안내가 펼쳐져 있다
        guide = panel.locator(".notion-guide")
        expect(guide).to_have_attribute("open", "")
        expect(guide.locator("ol > li")).to_have_count(5)
        link = guide.get_by_role("link", name="Notion 개발자 페이지")
        expect(link).to_have_attribute("href", "https://app.notion.com/developers/connections")
        expect(link).to_have_attribute("target", "_blank")
        for words in ("소유자(Owner)", "내용 읽기(Read content)", "API token", "••• → 연결(Connections)", "하위 페이지가 모두",
                      "이미 가져온 메모도 갱신"):
            expect(guide).to_contain_text(words)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        panel.screenshot(path=str(ARTIFACTS / "notion-guide.png"))
        print("OK first-time users see a 5-step guide with the developer page link, owner limits and page sharing")

        # ---- 연결 테스트는 저장 전 입력값으로
        panel.locator("#ntToken").fill("ntn_wrong")
        panel.locator("#ntTest").click()
        expect(panel.locator("#ntOut")).to_contain_text("✗ 토큰이 올바르지 않습니다")
        panel.locator("#ntToken").fill(TOKEN)
        panel.locator("#ntTest").click()
        expect(panel.locator("#ntOut")).to_contain_text("✓ 연결 성공 — ‘memoinall’ · 우리 팀 · 읽을 수 있는 페이지 2개")
        assert settings.get("import.notion.token") == ""                          # 시험만 했지 저장은 안 했다

        # ---- 저장: 토큰은 가려서 보이고 안내는 접힌다
        panel.locator("#ntLimit").fill("50")
        panel.locator("#ntSince").fill("2026-11-01")
        panel.locator("#ntSave").click()
        expect(page.locator("#toast")).to_contain_text("Notion 설정을 저장했습니다")
        assert settings.get("import.notion.token") == TOKEN
        assert (settings.get("import.notion.limit"), settings.get("import.notion.since")) == ("50", "2026-11-01")
        panel = page.locator("#notionPanel")
        expect(panel.locator("summary").first).to_contain_text("● 토큰 설정됨")
        if not panel.evaluate("el => el.open"):
            panel.locator("summary").first.click()
        expect(panel.locator(".notion-guide")).not_to_have_attribute("open", "")
        placeholder = panel.locator("#ntToken").get_attribute("placeholder")
        assert TOKEN not in placeholder and "바꾸려면 새로 입력" in placeholder, placeholder
        print("OK connection test uses typed values; saving masks the token and folds the guide")

        # ---- 미리보기 → 가져오기 (가져오기는 방금 읽은 것을 다시 쓴다)
        panel.locator('[data-src-preview="notion"]').click()
        out = panel.locator("#impOut-notion")
        expect(out).to_contain_text("2")
        expect(out).to_contain_text("2026 교육 기획")
        assert len(reads) == 1
        panel.locator('[data-src-run="notion"]').click()
        expect(page.locator("#toast")).to_contain_text("2건 가져옴")
        assert len(reads) == 1, reads                                               # Notion 을 다시 읽지 않았다
        memo = store.get_memo(store.find_by_external("notion", "page:a"))
        assert memo["source"] == "notion" and memo["created_at"] == "2026-11-01T09:30:00", (memo["source"], memo["created_at"])
        assert {"교육_과제", "Notion"} <= set(memo["facets"]["tag"]), memo["facets"]
        expect(out).to_contain_text("저장됨")
        panel.screenshot(path=str(ARTIFACTS / "notion-imported.png"))
        page.locator("#tabs [data-tab='import']").click()                         # 다시 그리면 건수가 보인다(다른 소스와 같다)
        expect(page.locator("#notionPanel")).to_contain_text("가져와 있는 메모 2건")
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#notionPanel summary").first.click()
        wide = page.evaluate("""() => [...document.querySelectorAll('body *')]
            .filter(el => el.getBoundingClientRect().right > window.innerWidth + 1 && el.offsetParent !== null)
            .slice(0, 8).map(el => el.tagName + '.' + el.className + ' ' + Math.round(el.getBoundingClientRect().right)
              + ' «' + el.textContent.trim().slice(0, 40) + '» in ' + (el.closest('[id]')?.id || ''))""")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), wide
        print("OK preview then import saves pages as memos with original times and #Notion, without reading Notion twice")

        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close()
