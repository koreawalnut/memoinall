"""Task dates, ownership, lifecycle, migration and API regression tests."""
from __future__ import annotations

import gc
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from memoinall import config, db, embed, organize, store, todo_extract, todos
from memoinall.api import app

BASE = datetime(2026, 11, 16, 10)


class ExtractionTests(unittest.TestCase):
    def one(self, text, **kwargs):
        result = todo_extract.extract_todos(text, BASE, **kwargs)
        self.assertEqual(len(result), 1, result)
        return result[0]

    def test_deadline_not_first_context_date(self):
        item = self.one("11월 10일 회의 내용을 11월 20일까지 정리해야 함")
        self.assertEqual(item["due"], "2026-11-20")
        self.assertIn("11월 10일 회의", item["title"])

    def test_weekday_and_relative_deadlines(self):
        for phrase, expected in [("금요일", "2026-11-20"), ("다음 주 화요일", "2026-11-24"),
                                 ("내일", "2026-11-17"), ("다다음주 월요일", "2026-11-30")]:
            with self.subTest(phrase=phrase):
                item = self.one(phrase + "까지 보고서 보내야 함")
                self.assertEqual(item["due"], expected)
                self.assertEqual(item["title"], "보고서 보내기")

    def test_unqualified_weekday_is_next_occurrence(self):
        base = datetime(2026, 11, 21)
        self.assertEqual(todo_extract.extract_todos("금요일까지 보고서 보내야 함", base)[0]["due"], "2026-11-27")

    def test_week_precision_does_not_invent_deadline(self):
        for phrase in ("다음 주쯤", "다음 주까지"):
            item = self.one(phrase + " 자료 검토해야 함")
            self.assertIsNone(item["due"])
            self.assertEqual((item["scheduled_for"], item["schedule_end"]), ("2026-11-23", "2026-11-29"))

    def test_scheduled_day_is_not_deadline(self):
        item = self.one("내일 보고서 작성하기")
        self.assertIsNone(item["due"])
        self.assertEqual(item["scheduled_for"], "2026-11-17")

    def test_dated_shorthand_does_not_require_should_do_suffix(self):
        for text in ("금요일까지 보고서 제출", "금요일까지 보고서 제출 예정"):
            item = self.one(text)
            self.assertEqual(item["due"], "2026-11-20")
        item = self.one("내일 자료 검토")
        self.assertEqual(item["scheduled_for"], "2026-11-17")
        self.assertIsNone(item["due"])

    def test_planning_and_deadline_can_coexist(self):
        item = self.one("오늘 초안 작성해서 금요일까지 보고서 보내야 함")
        self.assertEqual(item["scheduled_for"], "2026-11-16")
        self.assertEqual(item["due"], "2026-11-20")

    def test_past_context_date_is_not_deadline(self):
        item = self.one("11월 10일 회의 내용을 정리해야 함")
        self.assertIsNone(item["due"])
        self.assertIsNone(item["scheduled_for"])

    def test_multiple_deadlines_and_event_without_date_need_review(self):
        for text in ("화요일까지 초안, 금요일까지 최종본 보내야 함", "회의 전까지 안건 정리해야 함"):
            with self.subTest(text=text):
                item = self.one(text)
                self.assertEqual(item["status"], "review")
                self.assertIsNone(item["due"])

    def test_waiting_for_reply_uses_followup_day(self):
        item = self.one("민수님에게 견적 요청함. 목요일까지 받기로 함")
        self.assertEqual(item["status"], "waiting")
        self.assertEqual(item["review_on"], "2026-11-19")
        self.assertIsNone(item["due"])
        self.assertIn("회신 확인", item["title"])
        self.assertIn("목요일", item["text"])

    def test_waiting_word_inside_an_action_does_not_block_it(self):
        self.assertEqual(self.one("견적 대기 시간을 확인해야 함")["status"], "open")
        self.assertEqual(self.one("견적 회신 대기 중")["status"], "waiting")

    def test_conditional_work_waits_without_invented_date(self):
        item = self.one("견적 오면 비교해서 업체 정해야 함")
        self.assertEqual(item["status"], "waiting")
        self.assertIn("견적 오면", item["reason"])
        self.assertFalse(item["title"].startswith("견적 오면"))
        self.assertIsNone(item["due"])

    def test_other_owners_need_review_but_collaborators_do_not(self):
        for subject in ("김대리가", "민수님이", "@담당자가"):
            self.assertEqual(self.one(subject + " 금요일까지 견적 보내야 함")["status"], "review")
        self.assertEqual(self.one("민수님에게 자료 보내야 함")["status"], "open")
        self.assertEqual(self.one("내일 김대리가 견적 보내야 함")["status"], "review")

    def test_imported_prose_needs_review_explicit_checklist_does_not(self):
        self.assertEqual(self.one("자료 준비 필요함", source="document")["status"], "review")
        self.assertEqual(self.one("[ ] 자료 준비", source="document")["status"], "open")

    def test_negative_and_formal_prose_are_not_tasks(self):
        for text in ("자료 제출은 안 해도 됨", "검토 필요 없음", "자료를 제출해야 합니다", "처리할 것으로 기대"):
            self.assertEqual(todo_extract.extract_todos(text, BASE), [], text)

    def test_missing_and_invalid_dates_are_never_fabricated(self):
        self.assertIsNone(self.one("2026-02-30까지 자료 보내야 함")["due"])
        self.assertIsNone(self.one("자료 준비 필요함")["due"])

    def test_year_boundary(self):
        item = todo_extract.extract_todos("1월 3일까지 자료 보내야 함", datetime(2026, 12, 28))[0]
        self.assertEqual(item["due"], "2027-01-03")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="memoinall-todos-", ignore_cleanup_errors=True)
        home = Path(self.tmp.name)
        self.patchers = [patch.object(config, "HOME", home), patch.object(config, "DB_PATH", home / "memo.db"),
                         patch.object(embed, "_current", embed.HashEmbedder()), patch.object(store, "start_worker"),
                         patch("memoinall.api._prepare_search_later", new=self.no_prepare)]
        db.close()
        for patcher in self.patchers:
            patcher.start()
        db.init()

    @staticmethod
    async def no_prepare():
        pass

    def tearDown(self):
        db.close()
        for patcher in reversed(self.patchers):
            patcher.stop()
        gc.collect()
        self.tmp.cleanup()

    def add(self, text, **kwargs):
        memo = store.add_memo(text, created_at=BASE.isoformat(), enqueue_enrich=False, **kwargs)
        todos.refresh_pending()
        return memo, dict(db.one("SELECT * FROM todos WHERE memo_id=? AND active=1", (memo["id"],)))

    def board(self, **kwargs):
        return todos.board(today="2026-11-16", **kwargs)

    def test_all_categories_and_counts(self):
        self.add("오늘까지 자료 보내야 함")
        self.add("금요일까지 견적 보내야 함")
        self.add("자료 회신 대기")
        self.add("보고서 검토 필요함")
        self.add("김대리가 자료 보내야 함")
        counts = self.board()["counts"]
        self.assertEqual({key: counts[key] for key in ("today", "upcoming", "waiting", "unscheduled", "review")},
                         dict(today=1, upcoming=1, waiting=1, unscheduled=1, review=1))

    def test_actual_deadlines_precede_other_followups(self):
        _, due = self.add("오늘까지 자료 보내야 함")
        _, next_item = self.add("금요일까지 보고서 보내야 함")
        todos.update(next_item["id"], {"review_on": "2026-11-16"})
        self.assertEqual(self.board(view="today")["items"][0]["id"], due["id"])

    def test_snooze_preserves_deadline_and_overdue_visibility(self):
        _, item = self.add("11월 15일까지 보고서 보내야 함")
        todos.update(item["id"], {"review_on": "2026-11-17"})
        actual = self.board(view="today")["items"][0]
        self.assertEqual(actual["due"], "2026-11-15")
        self.assertTrue(actual["overdue"])

    def test_followup_becomes_today_without_moving_deadline(self):
        _, item = self.add("금요일까지 보고서 보내야 함")
        todos.update(item["id"], {"review_on": "2026-11-16"})
        self.assertEqual(self.board(view="today")["items"][0]["id"], item["id"])
        todos.update(item["id"], {"review_on": "2026-11-17"})
        self.assertFalse(self.board(view="today")["items"])
        self.assertEqual(todos.board(today="2026-11-17", view="today")["items"][0]["id"], item["id"])

    def test_waiting_reply_is_shown_on_followup_date(self):
        _, item = self.add("견적 요청함. 목요일까지 받기로 함")
        self.assertEqual(self.board(view="upcoming")["items"][0]["status"], "waiting")
        self.assertEqual(todos.board(today="2026-11-19", view="today")["items"][0]["id"], item["id"])

    def test_manual_choices_and_id_survive_edit_reindex_and_restore(self):
        memo, item = self.add("금요일까지 보고서 보내야 함")
        todo_id = item["id"]
        todos.update(todo_id, {"status": "dismissed", "due": "2026-12-01", "review_on": "2026-11-18", "title": "주간 보고서 보내기"})
        store.update_memo(memo["id"], "다음 주 월요일까지 보고서 보내야 함 #업무", enqueue_enrich=False)
        store.enrich(memo["id"])
        actual = self.board(view="dismissed")["items"][0]
        self.assertEqual((actual["id"], actual["due"], actual["review_on"], actual["title"]),
                         (todo_id, "2026-12-01", "2026-11-18", "주간 보고서 보내기"))
        store.update_memo(memo["id"], "관련 없는 기록", enqueue_enrich=False)
        self.assertFalse(self.board(view="dismissed")["items"])
        store.update_memo(memo["id"], "금요일까지 보고서 보내야 함", enqueue_enrich=False)
        self.assertEqual(self.board(view="dismissed")["items"][0]["id"], todo_id)
        todos.update(todo_id, {"status": "done"})
        store.enrich(memo["id"])
        self.assertEqual(self.board(view="done")["items"][0]["id"], todo_id)

    def test_reset_returns_to_latest_derived_dates(self):
        memo, item = self.add("금요일까지 보고서 보내야 함")
        todos.update(item["id"], {"due": None, "review_on": "2026-11-17"})
        store.update_memo(memo["id"], "다음 주 월요일까지 보고서 보내야 함", enqueue_enrich=False)
        todos.refresh_pending()
        reset = todos.update(item["id"], {"reset": ["due", "review_on"]})
        self.assertEqual(reset["due"], "2026-11-23")
        self.assertIsNone(reset["review_on"])
        self.assertEqual(reset["manual_fields"], [])

    def test_two_similar_tasks_do_not_share_completion(self):
        memo = store.add_memo("화요일까지 자료 보내야 함\n금요일까지 자료 보내야 함", created_at=BASE.isoformat(), enqueue_enrich=False)
        items = self.board()["items"]
        self.assertEqual(len(items), 2)
        todos.update(items[0]["id"], {"status": "done"})
        store.enrich(memo["id"])
        self.assertEqual(len(self.board()["items"]), 1)
        self.assertEqual(len(self.board(view="done")["items"]), 1)

    def test_archived_deleted_and_dismissed_do_not_pollute_counts_or_rollup(self):
        memo, item = self.add("자료 준비 필요함")
        todos.update(item["id"], {"status": "dismissed"})
        self.assertEqual(store.stats()["open_todos"], 0)
        self.assertFalse(store.get_memo(memo["id"])["todos"])
        self.assertFalse(organize.rollup(anchor="2026-11-16")["todos_open"])
        todos.update(item["id"], {"status": "open"})
        store.set_flag(memo["id"], archived=True)
        self.assertEqual(sum(self.board()["counts"].values()), 0)
        self.assertEqual(store.stats()["open_todos"], 0)
        store.delete_memo(memo["id"])
        self.assertFalse(db.query("SELECT * FROM todos"))

    def test_pagination_counts_all_tasks(self):
        for i in range(7):
            self.add(f"[ ] 자료 {i} 확인")
        result = self.board(view="unscheduled", limit=3, offset=3)
        self.assertEqual((result["total"], len(result["items"]), result["counts"]["unscheduled"]), (7, 3, 7))
        self.assertTrue(result["has_more"])

    def test_refresh_preserves_memo_timestamps_and_embeddings(self):
        memo, item = self.add("자료 준비 필요함")
        store.enrich(memo["id"])
        before = dict(db.one("SELECT * FROM memos WHERE id=?", (memo["id"],)))
        vectors = [tuple(row) for row in db.query("SELECT * FROM chunks")]
        db.execute("UPDATE memos SET todo_version=0")
        with patch.object(embed, "encode", side_effect=AssertionError("must not embed")):
            self.assertEqual(todos.refresh_pending(), 1)
        after = dict(db.one("SELECT * FROM memos WHERE id=?", (memo["id"],)))
        self.assertEqual(before, after)
        self.assertEqual(vectors, [tuple(row) for row in db.query("SELECT * FROM chunks")])

    def test_legacy_migration_keeps_id_and_completion(self):
        legacy = sqlite3.connect(":memory:")
        legacy.row_factory = sqlite3.Row
        self.addCleanup(legacy.close)
        legacy.executescript(db.SCHEMA)
        legacy.execute("INSERT INTO memos(id,body,created_at,updated_at) VALUES(1,?,?,?)",
                       ("보고서 보내야 함", BASE.isoformat(), BASE.isoformat()))
        legacy.execute("INSERT INTO todos(id,memo_id,text,done,due,created_at) VALUES(77,1,?,1,NULL,?)", ("보고서 보내야 함", BASE.isoformat()))
        db._migrate(legacy)
        with legacy:
            todos.sync(legacy, dict(legacy.execute("SELECT * FROM memos").fetchone()))
        item = dict(legacy.execute("SELECT * FROM todos WHERE active=1").fetchone())
        self.assertEqual((item["id"], item["status"], item["done"]), (77, "done", 1))
        db._migrate(legacy)
        self.assertEqual(legacy.execute("SELECT COUNT(*) FROM todos").fetchone()[0], 1)

    def test_api_validation_and_missing_tasks(self):
        _, item = self.add("자료 준비 필요함")
        with TestClient(app) as client:
            url = f"/api/todos/{item['id']}"
            for body in ({}, {"done": "false"}, {"due": "2026-02-30"}, {"review_on": "tomorrow"},
                         {"status": []}, {"title": " "}, {"reset": "due"}, {"unknown": True}):
                with self.subTest(body=body):
                    self.assertEqual(client.patch(url, json=body).status_code, 400)
            self.assertEqual(client.patch("/api/todos/999", json={"done": True}).status_code, 404)
            self.assertEqual(client.get("/api/todos?today=wrong").status_code, 400)
            self.assertEqual(client.get("/api/todos?view=wrong").status_code, 400)
            self.assertEqual(client.get("/api/todos?limit=0").status_code, 422)
            self.assertEqual(client.patch(url, json={"done": True}).json()["item"]["status"], "done")
            self.assertEqual(client.get("/api/todos?view=done").json()["total"], 1)


if __name__ == "__main__":
    unittest.main()
