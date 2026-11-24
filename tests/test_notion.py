"""Notion 임포터 테스트.  python tests/test_notion.py

진짜 Notion 없이도 확인할 수 있게, 실제 응답 모양을 흉내 낸 HTTP 서버를 띄워 실제 소켓으로 돌린다.
페이지 목록 넘기기·블록 중첩·표·동기화 블록·속성·태그·호출 제한(429)·오류 안내·미리보기 재사용·저장까지 본다.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

TMP = tempfile.TemporaryDirectory(prefix="memoinall-notion-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
os.environ.pop("NOTION_TOKEN", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from memoinall import db, importers, settings, store  # noqa: E402
from memoinall.api import app  # noqa: E402
from memoinall.importers import notion  # noqa: E402

GOOD = "ntn_good_token"
EMPTY = "ntn_no_pages"


def local(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().replace(tzinfo=None, microsecond=0).isoformat()


def text(content, href=None, kind="text"):
    return {"type": kind, "plain_text": content, "href": href}


def block(kind, content="", children=False, block_id=None, **extra):
    data = {"rich_text": [text(content)] if content else [], **extra}
    return {"object": "block", "id": block_id or f"b-{kind}-{content}", "type": kind, "has_children": children, kind: data}


PAGES = [
    {"object": "page", "id": "page-plan", "created_time": "2026-11-01T00:30:00.000Z",
     "last_edited_time": "2026-11-20T03:00:00.000Z", "archived": False,
     "parent": {"type": "database_id", "database_id": "db-1"},
     "properties": {
         "이름": {"type": "title", "title": [text("2026 교육 기획")]},
         "상태": {"type": "status", "status": {"name": "진행 중"}},
         "분류": {"type": "multi_select", "multi_select": [{"name": "교육"}, {"name": "AX 세미나"}]},
         "담당": {"type": "people", "people": [{"name": "김민수"}]},
         "마감": {"type": "date", "date": {"start": "2026-11-30", "end": None}},
         "완료": {"type": "checkbox", "checkbox": False},
         "예산": {"type": "number", "number": 1200},
     }},
    {"object": "page", "id": "page-notes", "created_time": "2026-10-02T01:00:00.000Z",
     "last_edited_time": "2026-11-10T01:00:00.000Z", "archived": False,
     "parent": {"type": "page_id", "page_id": "root"},
     "properties": {"title": {"type": "title", "title": [text("회의록")]}}},
    {"object": "page", "id": "page-trash", "created_time": "2026-09-01T00:00:00.000Z",
     "last_edited_time": "2026-11-05T00:00:00.000Z", "archived": True, "parent": {"type": "workspace"},
     "properties": {"title": {"type": "title", "title": [text("지운 페이지")]}}},
    {"object": "page", "id": "page-old", "created_time": "2025-01-01T00:00:00.000Z",
     "last_edited_time": "2025-02-01T00:00:00.000Z", "archived": False, "parent": {"type": "workspace"},
     "properties": {"title": {"type": "title", "title": [text("오래된 페이지")]}}},
]

BLOCKS = {
    "page-plan": [
        block("heading_2", "목표"),
        block("paragraph", "연수원 AX 세미나 기획안을 정리한다."),
        block("bulleted_list_item", "대상", children=True, block_id="b-target"),
        block("numbered_list_item", "일정 확정"),
        block("numbered_list_item", "강사 섭외"),
        block("to_do", "장소 예약", checked=False),
        block("to_do", "예산 승인", checked=True),
        block("toggle", "참고", children=True, block_id="b-toggle"),
        block("table", children=True, block_id="b-table", table_width=2),
        block("code", "print('hi')", language="python"),
        block("callout", "마감 엄수", icon={"type": "emoji", "emoji": "⚠️"}),
        block("synced_block", children=True, block_id="b-sync-copy",
              synced_from={"type": "block_id", "block_id": "b-sync-original"}),
        {"object": "block", "id": "b-child", "type": "child_page", "has_children": True,
         "child_page": {"title": "세부 일정"}},
        {"object": "block", "id": "b-image", "type": "image", "has_children": False,
         "image": {"caption": [text("배치도")], "type": "external", "external": {"url": "https://example.com/x.png"}}},
        {"object": "block", "id": "b-bookmark", "type": "bookmark", "has_children": False,
         "bookmark": {"url": "https://example.com/guide", "caption": [text("가이드")]}},
        block("paragraph", "자세한 건 링크 참고", ),
    ],
    "b-target": [block("bulleted_list_item", "신입 사원"), block("bulleted_list_item", "팀장")],
    "b-toggle": [block("paragraph", "작년 자료는 공유 폴더")],
    "b-table": [
        {"object": "block", "id": "r1", "type": "table_row", "has_children": False,
         "table_row": {"cells": [[text("항목")], [text("금액")]]}},
        {"object": "block", "id": "r2", "type": "table_row", "has_children": False,
         "table_row": {"cells": [[text("강사료")], [text("800")]]}},
    ],
    "b-sync-original": [block("paragraph", "동기화된 공지")],
    "page-notes": [block("paragraph", "결정: 매주 화요일 정기 회의"),
                   {"object": "block", "id": "b-link", "type": "paragraph", "has_children": False,
                    "paragraph": {"rich_text": [text("문서", href="https://example.com/doc")]}}],
    "page-old": [block("paragraph", "오래된 내용")],
}


class Handler(BaseHTTPRequestHandler):
    calls: list[str] = []
    throttle_once: set[str] = set()

    def log_message(self, *a):
        pass

    def _send(self, code, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _guard(self):
        Handler.calls.append(f"{self.command} {self.path}")
        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        if self.headers.get("Notion-Version") != notion.NOTION_VERSION:
            self._send(400, {"code": "missing_version"})
            return None
        if token not in (GOOD, EMPTY):
            self._send(401, {"object": "error", "code": "unauthorized"})
            return None
        path = urlparse(self.path).path
        if path in Handler.throttle_once:
            Handler.throttle_once.discard(path)
            self._send(429, {"code": "rate_limited"}, {"Retry-After": "1"})
            return None
        return token

    def do_GET(self):
        token = self._guard()
        if token is None:
            return
        url = urlparse(self.path)
        if url.path == "/v1/users/me":
            self._send(200, {"object": "user", "type": "bot", "name": "memoinall",
                             "bot": {"workspace_name": "우리 팀"}})
        elif url.path == "/v1/databases/db-1":
            self._send(200, {"object": "database", "title": [text("교육 과제")]})
        elif url.path.startswith("/v1/blocks/") and url.path.endswith("/children"):
            block_id = url.path.split("/")[3]
            if block_id == "page-missing":
                self._send(404, {"code": "object_not_found"})
                return
            items = BLOCKS.get(block_id, [])
            cursor = int((parse_qs(url.query).get("start_cursor") or ["0"])[0])
            size = 10   # 실제는 100 — 넘기기를 확인하려고 작게
            chunk = items[cursor:cursor + size]
            more = cursor + size < len(items)
            self._send(200, {"results": chunk, "has_more": more, "next_cursor": str(cursor + size) if more else None})
        else:
            self._send(404, {"code": "object_not_found"})

    def do_POST(self):
        token = self._guard()
        if token is None:
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        if urlparse(self.path).path != "/v1/search":
            self._send(404, {"code": "object_not_found"})
            return
        assert body["filter"] == {"property": "object", "value": "page"}, body
        pages = [] if token == EMPTY else Handler.pages
        cursor = int(body.get("start_cursor") or 0)
        size = 2      # 넘기기 확인
        chunk = pages[cursor:cursor + size]
        more = cursor + size < len(pages)
        self._send(200, {"results": chunk, "has_more": more, "next_cursor": str(cursor + size) if more else None})


Handler.pages = PAGES


class NotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.api = patch.object(notion, "API", f"http://127.0.0.1:{cls.server.server_port}/v1")
        cls.api.start()
        cls.fast = patch.object(notion, "MIN_INTERVAL", 0)
        cls.fast.start()

    @classmethod
    def tearDownClass(cls):
        cls.api.stop()
        cls.fast.stop()
        cls.server.shutdown()

    def setUp(self):
        Handler.calls = []
        Handler.throttle_once = set()
        Handler.pages = PAGES
        notion._cache.clear()

    def test_connection_messages(self):
        ok = notion.NotionImporter(GOOD).test_connection()
        self.assertTrue(ok["ok"])
        self.assertIn("‘memoinall’ · 우리 팀", ok["message"])
        self.assertIn("읽을 수 있는 페이지 2개 이상", ok["message"])   # 첫 쪽만 센다(가짜 서버는 2개씩 넘긴다)
        empty = notion.NotionImporter(EMPTY).test_connection()
        self.assertTrue(empty["ok"] and empty["pages"] == 0)
        self.assertIn("••• → 연결(Connections)", empty["message"])
        wrong = notion.NotionImporter("ntn_wrong").test_connection()
        self.assertFalse(wrong["ok"])
        self.assertIn("토큰이 올바르지 않습니다", wrong["message"])
        korean = notion.NotionImporter("토큰").test_connection()
        self.assertIn("한글이나 공백", korean["message"])
        missing = notion.NotionImporter("").test_connection()
        self.assertFalse(missing["ok"])
        self.assertIn("토큰이 설정되지 않았습니다", missing["message"])

    def test_pages_become_memos_with_original_times_properties_and_blocks(self):
        reader = notion.NotionImporter(GOOD)
        notes = {n.external_id: n for n in reader.read()}
        self.assertEqual(set(notes), {"page:page-plan", "page:page-notes", "page:page-old"})   # 휴지통 페이지 제외
        plan = notes["page:page-plan"]
        lines = plan.body.splitlines()
        self.assertEqual(lines[0], "2026 교육 기획")
        for line in ("상태: 진행 중", "분류: 교육, AX 세미나", "담당: 김민수", "마감: 2026-11-30", "예산: 1200"):
            self.assertIn(line, lines)
        self.assertNotIn("완료:", plan.body)                                    # 체크 안 된 체크박스는 적지 않는다
        expected = [
            "## 목표", "연수원 AX 세미나 기획안을 정리한다.",
            "- 대상", "  - 신입 사원", "  - 팀장",
            "1. 일정 확정", "2. 강사 섭외",
            "[ ] 장소 예약", "[x] 예산 승인",
            "▸ 참고", "  작년 자료는 공유 폴더",
            "항목 | 금액", "강사료 | 800",
            "```python", "print('hi')", "```",
            "⚠️ 마감 엄수", "동기화된 공지",
            "↳ 하위 페이지: 세부 일정", "[이미지] 배치도", "가이드 (https://example.com/guide)",
            "자세한 건 링크 참고",
        ]
        body_lines = [line for line in lines if line.strip()]
        positions = [body_lines.index(line) for line in expected]
        self.assertEqual(positions, sorted(positions), plan.body)              # 순서도 원래대로
        self.assertEqual(plan.created_at, local("2026-11-01T00:30:00.000Z"))
        self.assertEqual(plan.updated_at, local("2026-11-20T03:00:00.000Z"))
        self.assertEqual(plan.tags, ["교육_과제", "교육", "AX_세미나"])
        self.assertTrue(plan.warnings and "이미지" in plan.warnings[0])
        self.assertIn("문서 (https://example.com/doc)", notes["page:page-notes"].body)
        self.assertEqual(notes["page:page-notes"].tags, [])
        # 하위 페이지는 본문을 따로 읽지 않는다(그 페이지가 따로 메모가 된다)
        self.assertFalse(any("/blocks/b-child/" in c for c in Handler.calls))
        self.assertTrue(any("/blocks/b-sync-original/" in c for c in Handler.calls))

    def test_since_limit_retry_and_errors(self):
        recent = notion.NotionImporter(GOOD, since="2026-11-15").read()
        self.assertEqual([n.external_id for n in recent], ["page:page-plan"])   # 최근 수정순이라 그 뒤는 안 읽는다
        limited = notion.NotionImporter(GOOD, limit=1)
        self.assertEqual(len(limited.read()), 1)
        self.assertIn("최대 1개", limited.warnings[0])
        Handler.throttle_once = {"/v1/search"}
        with patch.object(notion.time, "sleep") as sleep:
            self.assertEqual(len(notion.NotionImporter(GOOD).read()), 3)
        self.assertTrue(any(call.args[0] >= 1 for call in sleep.call_args_list))   # Retry-After 를 지킨다
        Handler.pages = [*PAGES[:1], {**PAGES[1], "id": "page-missing"}]
        broken = notion.NotionImporter(GOOD)
        notes = broken.read()
        self.assertEqual([n.external_id for n in notes], ["page:page-plan"])
        self.assertIn("읽지 못한 페이지", broken.warnings[0])
        self.assertIn("연결을 추가했는지", broken.warnings[0])
        with self.assertRaisesRegex(notion.NotionError, "토큰이 올바르지 않습니다"):
            notion.NotionImporter("ntn_wrong").read()

    def test_import_saves_once_and_run_reuses_the_preview(self):
        db.execute("DELETE FROM memos")
        preview = importers.run_import(importers.build_notion(notion_token=GOOD), dry_run=True)
        self.assertEqual((preview.found, preview.imported, preview.error), (3, 3, ""))
        before = len(Handler.calls)
        saved = importers.run_import(importers.build_notion(notion_token=GOOD, notion_reuse_preview=True),
                                     dry_run=False, background_enrich=False)
        self.assertEqual(saved.imported, 3)
        self.assertEqual(len(Handler.calls), before)                           # 방금 미리 본 것을 다시 쓴다
        memo = store.get_memo(store.find_by_external("notion", "page:page-plan"))
        self.assertEqual(memo["source"], "notion")
        self.assertEqual(memo["created_at"], local("2026-11-01T00:30:00.000Z"))
        self.assertIn("#Notion", memo["body"])
        self.assertEqual(set(memo["facets"]["tag"]), {"교육_과제", "교육", "AX_세미나", "Notion"})
        again = importers.run_import(importers.build_notion(notion_token=GOOD), dry_run=False, background_enrich=False)
        self.assertEqual((again.imported, again.skipped_existing), (0, 3))     # 다시 가져와도 중복 없음
        self.assertGreater(len(Handler.calls), before)                         # 미리보기는 늘 새로 읽는다
        # Notion 에서 고친 뒤 '이미 가져온 메모도 갱신'
        BLOCKS["page-notes"].append(block("paragraph", "추가된 안건"))
        try:
            updated = importers.run_import(importers.build_notion(notion_token=GOOD), dry_run=False,
                                           background_enrich=False, update_existing=True)
        finally:
            BLOCKS["page-notes"].pop()
        self.assertEqual(updated.updated, 1)
        self.assertIn("추가된 안건", store.get_memo(store.find_by_external("notion", "page:page-notes"))["body"])

    def test_http_api_uses_typed_values_and_saved_token(self):
        with TestClient(app) as client:
            r = client.post("/api/import/notion/test", json={"token": GOOD}).json()
            self.assertTrue(r["ok"], r)
            self.assertFalse(client.post("/api/import/notion/test", json={}).json()["ok"])       # 저장된 토큰 없음
            for bad in ({"since": "어제"}, {"limit": "많이"}, {"limit": 0}, {"token": 3}):
                self.assertEqual(client.post("/api/import/notion/test", json=bad).status_code, 400, bad)
                self.assertEqual(client.post("/api/import/preview", json={"source": "notion", "notion": bad}).status_code, 400)
            client.put("/api/settings", json={"values": {"import.notion.token": GOOD}})
            self.assertTrue(settings.public_view()["settings"]["import.notion.token"]["secret"])
            self.assertNotIn(GOOD, json.dumps(client.get("/api/settings").json()))             # 토큰 원문은 안 나간다
            preview = client.post("/api/import/preview", json={"source": "notion", "notion": {"limit": 2}}).json()
            self.assertEqual(preview["results"][0]["found"], 2)
            sources = {s["name"]: s for s in client.get("/api/import/sources").json()["sources"]}
            self.assertTrue(sources["notion"]["available"] and sources["notion"]["network"])
            self.assertEqual(sources["notion"]["source_tag"], "Notion")
            client.put("/api/settings", json={"values": {"import.notion.token": ""}})


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        db.close()
