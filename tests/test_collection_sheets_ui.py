"""여러 시트가 있는 파일을 시트 이름별로 한 번에 선택.  python tests/test_collection_sheets_ui.py"""
import io, sys
sys.path.insert(0, r"C:\workspace\vscode\memoinall\tests"); sys.path.insert(0, r"C:\workspace\vscode\memoinall")
import test_collection_ui as t
from openpyxl import Workbook
from playwright.sync_api import expect, sync_playwright
from fastapi.testclient import TestClient

def multi(dept):
    b = Workbook(); ws = b.active; ws.title = "신청내역"
    ws.append(["부서", "성명", "금액"]); ws.append([dept, "가", 100]); ws.append([dept, "나", 200])
    w2 = b.create_sheet("메모"); w2.append(["부서", "성명", "금액"]); w2.append([dept, "다", 5])
    o = io.BytesIO(); b.save(o); return o.getvalue()

files = [(f"{d}.xlsx", multi(d)) for d in ("A팀", "B팀", "C팀")]
with TestClient(t.app) as client, sync_playwright() as pw:
    page = pw.chromium.launch(headless=True).new_page()
    errs = []; page.on("pageerror", lambda e: errs.append(str(e)))
    def serve(route):
        r = route.request; path = r.url.split("127.0.0.1:8787", 1)[-1]
        sent = {"content-type": r.headers["content-type"]} if "content-type" in r.headers else {}
        resp = client.request(r.method, path, content=r.post_data_buffer, headers=sent)
        route.fulfill(status=resp.status_code, headers={k: v for k, v in resp.headers.items() if k not in {"content-length", "content-encoding"}}, body=resp.content)
    page.route("http://127.0.0.1:8787/**", serve)
    page.goto("http://127.0.0.1:8787/"); page.locator('[data-tab="import"]').click(); page.locator("#openCollect").click()
    page.locator("#colFiles").set_input_files([{"name": n, "mimeType": t.XLSX, "buffer": b} for n, b in files])
    expect(page.locator(".col-groups")).to_be_visible(timeout=20000)
    expect(page.locator(".col-group")).to_have_count(2)
    expect(page.locator("#colCommit")).to_have_text("3개 파일 · 9행 취합하기")
    page.locator(".col-group", has_text="‘메모’").locator("input").uncheck()
    expect(page.locator("#colCommit")).to_have_text("3개 파일 · 6행 취합하기")
    expect(page.locator(".col-group", has_text="‘메모’")).to_contain_text("0/3개 파일")
    page.locator(".col-group", has_text="‘메모’").locator("input").check()
    expect(page.locator("#colCommit")).to_have_text("3개 파일 · 9행 취합하기")
    # 파일 하나만 따로 빼면 일부 선택 상태
    page.locator("[data-col-sheet]").first.uncheck()
    expect(page.locator(".col-group", has_text="‘신청내역’")).to_contain_text("2/3개 파일")
    assert page.locator(".col-group", has_text="‘신청내역’").locator("input").evaluate("e => e.indeterminate")
    assert not errs, errs
    print("OK sheets with the same name are selected for all files at once")
