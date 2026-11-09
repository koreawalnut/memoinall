"""Browser workflow for AI drafts, selecting references, refinement and history."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-ai-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from memoinall import ai_history, generate, llm, store
from memoinall.api import app


def main():
    calls, errors = [], []
    switches = {"fail_model": False, "fail_draft": False}

    def complete(system, prompt, **kwargs):
        calls.append(prompt)
        if switches["fail_model"]:
            raise llm.ProviderError("테스트 연결 실패")
        if "# 사용자의 수정 요청" in prompt:
            return f"짧게 다듬은 결과 [M{a['id']}]"
        return f"이번 주 결정 사항\n비용을 검토합니다. [M{a['id']}]"

    with TestClient(app) as client, sync_playwright() as pw, patch.object(llm, "available", return_value=True), patch.object(llm, "complete", side_effect=complete), patch.object(store, "enqueue"):
        a = store.add_memo("회의 기록: 비용 검토", enqueue_enrich=False)
        b = store.add_memo("제외할 개인적인 이야기", enqueue_enrich=False)
        c = store.add_memo("추가할 참고자료: 일정 확정", enqueue_enrich=False)
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 920})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            if path.endswith("/draft") and switches["fail_draft"]:
                route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "테스트 저장 실패"}))
                return
            response = client.request(request.method, path, content=request.post_data_buffer)
            headers = {k: v for k, v in response.headers.items() if k not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("http://127.0.0.1:8787/**", serve)
        page.goto("http://127.0.0.1:8787/")
        page.locator("#toggleSelection").click()
        page.locator(f'[data-select="{a["id"]}"]').check()
        page.locator(f'[data-select="{b["id"]}"]').check()
        page.locator("#generateSelection").click()
        page.locator("#genPlan").click()
        picker = page.locator("#genPicker")
        expect(picker.locator("[data-pick]")).to_have_count(2)
        picker.locator(f'[data-pick="{b["id"]}"]').uncheck()
        page.locator("#genAddPanel > summary").click()
        row = page.locator("#genFound .src").filter(has_text="추가할 참고자료")
        row.locator("button").click()
        expect(page.locator("#genRefCount")).to_have_text("2건")
        page.locator("#genRun2").click()
        expect(page.locator("#genOut")).to_contain_text("이번 주 결정 사항")
        assert a["body"] in calls[-1] and c["body"] in calls[-1] and b["body"] not in calls[-1]
        assert store.stats()["memos"] == 3
        result_id = page.evaluate("gen.output.history_id")
        print("OK exclude and add references; generation uses precisely the checked memos")

        page.locator("#genEdit").click()
        edited = f'직접 고친 문장 [M{a["id"]}]\n<img src=x onerror="window.injected=true">'
        page.locator("#genBody").fill(edited)
        page.wait_for_function("!genDirty()")
        assert ai_history.get(result_id)["draft_body"] == edited
        page.reload()
        page.locator('[data-tab="generate"]').click()
        expect(page.locator("#genBody")).to_have_value(edited)
        page.locator("#genDone").click()
        expect(page.locator("#genEdit")).to_be_visible()
        assert page.evaluate("gen.output.version") == 2
        assert not page.evaluate("!!window.injected")
        page.locator("#genMemo").click()
        expect(page.locator("#genMemo")).to_have_text("저장한 메모 열기")
        assert store.stats()["memos"] == 4
        memo_id = page.evaluate("gen.output.memo_id")
        assert "AI로 정리한 메모" in store.get_memo(memo_id)["body"]
        page.locator("#genMemo").click()
        expect(page.locator("#detail")).to_be_visible()
        page.locator("#dClose").click()
        assert store.stats()["memos"] == 4
        print("OK manual edits persist after reload; explicit memo save preserves provenance and avoids duplicates")

        page.locator('[data-gen-quick="더 짧게"]').click()
        expect(page.locator("#genFollowup")).to_have_value("더 짧게")
        assert page.evaluate("gen.output.version") == 2
        page.locator("#genRefine").click()
        expect(page.locator("#genOut")).to_contain_text("짧게 다듬은 결과")
        assert edited in calls[-1]
        assert page.evaluate("gen.output.version") == 3
        assert ai_history.get(result_id, 2)["output"] == edited
        page.locator("#genEdit").click()
        page.locator("#genBody").fill("실패해도 남아야 하는 내용")
        switches["fail_model"] = True
        page.locator("#genFollowup").fill("보고용으로 바꿔줘")
        page.locator("#genRefine").click()
        expect(page.locator("#genDraftStatus")).to_contain_text("테스트 연결 실패")
        expect(page.locator("#genOut")).to_contain_text("실패해도 남아야 하는 내용")
        assert ai_history.get(result_id)["output"] == "실패해도 남아야 하는 내용"
        switches["fail_model"] = False
        print("OK refinement uses the edited draft, preserves revisions and keeps content after provider failure")

        page.get_by_text("이전 내용 보기", exact=True).click()
        page.locator("#genOldSelect").select_option("1")
        expect(page.locator("#genOldPreview")).to_contain_text("이번 주 결정 사항")
        page.locator("#genRestore").click()
        # The old-version preview already contains this text; wait for restore to finish.
        page.wait_for_function("gen.output.version === 5 && !gen.busy")
        expect(page.locator("#genOut")).to_contain_text("이번 주 결정 사항")
        assert page.evaluate("gen.output.version") == 5
        assert ai_history.get(result_id, 4)["output"] == "실패해도 남아야 하는 내용"
        print("OK restoring an earlier result retains the replaced version")

        page.locator("#genEdit").click()
        switches["fail_draft"] = True
        page.locator("#genBody").fill("보관에 실패한 수정 내용")
        expect(page.locator("#genDraftStatus")).to_contain_text("보관하지 못했습니다")
        page.locator("#genNew").click()
        expect(page.locator("#genBody")).to_have_value("보관에 실패한 수정 내용")
        switches["fail_draft"] = False
        page.locator("#genNew").click()
        expect(page.locator("#genInput")).to_have_value("")
        expect(page.locator("#genOut")).to_be_empty()
        page.locator("#genHistory > summary").click()
        page.locator(f'[data-gen-history="{result_id}"]').click()
        expect(page.locator("#genBody")).to_have_value("보관에 실패한 수정 내용")
        page.locator("#genDone").click()
        print("OK failed draft save blocks replacing work; history reopens the recovered edit")

        artifact = Path(__file__).resolve().parents[1] / "artifacts" / "ux-review"
        artifact.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifact / "ai-editing-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 720, "height": 920})
        page.emulate_media(color_scheme="dark")
        page.screenshot(path=str(artifact / "ai-editing-narrow.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    main()
