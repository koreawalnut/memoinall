"""Exercise review checkboxes, pagination and actual saved files in Chromium."""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-review-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from document_fixtures import onenote_desktop_records, onenote_docx
from playwright.sync_api import expect, sync_playwright
from memoinall import exchange, store
from memoinall.api import app


def main():
    with TestClient(app) as client, sync_playwright() as pw:
        for i in range(60):
            store.add_memo(f"업무 기록 {i:03d}\n본문 {i}", created_at=(datetime(2025, 1, 1) + timedelta(days=i)).isoformat(), enqueue_enrich=False)
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 920})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        output = Path(TMP.name) / "selected.json"

        def serve(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            content = request.post_data_buffer
            if path == "/api/export":
                payload = json.loads(content)
                payload["path"] = str(output)
                content = json.dumps(payload).encode()
            sent = {"content-type": request.headers["content-type"]} if "content-type" in request.headers else {}
            response = client.request(request.method, path, content=content, headers=sent)
            headers = {k: v for k, v in response.headers.items() if k not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("http://127.0.0.1:8787/**", serve)
        page.goto("http://127.0.0.1:8787/")
        page.locator('[data-tab="import"]').click()
        page.locator('[data-exchange-tab="share"]').click()
        page.locator("#expPreview").click()
        picker = page.locator("#expReview")
        expect(picker.locator("[data-picker-count]")).to_contain_text("60건 선택")
        expect(picker.locator("[data-pick]")).to_have_count(50)
        picker.locator("[data-picker-search]").fill("업무 기록 055")
        expect(picker.locator("[data-pick]")).to_have_count(1)
        picker.locator("[data-picker-none]").click()
        expect(picker.locator("[data-picker-count]")).to_contain_text("59건 선택")
        picker.locator("[data-picker-search]").fill("")
        picker.locator("[data-picker-next]").click()
        expect(picker.locator("[data-picker-page]")).to_contain_text("51–60")
        expect(picker.locator("[data-pick='56']")).not_to_be_checked()
        picker.locator("[data-picker-only]").check()
        expect(picker.locator("[data-pick='56']")).to_have_count(0)
        picker.locator("[data-picker-only]").uncheck()
        picker.locator("[data-picker-none]").click()
        page.locator("#expPw").fill("test-pass")
        page.locator("#expPw2").fill("test-pass")
        page.locator("#expSave").click()
        expect(page.locator("#expOut")).to_contain_text("하나 이상 선택")
        assert not output.exists()
        picker.locator("[data-picker-search]").fill("업무 기록 055")
        picker.locator("[data-picker-all]").click()
        picker.locator("summary").click()
        expect(picker.locator("pre")).to_contain_text("본문 55")
        page.locator("#expSave").click()
        expect(page.locator("#expOut")).to_contain_text("메모 1건을 내보냈습니다")
        packet = exchange.parse(output.read_text(encoding="utf-8"), "test-pass")
        assert packet["count"] == 1 and "055" in packet["memos"][0]["body"]
        print("OK search, pagination, empty selection and encrypted export contain exactly the checked memo")

        page.evaluate("""() => {
            window.teamShares = [];
            window.pywebview = {api: {
                share_to_teams: async (content, filename, text) => {
                    teamShares.push(text === undefined ? {content, filename} : {content, filename, text});
                    if (!filename) return window.teamsMode === 'manual' ? 'draft-copied' : 'filled';
                    return window.teamsMode === 'manual'
                        ? {name: filename, path: filename, method: 'manual', opened: true, reason: '새 채팅의 첨부 메뉴가 보이지 않습니다.'}
                        : {name: filename, path: filename, method: 'attached', opened: true};
                },
                reveal_teams_file: async filename => { window.revealedFile = filename; }
            }};
            window.dispatchEvent(new Event('pywebviewready'));   // 데스크톱 연결이 붙는 순간
        }""")
        output.unlink()
        page.locator("#expShare").click()
        # 데스크톱 앱이 Teams 새 채팅에 파일과 함께 보낼 글을 직접 넣는다
        expect(page.locator("#expOut")).to_contain_text("Teams 새 채팅에 파일과 안내 글을 넣었습니다")
        expect(page.locator("#expShareHint")).to_contain_text("파일과 안내 글이 자동으로 들어갑니다")
        shared_file, = page.evaluate("teamShares")
        assert shared_file["filename"].endswith("-1건.json") and not output.exists()
        assert exchange.parse(shared_file["content"], "test-pass")["memos"] == packet["memos"]
        assert "memoinall 메모 1건" in shared_file["text"] and "비밀번호는 따로" in shared_file["text"], shared_file["text"]
        page.get_by_role("button", name="파일 위치 열기").click()
        assert page.evaluate("revealedFile") == shared_file["filename"]
        # 자동으로 붙이지 못하면 새 채팅만 열고 '파일 첨부 → 이 디바이스에서 업로드'로 직접 올리게 안내
        page.evaluate("window.teamsMode = 'manual'")
        page.locator("#expShare").click()
        expect(page.locator("#expOut")).to_contain_text("이 디바이스에서 업로드")
        expect(page.locator("#expOut")).to_contain_text("새 채팅의 첨부 메뉴가 보이지 않습니다.")
        expect(page.locator("#expOut")).not_to_contain_text("Ctrl+V")
        assert page.evaluate("teamShares.length") == 2
        page.locator("#expPw2").fill("different")
        page.locator("#expShare").click()
        expect(page.locator("#expOut")).to_contain_text("두 비밀번호가 다릅니다")
        assert page.evaluate("teamShares.length") == 2
        page.locator("#expPw2").fill("test-pass")
        page.evaluate("openMemo(56)")
        page.locator("#dBody").fill("현재 편집한 한글 본문\n둘째 줄")
        page.locator("#dShare").click()
        assert page.evaluate("teamShares[2]") == {"content": "현재 편집한 한글 본문\n둘째 줄", "filename": None}
        expect(page.locator("#dShareMessage")).to_contain_text("받는 사람과 본문")
        page.evaluate("window.teamsMode = 'filled'")         # 앱이 새 채팅에 본문을 직접 넣은 경우
        page.locator("#dShare").click()
        expect(page.locator("#dShareMessage")).to_contain_text("Teams 새 채팅에 본문을 넣었습니다")
        assert page.evaluate("teamShares.length") == 4
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#dClose").click()
        print("OK Teams draft contains the visible memo; the attachment (only checked encrypted memos) is put into the new chat with a note, manual upload as fallback")
        page.evaluate("""() => {
            delete window.pywebview;
            Object.defineProperty(navigator, 'userAgentData', {configurable: true, value: {platform: 'Windows'}});
            Object.defineProperty(navigator, 'canShare', {configurable: true, value: () => true});
            Object.defineProperty(navigator, 'share', {configurable: true, value: async ({files}) => {
                window.webFile = {name: files[0].name, content: await files[0].text()};
            }});
        }""")
        page.locator("#expShare").click()
        expect(page.get_by_role("link", name="Teams 새 채팅 열기")).to_have_attribute("href", "https://teams.microsoft.com/l/chat/0/0?users=")
        with page.expect_download() as download:
            page.get_by_role("button", name="첨부파일 다운로드").click()
        assert download.value.suggested_filename.endswith("-1건.json")
        assert exchange.parse(download.value.path().read_text(encoding="utf-8"), "test-pass")["memos"] == packet["memos"]
        assert page.evaluate("!window.webFile")
        page.evaluate("Object.defineProperty(navigator, 'userAgentData', {configurable: true, value: {platform:'Android'}})")
        page.locator("#expShare").click()
        page.wait_for_function("window.webFile || document.querySelector('[data-open-share]')")
        if page.locator("[data-open-share]").count():
            page.locator("[data-open-share]").click()
        expect(page.locator("#expOut")).to_contain_text("공유 창에 내용을 전달")
        web_file = page.evaluate("webFile")
        assert web_file["name"].endswith("-1건.json")
        assert exchange.parse(web_file["content"], "test-pass")["memos"] == packet["memos"]
        page.evaluate("""Object.defineProperty(navigator, 'share', {configurable: true, value: async () => {
            throw new DOMException('cancelled', 'AbortError');
        }})""")
        page.locator("#expShare").click()
        page.wait_for_function("document.querySelector('#expOut').textContent.includes('취소') || document.querySelector('[data-open-share]')")
        if page.locator("[data-open-share]").count():
            page.locator("[data-open-share]").click()
        expect(page.locator("#expOut")).to_contain_text("공유를 취소")
        page.evaluate("""() => {
            HTMLAnchorElement.prototype.click = function () { window.chatLink = this.href };
            Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
                writeText: async text => { window.copiedDraft = text }
            }});
        }""")
        page.evaluate("openMemo(56)")
        expect(page.locator("#dShareFeedback")).to_be_hidden()
        page.locator("#dShare").click()
        expect(page.locator("#dShareMessage")).to_contain_text("받는 사람과 본문")
        assert page.evaluate("new URL(chatLink).searchParams.get('message') === copiedDraft && copiedDraft === document.querySelector('#dBody').value")
        page.evaluate("window.pywebview = {api:{share_to_teams:async () => {throw new Error('Teams를 열 수 없습니다.')}}}")
        page.locator("#dShare").click()
        expect(page.locator("#dShareMessage")).to_contain_text("Teams를 열 수 없습니다")
        page.locator("#dShareCopy").click()
        expect(page.locator("#dShareMessage")).to_contain_text("본문을 복사했습니다")
        page.locator("#dClose").click()
        print("OK browser receives the same export as a File and sharing can be cancelled")

        page.locator('[data-exchange-tab="import"]').click()
        content = '<en-export><note><title>팀 회의</title><content><![CDATA[<en-note>자료 확인<en-media type="image/png"/></en-note>]]></content><tag>팀 업무</tag></note><note><title>개인 메모</title><content><![CDATA[<en-note>선택하지 않을 내용</en-note>]]></content></note></en-export>'
        page.locator("#evernoteFile").set_input_files({"name": "notes.enex", "mimeType": "application/xml", "buffer": content.encode()})
        picker = page.locator("#impOut-evernote")
        expect(picker).to_contain_text("아직 저장 안 함")
        expect(picker).to_contain_text("첨부파일은 저장하지 않습니다")
        picker.locator('[data-pick="1"]').uncheck()
        expect(page.locator('[data-src-run="evernote"]')).to_contain_text("선택 1건")
        page.locator('[data-src-run="evernote"]').click()
        expect(picker).to_contain_text("가져오기 완료")
        assert store.count_by_source("evernote")["memos"] == 1
        page.locator('[data-src-preview="evernote"]').click()
        expect(picker.locator('[data-pick="0"]')).to_be_disabled()
        expect(picker.locator('[data-pick="1"]')).to_be_checked()
        page.locator("#evernoteFile").set_input_files({"name": "bad.enex", "mimeType": "application/xml", "buffer": b"<broken>"})
        expect(picker).to_contain_text("올바른 ENEX 파일이 아닙니다")
        expect(picker.locator("[data-pick]")).to_have_count(0)
        assert store.count_by_source("evernote")["memos"] == 1
        print("OK ENEX warning, subset import, duplicate display and invalid-file replacement")

        page.locator('[data-exchange-tab="share"]').click()
        packet = {"format": exchange.FORMAT, "version": exchange.VERSION, "memos": [
            {"uid": "coworker-1", "body": "동료 자료"}, {"uid": "coworker-2", "body": "추가 참고"}]}
        page.locator("#sharedFile").set_input_files({"name": "shared.json", "mimeType": "application/json", "buffer": exchange.dumps(packet).encode()})
        picker = page.locator("#impOut-shared")
        expect(picker.locator("[data-pick]")).to_have_count(2)
        picker.locator('[data-pick="0"]').uncheck()
        page.locator('[data-src-run="shared"]').click()
        expect(picker).to_contain_text("가져오기 완료")
        assert [m["body"] for m in store.list_memos() if m["source"] == "shared"] == ["추가 참고"]
        print("OK received shared file imports only the checked memo")
        page.set_viewport_size({"width": 720, "height": 900})
        page.locator("#expPreview").click()
        expect(page.locator("#expReview [data-picker-count]")).to_contain_text("62건")
        artifact = Path(__file__).resolve().parents[1] / "artifacts" / "ux-review" / "import-sharing-review.png"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifact), full_page=True)
        page.locator('[data-exchange-tab="import"]').click()
        page.locator("#onenoteFile").set_input_files({"name": "회의.docx", "mimeType": "application/octet-stream", "buffer": onenote_docx()})
        picker = page.locator("#impOut-onenote")
        expect(picker.locator("[data-pick]")).to_have_count(2)
        picker.locator('[data-pick="0"]').uncheck()
        page.locator('[data-src-run="onenote"]').click()
        expect(picker).to_contain_text("가져오기 완료")
        memo, = store.list_memos(tags=["OneNote"])
        assert "회신 요청" in memo["body"] and "진행 상황" not in memo["body"]
        page.locator('[data-src-preview="onenote"]').click()
        expect(picker.locator('[data-pick="1"]')).to_be_disabled()
        print("OK OneNote file picker previews each part, saves only checked text, and marks existing pages")
        with patch("memoinall.importers.onenote_local.LocalOneNoteImporter.available", return_value=True), \
                patch("memoinall.importers.onenote_local._read_desktop", return_value=onenote_desktop_records()):
            page.reload()
            page.locator('[data-tab="import"]').click()
            page.locator('[data-exchange-tab="import"]').click()
            card = page.locator("#impOut-onenote_local")
            page.get_by_role("button", name="가져올 페이지 미리보기").click()
            expect(card).to_contain_text("주간 회의")
            expect(card).to_contain_text("잠긴 섹션")
            assert store.count_by_source("onenote_local")["memos"] == 0
            page.get_by_role("button", name="전자 필기장 일괄 가져오기").click()
            expect(card).to_contain_text("가져오기 완료")
            assert store.count_by_source("onenote_local")["memos"] == 2
            page.get_by_role("button", name="가져올 페이지 미리보기").click()
            expect(card).to_contain_text("이미있음 2")
        print("OK desktop OneNote batch preview, save and duplicate count")
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    main()
