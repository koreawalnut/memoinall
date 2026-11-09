"""Tag selection stays consistent across AI discovery, generation and history."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-ai-scope-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from memoinall import db, generate, llm, store
from memoinall.api import app


def main():
    errors, calls = [], []

    def complete(system, prompt, **kwargs):
        calls.append(prompt)
        return "회의 내용을 정리한 결과입니다."

    with patch.object(store, "start_worker"), TestClient(app) as client, sync_playwright() as pw, \
         patch.object(llm, "available", return_value=True), patch.object(llm, "complete", side_effect=complete), \
         patch.object(generate, "derive_queries", return_value={"queries": ["회의"], "method": "rules", "topic": "회의"}):
        a = store.add_memo("회의: 계약 일정 #업무", source="evernote", enqueue_enrich=False)
        b = store.add_memo("회의: 다른 앱 기록 #업무", source="sticky", enqueue_enrich=False)
        c = store.add_memo("회의: 제외할 개인 기록 #개인", source="evernote", enqueue_enrich=False)
        d = store.add_memo("회의: 진행 상황 #업무", source="evernote", enqueue_enrich=False)
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 960})
        page.on("pageerror", lambda e: errors.append(str(e)))

        def serve(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            response = client.request(request.method, path, content=request.post_data_buffer)
            headers = {k: v for k, v in response.headers.items() if k not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("http://127.0.0.1:8787/**", serve)
        page.goto("http://127.0.0.1:8787/")
        page.locator('[data-tab="generate"]').click()
        page.locator("#genTagFilter > summary").click()
        page.locator('[data-gen-tag="업무"]').click()
        page.locator("#genTagSearch").fill("에버")
        expect(page.locator("[data-gen-tag]")).to_have_count(1)
        page.locator('[data-gen-tag="에버노트"]').click()
        expect(page.locator("#genTagCount")).to_have_text("2개 선택")
        expect(page.locator("#genScope")).to_contain_text("#업무 · #에버노트")
        page.locator("#genPlan").click()
        expect(page.locator("#genPicker [data-pick]")).to_have_count(2)
        ids = set(page.locator("#genPicker [data-pick]").evaluate_all("els => els.map(e => Number(e.dataset.pick))"))
        assert ids == {a["id"], d["id"]}, ids
        expect(page.locator("#genRun")).to_be_disabled()
        page.locator("#genAddPanel > summary").click()
        expect(page.locator("#genFound .src")).to_have_count(2)
        expect(page.locator("#genFound")).not_to_contain_text("다른 앱 기록")
        expect(page.locator("#genFound")).not_to_contain_text("개인 기록")
        page.locator("#genFind").fill("개인")
        expect(page.locator("#genFound")).to_contain_text("찾은 메모가 없습니다")
        page.locator(f'#genPicker [data-pick="{a["id"]}"]').check()
        page.locator("#genInput").fill("회의 정리")
        page.locator("#genRun").click()
        expect(page.locator("#genOut")).to_contain_text("회의 내용을 정리한 결과")
        assert a["body"] in calls[-1]
        assert all(m["body"] not in calls[-1] for m in (b, c, d))
        print("OK source tags, AND scope, pre-instruction browse, extra search and exact AI selection")

        page.reload()
        page.locator('[data-tab="generate"]').click()
        expect(page.locator("#genOut")).to_contain_text("회의 내용을 정리한 결과")
        expect(page.locator("#genTagCount")).to_have_text("2개 선택")
        expect(page.locator('[data-gen-tag="에버노트"]')).to_have_attribute("aria-pressed", "true")
        page.locator("#genPlan").click()
        expect(page.locator("#genPicker [data-pick]")).to_have_count(2)
        expect(page.locator("#genRefCount")).to_have_text("2건")
        page.locator('[data-gen-remove-tag="에버노트"]').click()
        expect(page.locator("#genPlanOut")).to_be_empty()
        assert page.evaluate("gen.review === null && gen.memoIds === null")
        page.locator("#genInput").fill("")
        page.locator("#genPlan").click()
        expect(page.locator("#genPicker [data-pick]")).to_have_count(3)
        print("OK saved result restores tag scope; changing a tag discards old source selection")

        page.locator('[data-gen-tag="개인"]').click()
        page.locator("#genPlan").click()
        expect(page.locator("#genProgress")).to_contain_text("고른 태그에 맞는 메모를 찾지 못했습니다")
        expect(page.locator("#genPicker [data-pick]")).to_have_count(0)
        expect(page.locator("#genRun")).to_be_disabled()
        page.locator("#genTagClear").click()
        expect(page.locator("#genScope")).to_have_text("전체 메모에서 필요한 내용을 찾습니다")
        page.locator("#genPlan").click()
        expect(page.locator("#genPicker [data-pick]")).to_have_count(4)
        page.locator('[data-gen-tag="스티커메모"]').click()
        page.locator("#genInput").fill("회의 정리")
        page.locator("#genRun").click()
        page.wait_for_function("!gen.busy && gen.output?.scope?.tags?.[0] === '스티커메모'")
        assert b["body"] in calls[-1]
        assert all(m["body"] not in calls[-1] for m in (a, c, d))
        print("OK empty scope does not broaden; clear and direct generation use the new scope")

        artifact = Path(__file__).resolve().parents[1] / "artifacts" / "ux-review"
        artifact.mkdir(parents=True, exist_ok=True)
        page.locator("#genTagFilter").scroll_into_view_if_needed()
        page.screenshot(path=str(artifact / "ai-tag-scope-desktop.png"))
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#genTagFilter").scroll_into_view_if_needed()
        expect(page.locator("#genTagSearch")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(artifact / "ai-tag-scope-narrow.png"))
        assert not errors, errors
        browser.close()
        print("OK desktop and narrow layouts; no uncaught browser errors")


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close()
