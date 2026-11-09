"""Exercise review checkboxes, pagination and actual saved files in Chromium."""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

TMP = tempfile.TemporaryDirectory(prefix="memoinall-review-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
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
            response = client.request(request.method, path, content=content)
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
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    main()
