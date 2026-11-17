"""Optional AI task extraction with fake providers; never calls a real API."""
from __future__ import annotations

import gc
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from memoinall import config, db, embed, providers, settings, store, todo_ai, todos
from memoinall.api import app


async def no_prepare():
    pass


class TodoAiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="memoinall-todo-ai-", ignore_cleanup_errors=True)
        home = Path(self.tmp.name)
        db.close()
        self.provider = MagicMock()
        self.patchers = [patch.object(config, "HOME", home), patch.object(config, "DB_PATH", home / "memo.db"),
                         patch.object(embed, "_current", embed.HashEmbedder()), patch.object(store, "start_worker"),
                         patch("memoinall.api._prepare_search_later", new=no_prepare),
                         patch.object(providers, "build", return_value=self.provider),
                         patch.dict(os.environ, {"OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "GEMINI_API_KEY": ""})]
        for patcher in self.patchers:
            patcher.start()
        db.init()
        settings.set_many({"llm.provider": "openai", "llm.openai.api_key": "private-test-key", "llm.openai.model": "test-model"})
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        db.close()
        for patcher in reversed(self.patchers):
            patcher.stop()
        gc.collect()
        self.tmp.cleanup()

    def add(self, body, created="2026-11-16T09:00:00", **kwargs):
        return store.add_memo(body, created_at=created, enqueue_enrich=False, **kwargs)

    def suggest(self, memo, *, todo_id=None, quote=None, **changes):
        return {"memo_id": memo["id"], "todo_id": todo_id, "title": "설명회 자료 보내기", "status": "open",
                "due": "2026-11-20", "review_on": None, "reason": "자료를 보내기로 한 약속이 기록되어 있습니다.",
                "evidence": [{"memo_id": memo["id"], "quote": quote or memo["body"]}], **changes}

    def preview(self, suggestions, **kwargs):
        self.provider.complete_json.return_value = {"suggestions": suggestions}
        response = self.client.post("/api/todos/ai/preview", json={"days": 30, "today": "2026-11-16", **kwargs})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def apply(self, plan, selected=None):
        return self.client.post(f"/api/todos/ai/{plan['id']}/apply", json={"selected": selected if selected is not None else list(range(len(plan["suggestions"])))})

    def natural(self):
        return self.add("교육팀과 통화해서 금요일까지 설명회 자료를 보내기로 했다.")

    def test_no_key_disables_optional_feature_without_provider_calls(self):
        settings.set_many({"llm.openai.api_key": "", "llm.anthropic.api_key": "different-provider-key"})
        memo = self.add("보고서 작성해야 함")
        result = self.client.get("/api/todos").json()
        self.assertFalse(result["ai"]["ready"])
        self.assertEqual(len(result["items"]), 1)
        response = self.client.post("/api/todos/ai/preview", json={})
        self.assertEqual(response.status_code, 503)
        self.provider.complete_json.assert_not_called()
        self.assertEqual(store.get_memo(memo["id"])["body"], memo["body"])

    def test_ordinary_reads_and_reindex_never_call_ai(self):
        memo = self.natural()
        self.client.get("/api/todos")
        store.enrich(memo["id"])
        self.client.get("/api/todos")
        self.provider.complete_json.assert_not_called()

    def test_preview_is_readonly_and_keeps_exact_evidence(self):
        memo = self.natural()
        plan = self.preview([self.suggest(memo)])
        self.assertFalse(db.query("SELECT * FROM todos"))
        self.assertEqual(plan["suggestions"][0]["evidence"][0]["quote"], memo["body"])
        self.assertEqual(plan["model"], "test-model")
        self.assertNotIn("private-test-key", json.dumps(plan))
        self.assertNotIn("memo_hashes", plan)
        self.assertNotIn("task_hash", plan["suggestions"][0])
        prompt = json.loads(self.provider.complete_json.call_args.args[1])
        self.assertEqual(prompt["memos"][0]["id"], memo["id"])

    def test_apply_is_explicit_idempotent_and_survives_reindex(self):
        memo = self.natural()
        plan = self.preview([self.suggest(memo)])
        self.assertEqual(self.apply(plan).status_code, 200)
        self.assertEqual(self.apply(plan).status_code, 200)
        task = dict(db.one("SELECT * FROM todos"))
        self.assertEqual((task["origin"], task["due"]), ("ai", "2026-11-20"))
        store.enrich(memo["id"])
        self.assertEqual(len(todos.board()["items"]), 1)
        self.assertEqual(todos.board()["items"][0]["id"], task["id"])
        todos.update(task["id"], {"status": "done"})
        store.enrich(memo["id"])
        self.assertEqual(todos.board(view="done")["total"], 1)
        self.assertEqual(self.preview([self.suggest(memo)])["suggestions"], [])

    def test_rule_update_can_use_later_memo_as_completion_evidence(self):
        original = self.add("[ ] 교육계획안 작성")
        later = self.add("교육계획안 작성 완료. 검토를 요청했다.")
        todos.refresh_pending()
        task = db.one("SELECT * FROM todos WHERE memo_id=?", (original["id"],))
        proposal = self.suggest(original, todo_id=task["id"], title="교육계획안 작성", due=None, status="done",
                                evidence=[{"memo_id": original["id"], "quote": original["body"]},
                                          {"memo_id": later["id"], "quote": "교육계획안 작성 완료."}])
        plan = self.preview([proposal])
        self.assertEqual(self.apply(plan).status_code, 200)
        store.enrich(original["id"])
        item = todos.board(view="done")["items"][0]
        self.assertEqual(len(item["ai_sources"]), 2)
        self.assertEqual(item["manual_fields"], [])
        self.assertIn("status", item["ai_fields"])

    def test_manual_values_survive_ai_but_ai_values_can_be_refined_again(self):
        original = self.add("금요일까지 보고서 보내야 함")
        todos.refresh_pending()
        task = db.one("SELECT * FROM todos")
        todos.update(task["id"], {"title": "내가 정한 이름", "due": "2026-12-01"})
        proposal = self.suggest(original, todo_id=task["id"], status="waiting")
        plan = self.preview([proposal])
        self.assertEqual(plan["suggestions"][0]["changes"], {"status": "waiting"})
        self.assertEqual(self.apply(plan).status_code, 200)
        proposal["status"] = "open"
        second = self.preview([proposal])
        self.assertEqual(second["suggestions"][0]["changes"], {"status": "open"})
        self.assertEqual(self.apply(second).status_code, 200)
        actual = db.one("SELECT * FROM todos")
        self.assertEqual((actual["title"], actual["due"], actual["status"]), ("내가 정한 이름", "2026-12-01", "open"))

    def test_undo_restores_original_decisions_and_removes_only_added_tasks(self):
        memo = self.natural()
        existing = self.add("보고서 검토해야 함")
        todos.refresh_pending()
        task = db.one("SELECT * FROM todos WHERE memo_id=?", (existing["id"],))
        before = dict(task)
        plan = self.preview([self.suggest(memo), self.suggest(existing, todo_id=task["id"], due=None, title="보고서 검토하기", status="waiting")])
        self.assertEqual(self.apply(plan).status_code, 200)
        url = f"/api/todos/ai/{plan['id']}/undo"
        self.assertEqual(self.client.post(url).status_code, 200)
        self.assertEqual(self.client.post(url).status_code, 200)
        self.assertEqual(len(db.query("SELECT * FROM todos")), 1)
        self.assertEqual(dict(db.one("SELECT * FROM todos")), before)
        self.assertEqual(self.apply(plan).status_code, 409)

    def test_reset_user_edit_restores_ai_value_and_reindex_keeps_it(self):
        memo = self.add("금요일까지 보고서 보내야 함")
        todos.refresh_pending()
        task = db.one("SELECT * FROM todos")
        self.apply(self.preview([self.suggest(memo, todo_id=task["id"], status="waiting")]))
        todos.update(task["id"], {"status": "open"})
        reset = todos.update(task["id"], {"reset": ["status"]})
        self.assertEqual(reset["status"], "waiting")
        self.assertEqual(reset["manual_fields"], [])
        store.enrich(memo["id"])
        self.assertEqual(db.one("SELECT status FROM todos")["status"], "waiting")

    def test_stale_memo_rejects_entire_batch_without_partial_apply(self):
        a, b = self.natural(), self.add("금요일까지 자료를 공유하기로 했다.")
        plan = self.preview([self.suggest(a), self.suggest(b, title="자료 공유하기")])
        store.update_memo(b["id"], "일정이 바뀌었다", enqueue_enrich=False)
        self.assertEqual(self.apply(plan).status_code, 409)
        self.assertFalse(db.query("SELECT * FROM todos"))

    def test_stale_task_and_modified_after_apply_cannot_be_overwritten(self):
        memo = self.add("금요일까지 보고서 보내야 함")
        todos.refresh_pending()
        task = db.one("SELECT * FROM todos")
        plan = self.preview([self.suggest(memo, todo_id=task["id"], status="waiting")])
        todos.update(task["id"], {"review_on": "2026-11-17"})
        self.assertEqual(self.apply(plan).status_code, 409)
        fresh = self.preview([self.suggest(memo, todo_id=task["id"], status="waiting")])
        self.assertEqual(self.apply(fresh).status_code, 200)
        todos.update(task["id"], {"title": "적용 후 직접 수정"})
        self.assertEqual(self.client.post(f"/api/todos/ai/{fresh['id']}/undo").status_code, 409)
        self.assertEqual(db.one("SELECT title FROM todos")["title"], "적용 후 직접 수정")

    def test_hallucinated_sources_dates_and_invalid_shapes_are_filtered(self):
        memo = self.natural()
        invalid = [self.suggest(memo, memo_id=999), self.suggest(memo, todo_id=True),
                   self.suggest(memo, quote="메모에 없는 문장"), self.suggest(memo, due="2027-01-01"),
                   self.suggest(memo, evidence=[]), self.suggest(memo, status=[])]
        plan = self.preview(invalid)
        self.assertEqual(plan["suggestions"], [])
        self.assertEqual(plan["rejected"], len(invalid))
        self.assertFalse(db.query("SELECT * FROM todos"))

    def test_whole_response_and_provider_failures_leave_tasks_intact(self):
        self.natural()
        for value in (None, {"answer": "not a plan"}, {"suggestions": "wrong"}):
            self.provider.complete_json.return_value = value
            response = self.client.post("/api/todos/ai/preview", json={"today": "2026-11-16"})
            self.assertEqual(response.status_code, 502)
        self.provider.complete_json.side_effect = RuntimeError("secret raw error")
        response = self.client.post("/api/todos/ai/preview", json={"today": "2026-11-16"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("secret raw error", response.text)
        self.assertFalse(db.query("SELECT * FROM todos"))

    def test_period_archive_and_context_bounds_are_enforced(self):
        self.add("범위 밖", created="2025-01-01T00:00:00")
        archived = self.add("보관된 메모")
        store.set_flag(archived["id"], archived=True)
        for index in range(35):
            self.add(f"긴 자료 {index} " + "참고 내용 " * 700)
        plan = self.preview([])
        prompt = json.loads(self.provider.complete_json.call_args.args[1])
        self.assertLessEqual(len(prompt["memos"]), todo_ai.MAX_MEMOS)
        self.assertLessEqual(sum(len(m["body"]) for m in prompt["memos"]), todo_ai.MAX_CHARS)
        self.assertGreater(plan["scope"]["omitted_memos"], 0)
        self.assertGreater(plan["scope"]["partial_memos"], 0)
        self.assertTrue(all(m["id"] != archived["id"] for m in prompt["memos"]))

    def test_selection_validation_and_unselected_proposals(self):
        a, b = self.natural(), self.add("금요일까지 다른 자료를 보내기로 했다.")
        plan = self.preview([self.suggest(a), self.suggest(b, title="다른 자료 보내기")])
        for selected in ([], [True], [0, 0], [99], "0"):
            self.assertEqual(self.apply(plan, selected).status_code, 400)
        self.assertEqual(self.apply(plan, [1]).status_code, 200)
        self.assertEqual(db.one("SELECT memo_id FROM todos")["memo_id"], b["id"])
        self.assertEqual(self.apply(plan, [0]).status_code, 409)

    def test_new_rule_does_not_duplicate_ai_task_and_source_edits_are_visible(self):
        memo = self.natural()
        self.apply(self.preview([self.suggest(memo)]))
        ai = dict(db.one("SELECT * FROM todos"))
        with db.connect() as conn:
            todos.sync(conn, dict(db.one("SELECT * FROM memos")), [{"text": ai["text"], "identity_key": ai["identity_key"]}])
        self.assertEqual(len(db.query("SELECT * FROM todos")), 1)
        store.update_memo(memo["id"], "약속이 바뀌었다", enqueue_enrich=False)
        self.assertTrue(todos.board()["items"][0]["source_changed"])

    def test_ollama_uses_existing_local_configuration_without_key(self):
        settings.set_many({"llm.provider": "ollama"})
        memo = self.natural()
        plan = self.preview([self.suggest(memo)])
        self.assertIn("Ollama", plan["provider"])


if __name__ == "__main__":
    unittest.main()
