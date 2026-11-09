"""AI tag scope across discovery, exact selection, generation and saved history."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-ai-scope-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from memoinall import ai_history, db, generate, llm, store, tags
from memoinall.api import app


class ScopeTests(unittest.TestCase):
    def setUp(self):
        db.init()
        db.execute("DELETE FROM ai_results")
        db.execute("DELETE FROM memos")
        db.execute("DELETE FROM memo_fts")
        db.execute("DELETE FROM tag_defs")
        store.invalidate_cache()
        self.a = store.add_memo("회의: 계약 일정 #업무", source="evernote", created_at="2026-01-01T10:00:00", enqueue_enrich=False)
        self.b = store.add_memo("회의: 다른 앱 기록 #업무", source="sticky", created_at="2026-01-02T10:00:00", enqueue_enrich=False)
        self.c = store.add_memo("회의: 제외할 개인 기록 #개인", source="evernote", created_at="2026-01-03T10:00:00", enqueue_enrich=False)
        self.d = store.add_memo("회의: 진행 상황 #업무", source="evernote", created_at="2026-01-04T10:00:00", enqueue_enrich=False)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.payload = {"instruction": "회의 정리", "queries": ["회의"], "tags": ["업무", "에버노트"]}

    def test_choices_include_imported_unregistered_and_registered_tags(self):
        tags.add("등록만한태그")
        for n in range(20):
            store.add_memo(f"별도 분류 #{n:02}분류", enqueue_enrich=False)
        result = {t["name"]: t for t in self.client.get("/api/generate/tags").json()["items"]}
        self.assertEqual(result["에버노트"]["count"], 3)
        self.assertEqual(result["스티커메모"]["count"], 1)
        self.assertIn("19분류", result)
        self.assertEqual(result["등록만한태그"]["count"], 0)
        self.assertEqual(tags.names(), ["등록만한태그"])

    def test_browse_and_search_apply_all_tags_before_pagination(self):
        wanted = {self.a["id"], self.d["id"]}
        for q in ("", "회의"):
            with self.subTest(query=q):
                params = {"q": q, "tags": "업무,에버노트", "limit": 1}
                first = self.client.get("/api/generate/memos", params=params).json()
                second = self.client.get("/api/generate/memos", params={**params, "offset": 1}).json()
                self.assertTrue(first["has_more"])
                self.assertFalse(second["has_more"])
                self.assertEqual({first["items"][0]["id"], second["items"][0]["id"]}, wanted)
        narrowed = self.client.get("/api/generate/memos", params={"tags": "업무,에버노트", "since": "2026-01-04"}).json()
        self.assertEqual([m["id"] for m in narrowed["items"]], [self.d["id"]])
        none = self.client.get("/api/generate/memos", params={"tags": "업무,개인"}).json()
        self.assertEqual(none, {"items": [], "has_more": False})

    def test_plan_generation_and_history_share_the_same_scope(self):
        plan = self.client.post("/api/generate/plan", json=self.payload).json()
        self.assertEqual({m["id"] for m in plan["candidates"]}, {self.a["id"], self.d["id"]})
        for memo in (self.b, self.c):
            self.assertNotIn(memo["body"], plan["prompt"])
        with patch.object(llm, "available", return_value=True), patch.object(llm, "complete", return_value="정리 결과") as model:
            result = self.client.post("/api/generate", json=self.payload)
        self.assertEqual(result.status_code, 200, result.text)
        data = result.json()
        self.assertEqual(model.call_count, 1)
        self.assertEqual(data["scope"]["tags"], ["업무", "에버노트"])
        self.assertEqual(ai_history.get(data["history_id"])["scope"], data["scope"])
        prompt = model.call_args.args[1]
        self.assertIn(self.a["body"], prompt)
        self.assertIn(self.d["body"], prompt)
        self.assertNotIn(self.b["body"], prompt)
        self.assertNotIn(self.c["body"], prompt)

    def test_selected_ids_cannot_bypass_tag_scope(self):
        with patch.object(llm, "complete") as model:
            for endpoint in ("/api/generate/plan", "/api/generate"):
                response = self.client.post(endpoint, json={**self.payload, "memo_ids": [self.a["id"], self.b["id"]]})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("찾을 범위", response.json()["detail"])
            model.assert_not_called()
        candidate = generate.candidate(self.a)
        selected = {**self.payload, "memo_ids": [self.a["id"]], "source_revisions": {str(self.a["id"]): candidate["revision"]}}
        plan = self.client.post("/api/generate/plan", json=selected)
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertEqual([m["id"] for m in plan.json()["sources"]], [self.a["id"]])

    def test_tag_removed_after_review_is_rejected_before_ai_call(self):
        candidate = generate.candidate(self.a)
        store.update_memo(self.a["id"], "회의: 태그를 지운 메모", enqueue_enrich=False)
        with patch.object(llm, "complete") as model:
            response = self.client.post("/api/generate", json={**self.payload, "memo_ids": [self.a["id"]],
                "source_revisions": {str(self.a["id"]): candidate["revision"]}})
        self.assertEqual(response.status_code, 400, response.text)
        model.assert_not_called()

    def test_no_matches_never_broadens_scope_or_generates_without_sources(self):
        with patch.object(llm, "available", return_value=True), patch.object(llm, "complete") as model:
            result = self.client.post("/api/generate", json={**self.payload, "tags": ["없는태그"]}).json()
        self.assertEqual(result["sources"], [])
        self.assertIsNone(result["output"])
        self.assertIn("찾지 못했습니다", result["reason"])
        model.assert_not_called()
        self.assertEqual(ai_history.list_results()["items"], [])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        db.close()
