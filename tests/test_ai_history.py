"""AI document persistence, revision safety and exact source selection."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-ai-history-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from memoinall import ai_history, db, generate, llm, store
from memoinall.api import app


class HistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(app)
        cls.client = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)
        db.close()

    def setUp(self):
        db.execute("DELETE FROM ai_results")
        self.a = store.add_memo("회의에서 비용을 검토하기로 결정", enqueue_enrich=False)
        self.b = store.add_memo("공유 대상에서 뺄 개인 메모", enqueue_enrich=False)
        self.payload = {"instruction": "회의 내용을 정리해줘", "memo_ids": [self.a["id"], self.b["id"]]}

    def make(self):
        with patch.object(llm, "available", return_value=True), patch.object(llm, "complete", return_value=f"비용 검토 예정 [M{self.a['id']}]"):
            r = self.client.post("/api/generate", json=self.payload)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def post(self, result, operation, **kwargs):
        return self.client.post(f"/api/generate/history/{result['history_id']}/{operation}",
                                json={"version": result["version"], **kwargs})

    def test_new_result_persists_across_connections_without_creating_a_memo(self):
        before = store.stats()["memos"]
        r = self.make()
        db.close()
        loaded = ai_history.get(r["history_id"])
        self.assertEqual(loaded["output"], r["output"])
        self.assertEqual(len(loaded["versions"]), 1)
        self.assertEqual(store.stats()["memos"], before)
        history = self.client.get("/api/generate/history").json()
        self.assertEqual(history["items"][0]["id"], r["history_id"])

    def test_draft_restore_commit_and_stale_write_protection(self):
        r = self.make()
        url = f"/api/generate/history/{r['history_id']}/draft"
        self.assertEqual(self.client.put(url, json={"version": 1, "body": "직접 수정한 내용"}).status_code, 200)
        self.assertEqual(ai_history.get(r["history_id"])["draft_body"], "직접 수정한 내용")
        saved = self.post(r, "save", body="직접 수정한 내용").json()
        self.assertEqual(saved["version"], 2)
        self.assertEqual(ai_history.get(r["history_id"], 1)["output"], r["output"])
        self.assertEqual(self.client.put(url, json={"version": 1, "body": "늦은 요청"}).status_code, 409)
        self.assertEqual(self.post(r, "save", body="늦은 수정").status_code, 409)
        self.assertEqual(self.post(saved, "save", body=saved["output"]).json()["version"], 2)

    def test_restore_keeps_unfinished_edits_and_original_source_snapshot(self):
        r = self.make()
        self.client.put(f"/api/generate/history/{r['history_id']}/draft", json={"version": 1, "body": "수정 중 내용"})
        restored = self.post(r, "restore", target_version=1).json()
        self.assertEqual(restored["version"], 3)
        self.assertEqual(restored["output"], r["output"])
        self.assertEqual(ai_history.get(r["history_id"], 2)["output"], "수정 중 내용")
        self.assertEqual(restored["sources"], r["sources"])

    def test_save_as_memo_is_explicit_idempotent_and_preserves_sources(self):
        r = self.make()
        saved = self.post(r, "memo").json()
        self.assertEqual(saved, self.post(r, "memo").json())
        memo = store.get_memo(saved["memo_id"])
        self.assertEqual(memo["source"], "ai")
        self.assertIn("AI로 정리한 메모", memo["body"])
        self.assertIn(f"[M{self.a['id']}]", memo["body"])
        store.update_memo(memo["id"], "내 메모에서 별도로 수정", enqueue_enrich=False)
        self.post(r, "memo")
        self.assertEqual(store.get_memo(memo["id"])["body"], "내 메모에서 별도로 수정")
        store.delete_memo(memo["id"])
        fresh = self.post(r, "memo").json()
        self.assertNotEqual(fresh["memo_id"], memo["id"])

    def test_refine_uses_edited_text_and_original_snapshots_even_after_source_deletion(self):
        r = self.make()
        store.delete_memo(self.a["id"])
        with patch.object(llm, "available", return_value=True), patch.object(llm, "complete", return_value="더 짧은 정리") as model, patch.object(generate, "multi_search", side_effect=AssertionError("must not search")):
            refined = self.post(r, "refine", body="사용자가 고친 문장", request="더 짧게").json()
        prompt = model.call_args.args[1]
        self.assertIn("사용자가 고친 문장", prompt)
        self.assertIn(self.a["body"], prompt)
        self.assertEqual(refined["version"], 3)
        self.assertEqual(ai_history.get(r["history_id"], 2)["output"], "사용자가 고친 문장")
        self.assertEqual(refined["sources"], r["sources"])

    def test_refine_failure_keeps_current_text_and_can_retry(self):
        r = self.make()
        with patch.object(llm, "available", return_value=True), patch.object(llm, "complete", side_effect=llm.ProviderError("연결 실패")):
            failed = self.post(r, "refine", body="보존해야 할 직접 수정", request="보고용으로").json()
        self.assertEqual(failed["output"], "보존해야 할 직접 수정")
        self.assertIn("연결 실패", failed["refine_error"])
        self.assertEqual(failed["version"], 2)

    def test_failed_or_unconfigured_generation_does_not_store_an_empty_result(self):
        with patch.object(llm, "available", return_value=False):
            self.client.post("/api/generate", json=self.payload)
        self.assertEqual(ai_history.list_results()["items"], [])

    def test_only_checked_memos_enter_the_prompt_and_changed_sources_are_rejected(self):
        plan = self.client.post("/api/generate/plan", json=self.payload).json()
        a = next(x for x in plan["candidates"] if x["id"] == self.a["id"])
        payload = {**self.payload, "memo_ids": [a["id"]], "source_revisions": {str(a["id"]): a["revision"]}}
        with patch.object(llm, "available", return_value=True), patch.object(llm, "complete", return_value="정리") as model:
            r = self.client.post("/api/generate", json=payload)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(self.b["body"], model.call_args.args[1])
        store.update_memo(self.a["id"], "선택 후 바뀐 내용", enqueue_enrich=False)
        with patch.object(llm, "complete") as model:
            self.assertEqual(self.client.post("/api/generate", json=payload).status_code, 400)
            model.assert_not_called()
        self.assertEqual(self.client.post("/api/generate", json={**payload, "memo_ids": [], "source_revisions": {}}).status_code, 400)

    def test_add_candidates_and_history_pagination(self):
        candidates = self.client.get("/api/generate/memos?limit=1").json()
        self.assertEqual(len(candidates["items"]), 1)
        self.assertTrue(candidates["has_more"])
        self.make(); self.make()
        first = self.client.get("/api/generate/history?limit=1").json()
        second = self.client.get("/api/generate/history?limit=1&offset=1").json()
        self.assertTrue(first["has_more"])
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        self.assertEqual(self.client.get("/api/generate/history?q=%25").json()["items"], [])

    def test_invalid_edits_and_versions_do_not_change_saved_text(self):
        r = self.make()
        for body in ("", " ", None, 7, "x" * 200001):
            self.assertEqual(self.post(r, "save", body=body).status_code, 400)
        self.assertEqual(self.post(r, "refine", body="본문", request="").status_code, 400)
        self.assertEqual(self.post(r, "restore", target_version=999).status_code, 404)
        self.assertEqual(ai_history.get(r["history_id"])["version"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
