"""Save, share and reuse task settings without interrupting the ordinary AI flow."""
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-workflows-ui-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from memoinall import ai_history, db, generate, llm, store, workflows
from memoinall.api import app


def main():
    errors, calls, requests = [], [], []
    switches = {"fail_draft": False}

    def complete(system, prompt, **kwargs):
        calls.append(prompt)
        return "주간보고 결과입니다."

    with patch.object(store, "start_worker"), TestClient(app) as client, sync_playwright() as pw, \
         patch.object(llm, "available", return_value=True), patch.object(llm, "complete", side_effect=complete), \
         patch.object(workflows, "today", return_value=date(2026, 11, 9)) as calendar, \
         patch.object(generate, "derive_queries", return_value={"queries": ["회의"], "method": "rules", "topic": "회의"}):
        original = store.add_memo("회의: 원래 팀 자료 #업무", created_at="2026-11-09T10:00:00", enqueue_enrich=False)
        peer = store.add_memo("회의: 동료 팀 자료 #다른팀", created_at="2026-11-09T10:00:00", enqueue_enrich=False)
        old = store.add_memo("회의: 기간 밖의 자료 #다른팀", created_at="2026-11-02T10:00:00", enqueue_enrich=False)
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 960}, accept_downloads=True)
        page.on("pageerror", lambda e: errors.append(str(e)))

        def serve(route):
            request = route.request
            path = request.url.split("127.0.0.1:8787", 1)[-1]
            requests.append(path)
            if path.endswith("/draft") and switches["fail_draft"]:
                route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "테스트 저장 실패"}))
                return
            sent = {"content-type": request.headers["content-type"]} if "content-type" in request.headers else {}
            response = client.request(request.method, path, content=request.post_data_buffer, headers=sent)
            headers = {k: v for k, v in response.headers.items() if k not in {"content-length", "content-encoding"}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route("http://127.0.0.1:8787/**", serve)
        page.goto("http://127.0.0.1:8787/")
        page.locator('[data-tab="generate"]').click()
        expect(page.locator("#genInput")).to_be_visible()
        assert not page.locator("#workflowBar").evaluate("el => el.open")
        expect(page.locator("#workflowSaveCurrent")).not_to_be_visible()
        expect(page.locator("[data-workflow-quick]")).to_have_count(0)
        expect(page.locator("#workflowQuickSave")).to_be_visible()
        artifact = Path(__file__).resolve().parents[1] / "artifacts" / "ux-review"
        artifact.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifact / "workflows-default.png"))
        page.locator("#genInput").fill("회의 내용을 주간보고로 정리해줘")
        page.locator("#genFormat").select_option("report")
        page.locator("#genTagFilter > summary").click()
        page.locator('[data-gen-tag="업무"]').click()
        page.get_by_text("찾을 범위 바꾸기", exact=True).click()
        page.locator("#genPeriod").select_option("this_week")
        page.locator("#workflowBar > summary").click()
        page.locator("#workflowSaveCurrent").click()
        expect(page.locator("#workflowDialog")).to_be_visible()
        expect(page.locator("#wfTags")).to_have_value("업무")
        expect(page.locator("#wfPeriod")).to_have_value("this_week")
        expect(page.locator("#wfFormat")).to_have_value("report")
        page.locator("#wfName").fill("우리 팀 주간보고")
        page.locator("#wfSave").click()
        expect(page.locator("#workflowDialog")).not_to_be_visible()
        assert calls == []
        first = workflows.all_workflows()[0]
        expect(page.locator("#genInput")).to_have_value("회의 내용을 주간보고로 정리해줘")
        expect(page.locator(f'[data-workflow-quick="{first["id"]}"]')).to_have_text("우리 팀 주간보고")
        print("OK saved request appears as a one-click button; saving does not change the draft or call a model")

        page.locator("#workflowManage").click()
        with page.expect_download() as download_info:
            page.locator("#wfExport").click()
        downloaded = json.loads(Path(download_info.value.path()).read_text(encoding="utf-8"))
        assert downloaded["format"] == workflows.FILE_FORMAT
        assert downloaded["workflow"]["period"] == "this_week"
        assert downloaded["workflow"]["since"] is None
        assert all(m["body"] not in json.dumps(downloaded, ensure_ascii=False) for m in (original, peer, old))
        page.locator("#wfName").fill("저장하지 않은 이름")
        page.once("dialog", lambda d: d.dismiss())
        page.locator("#wfClose").click()
        expect(page.locator("#workflowDialog")).to_be_visible()
        page.once("dialog", lambda d: d.accept())
        page.locator("#wfClose").click()
        assert workflows.get(first["id"])["name"] == "우리 팀 주간보고"
        print("OK shared file contains portable settings only; unsaved manager edits are protected")

        page.locator("#workflowManage").click()
        # Loading a file is a preview. It cannot overwrite the original task.
        expect(page.locator("#workflowDialog")).to_be_visible()
        page.locator("#wfFile").set_input_files({"name": "업무.json", "mimeType": "application/json", "buffer": json.dumps(downloaded, ensure_ascii=False).encode("utf-8")})
        expect(page.locator("#wfName")).to_have_value("우리 팀 주간보고 (2)")
        assert len(workflows.all_workflows()) == 1
        page.locator("#wfName").fill("동료의 주간보고")
        page.locator("#wfTags").fill("다른팀")
        page.locator("#wfFile").set_input_files({"name": "broken.json", "mimeType": "application/json", "buffer": b"{"})
        expect(page.locator("#wfMessage")).to_contain_text("읽을 수 없습니다")
        expect(page.locator("#wfTags")).to_have_value("다른팀")
        page.screenshot(path=str(artifact / "workflows-manager.png"))
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert page.locator("#workflowDialog").evaluate("el => el.scrollWidth <= el.clientWidth")
        page.screenshot(path=str(artifact / "workflows-manager-narrow.png"))
        page.locator("#wfSave").click()
        expect(page.locator("#workflowDialog")).not_to_be_visible()
        second = next(w for w in workflows.all_workflows() if w["name"] == "동료의 주간보고")
        page.set_viewport_size({"width": 1280, "height": 960})
        page.locator(f'[data-workflow-quick="{second["id"]}"]').click()
        expect(page.locator("#genScope")).to_contain_text("#다른팀 · 이번 주")
        assert calls == []
        page.locator("#genPlan").click()
        expect(page.locator("#genPicker [data-pick]")).to_have_count(1)
        assert page.locator("#genPicker [data-pick]").get_attribute("data-pick") == str(peer["id"])
        page.locator("#genRun").click()
        expect(page.locator("#genOut")).to_contain_text("주간보고 결과")
        assert peer["body"] in calls[-1] and old["body"] not in calls[-1] and original["body"] not in calls[-1]
        print("OK preview, adapt team tags, save and reuse; AI receives only the chosen tag and current period")

        next_week = store.add_memo("회의: 새 주의 자료 #다른팀", created_at="2026-11-16T10:00:00", enqueue_enrich=False)
        before = page.evaluate("gen.output.history_id")
        calendar.return_value = date(2026, 11, 16)
        page.evaluate("""() => { const RealDate = window.Date; window.Date = class extends RealDate {
            constructor(...args) { super(...(args.length ? args : ['2026-11-16T10:00:00+09:00'])); }
        }; }""")
        page.locator("#genRun").click()
        page.wait_for_function("id => !gen.busy && gen.output.history_id !== id", arg=before)
        assert next_week["body"] in calls[-1] and peer["body"] not in calls[-1]
        print("OK an open screen crossing into a new week searches again instead of reusing old selected memos")

        result_id = page.evaluate("gen.output.history_id")
        page.locator("#genEdit").click()
        switches["fail_draft"] = True
        page.locator("#genBody").fill("보관되어야 할 직접 수정")
        expect(page.locator("#genDraftStatus")).to_contain_text("보관하지 못했습니다")
        page.locator("#workflowBar > summary").click()
        page.locator("#workflowChoice").select_option(str(first["id"]))
        page.locator("#workflowUse").click()
        expect(page.locator("#genProgress")).to_contain_text("테스트 저장 실패")
        expect(page.locator("#genBody")).to_have_value("보관되어야 할 직접 수정")
        expect(page.locator("#genScope")).to_contain_text("#다른팀")
        switches["fail_draft"] = False
        page.locator("#workflowUse").click()
        expect(page.locator("#genScope")).to_contain_text("#업무 · 이번 주")
        expect(page.locator("#genOut")).to_be_empty()
        assert ai_history.get(result_id)["draft_body"] == "보관되어야 할 직접 수정"
        page.locator("#workflowBar > summary").click()
        page.locator("#workflowChoice").select_option(str(second["id"]))
        page.locator("#workflowManage").click()
        page.once("dialog", lambda d: d.accept())
        page.locator("#wfDelete").click()
        expect(page.locator("#workflowDialog")).not_to_be_visible()
        assert len(workflows.all_workflows()) == 1 and store.stats()["memos"] == 4
        print("OK applying tasks preserves previous drafts; failed save blocks replacement; deletion leaves memos intact")

        # 취합 자료로 정리하는 중에는 자료 연결을 그대로 두고 요청문·형식만 넣는다.
        import io
        from openpyxl import Workbook
        book = Workbook(); book.active.append(["부서", "금액"]); book.active.append(["영업", 100])
        data = io.BytesIO(); book.save(data)
        job = client.post("/api/collections/jobs", json={"title": "11월"}).json()["id"]
        client.post(f"/api/collections/jobs/{job}/files", params={"path": "a.xlsx"}, content=data.getvalue())
        preview = client.post(f"/api/collections/jobs/{job}/preview", json={"rules": {}}).json()
        saved = client.post(f"/api/collections/jobs/{job}/commit", json={"rules": {}, "revision": preview["revision"]}).json()
        page.evaluate("c => startCollectionGenerate(c)", {"id": saved["collection_id"], "version": 1, "title": "11월", "files": 1, "rows": 1})
        expect(page.locator("#genScope")).to_contain_text("사용할 자료: 11월")
        page.locator(f'[data-workflow-quick="{first["id"]}"]').click()
        expect(page.locator("#genInput")).to_have_value("회의 내용을 주간보고로 정리해줘")
        expect(page.locator("#genFormat")).to_have_value("report")
        expect(page.locator("#genScope")).to_contain_text("사용할 자료: 11월")
        expect(page.locator("#genProgress")).to_contain_text("태그·기간 조건을 쓰지 않습니다")
        print("OK saved requests keep the attached Excel collection")
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    try:
        main()
    finally:
        db.close()
