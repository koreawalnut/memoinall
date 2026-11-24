"""더 보기 → 태그별·기간 요약: 설정의 '내 태그' 단위로 메모를 모아 보기.  python tests/test_tag_board.py"""
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = tempfile.TemporaryDirectory(prefix="memoinall-tagboard-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import patch  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from memoinall import organize, store, tags  # noqa: E402
from memoinall.api import app  # noqa: E402


def main():
    errors = []
    with patch.object(store, "start_worker"), TestClient(app) as client, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))

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

        # 메모 입력칸: 내 태그가 없으면 접힌 상태에서도 '내 태그 정하기' 링크가 보이고, 누르면 새 태그 입력으로 간다
        setup = page.locator("#capTagSetup a")
        expect(setup).to_be_visible()
        expect(setup).to_have_text("내 태그 정하기")
        setup.click()
        expect(page.locator('[data-tab="settings"]')).to_have_class(re.compile(r"\bon\b"))
        expect(page.locator("#newTag")).to_be_focused()
        page.locator('[data-tab="feed"]').click()
        expect(page.locator("#captureTags")).not_to_have_attribute("open", "")     # 링크를 눌러도 칸은 펼쳐지지 않았다
        print("OK with no 'my tags', a hint link next to '태그 추가' jumps straight to the new-tag field in settings")

        # 내 태그가 없으면 설정으로 안내한다
        page.locator("#moreMenu > summary").click()
        expect(page.locator('.more-items [data-tab="tagboard"]')).to_have_text("태그별")
        assert page.locator('[data-tab="clusters"]').count() == 0
        page.locator('[data-tab="tagboard"]').click()
        expect(page.locator("#lead h2")).to_contain_text("태그별 메모")
        expect(page.locator("#view")).to_contain_text("아직 내 태그가 없습니다")
        page.locator("#tbSettings").click()
        expect(page.locator('[data-tab="settings"]')).to_have_class(re.compile(r"\bon\b"))
        expect(page.locator("#newTag")).to_be_focused()
        print("OK with no 'my tags' yet, the tag view points to settings")

        tags.add("회의", color="teal")
        tags.add("결제", color="blue")
        tags.add("보고", color="amber")       # 아직 메모 없음
        tags.reorder(["결제", "회의", "보고"])
        for i in range(7):
            store.add_memo(f"결제 오류 대응 {i} #결제", created_at=f"2026-11-0{i + 1}T09:00:00", enqueue_enrich=False)
        store.add_memo("결제 회의 정리 #회의 #결제", created_at="2026-11-10T09:00:00", enqueue_enrich=False)
        hidden = store.add_memo("보관한 결제 메모 #결제", created_at="2026-11-11T09:00:00", enqueue_enrich=False)
        store.set_flag(hidden["id"], archived=True)
        store.add_memo("주말 계획 #개인", created_at="2026-11-12T09:00:00", enqueue_enrich=False)
        store.add_memo("태그 없는 메모", created_at="2026-11-13T09:00:00", enqueue_enrich=False)

        d = client.get("/api/tags/board?per=5").json()
        groups = {g["name"]: g for g in d["groups"]}
        assert [g["name"] for g in d["groups"]] == ["결제", "회의", "보고"], d["groups"]
        assert groups["결제"]["count"] == 8 and len(groups["결제"]["memos"]) == 5          # 보관한 것은 빼고, 최근 5건
        assert groups["결제"]["memos"][0]["title"].startswith("결제 회의 정리")
        assert groups["회의"]["count"] == 1 and groups["보고"]["count"] == 0
        assert d["untagged"]["count"] == 2 and {m["title"] for m in d["untagged"]["memos"]} == {"주말 계획 #개인", "태그 없는 메모"}
        assert any(s["value"] == "개인" for s in d["suggested"])
        print("OK API groups memos by my tags in my order, leaves out archived memos, and lists the rest")

        page.reload()
        expect(page.locator("#capTagSetup")).to_be_hidden()          # 내 태그가 생기면 안내 링크는 사라진다
        page.locator("#moreMenu > summary").click()
        page.locator('[data-tab="tagboard"]').click()
        expect(page.locator("[data-tb-group]")).to_have_count(3)
        expect(page.locator("[data-tb-group] h3 .tagchip")).to_have_text(["#결제", "#회의", "#보고"])
        pay = page.locator('[data-tb-group="결제"]')
        expect(pay).to_contain_text("8건")
        expect(pay.locator("[data-memo]")).to_have_count(5)
        expect(page.locator('[data-tb-group="보고"]')).to_contain_text("아직 없습니다")
        expect(page.locator("#view")).to_contain_text("내 태그가 없는 메모")
        expect(page.locator('[data-tb-all="결제"]')).to_have_text("8건 모두 보기")
        pay.locator("[data-memo]").first.click()
        expect(page.locator("#detail")).to_be_visible()
        page.locator("#dClose").click()
        print("OK the tag view shows each of my tags with its count and latest memos, in settings order")

        page.locator('[data-tb-all="결제"]').click()
        expect(page.locator('[data-tab="feed"]')).to_have_class(re.compile(r"\bon\b"))
        expect(page.locator(".card[data-id]")).to_have_count(8)
        print("OK 'show all' opens my memos filtered by that tag")

        page.locator("#moreMenu > summary").click()
        page.locator('[data-tab="tagboard"]').click()
        page.locator('[data-tb-ai="회의"]').click()
        expect(page.locator('[data-tab="generate"]')).to_have_class(re.compile(r"\bon\b"))
        expect(page.locator("#genInput")).to_have_value(re.compile("#회의"))
        expect(page.locator("#view")).to_contain_text("#회의")
        print("OK 'AI 정리' opens AI 정리 scoped to that tag")

        page.locator("#moreMenu > summary").click()
        page.locator('[data-tab="tagboard"]').click()
        page.locator('[data-tb-adopt="개인"]').click()
        expect(page.locator('[data-tb-group="개인"]')).to_contain_text("1건")
        expect(page.locator("#view")).to_contain_text("내 태그가 없는 메모 1건")
        assert tags.exists("개인")
        print("OK a frequently used tag can be added to my tags right from the view")

        # 기간 요약도 같은 기준: 이 기간의 메모를 내 태그 순서대로 묶고, 내 태그 없는 메모는 따로
        store.add_memo("오늘 결제 장애 회고 #결제 #회의", enqueue_enrich=False)
        store.add_memo("오늘 결제 정산 확인 #결제", enqueue_enrich=False)
        store.add_memo("오늘 점심 메뉴", enqueue_enrich=False)
        r = organize.rollup("day")
        assert [(g["name"], g["count"]) for g in r["tag_groups"]] == [("결제", 2), ("회의", 1)], r["tag_groups"]
        assert r["untagged"]["count"] == 1 and r["untagged"]["memos"][0]["title"] == "오늘 점심 메뉴"
        assert "clusters" not in r
        prompt = organize.rollup_prompt(r)
        assert "## 내 태그별" in prompt and "- #결제 (2건)" in prompt and "내 태그 없음 (1건)" in prompt
        assert "자동 주제 묶음" not in prompt
        page.locator("#moreMenu > summary").click()
        page.locator('[data-tab="rollup"]').click()
        page.locator("#period").select_option("day")
        box = page.locator("#rollupTags")
        expect(box.locator("h3")).to_contain_text("내 태그별")
        expect(box.locator("[data-rollup-group] .tagchip")).to_have_text(["#결제", "#회의"])
        expect(box.locator('[data-rollup-group="결제"]')).to_contain_text("2건")
        expect(box).to_contain_text("내 태그 없음")
        expect(page.locator("#view")).not_to_contain_text("주제 묶음")
        expect(page.locator("#view pre.pack")).to_contain_text("## 내 태그별")
        box.locator('[data-rollup-tag="결제"]').click()
        expect(page.locator('[data-tab="feed"]')).to_have_class(re.compile(r"\bon\b"))
        expect(page.locator(".card[data-id]")).to_have_count(2)       # 오늘·#결제 만
        print("OK period summary groups that period's memos by my tags (and its AI briefing material too); "
              "'이 기간 메모 보기' filters by tag and period")

        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    main()
