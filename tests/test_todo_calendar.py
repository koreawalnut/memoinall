"""Calendar semantics: deadlines, followups, ranges and month boundaries."""
import gc
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from memoinall import config, db, store, todos
from memoinall.api import app


async def no_prepare():
    pass


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="memoinall-calendar-", ignore_cleanup_errors=True)
        home = Path(self.tmp.name)
        db.close()
        self.patchers = [patch.object(config, "HOME", home), patch.object(config, "DB_PATH", home / "memo.db"),
                         patch.object(store, "start_worker"), patch("memoinall.api._prepare_search_later", new=no_prepare)]
        for p in self.patchers:
            p.start()
        db.init()

    def tearDown(self):
        db.close()
        for p in reversed(self.patchers):
            p.stop()
        gc.collect()
        self.tmp.cleanup()

    def add(self, body):
        memo = store.add_memo(body, created_at="2026-11-16T09:00:00", enqueue_enrich=False)
        todos.refresh_pending()
        return memo, db.one("SELECT * FROM todos WHERE memo_id=?", (memo["id"],))

    def month(self, **kwargs):
        return todos.calendar_view(**{"month": "2026-11", "today": "2026-11-16", **kwargs})

    def cell(self, result, day):
        return next(item for item in result["days"] if item["date"] == day)

    def test_deadline_and_followup_have_distinct_days(self):
        _, task = self.add("금요일까지 보고서 보내야 함")
        todos.update(task["id"], {"review_on": "2026-11-18"})
        result = self.month(selected="2026-11-18")
        self.assertEqual(self.cell(result, "2026-11-18")["events"][0]["kinds"], ["review"])
        self.assertEqual(self.cell(result, "2026-11-20")["events"][0]["kinds"], ["due"])
        self.assertEqual(result["month_count"], 1)
        self.assertEqual(result["items"][0]["due"], "2026-11-20")

    def test_written_day_uses_local_time_tag_and_independent_pages(self):
        deadline, _ = self.add("금요일까지 보고서 보내야 함 #업무")
        notes = [store.add_memo(f"회의 기록 {i} #업무", created_at="2026-11-15T15:15:00Z", enqueue_enrich=False)
                 for i in range(5)]
        hidden = store.add_memo("보관 항목 #업무", created_at="2026-11-16T19:00:00", enqueue_enrich=False)
        store.set_flag(hidden["id"], archived=True)
        other = store.add_memo("다른 태그 #개인", created_at="2026-11-16T16:00:00", enqueue_enrich=False)
        store.update_memo(other["id"], "수정한 기록 #개인")
        result = self.month(selected="2026-11-16", tz="Asia/Seoul", tag="업무", offset=50)
        self.assertEqual(self.cell(result, "2026-11-16")["memo_count"], 6)
        self.assertEqual(self.cell(result, "2026-11-20")["memo_count"], 0)
        first = result["day_memos"]
        self.assertEqual(first["items"][0]["id"], deadline["id"])
        self.assertEqual(first["items"][1]["time"], "00:15")
        self.assertEqual(len(first["items"]), 5)
        tail = self.month(selected="2026-11-16", tz="Asia/Seoul", tag="업무", memo_offset=5)["day_memos"]
        self.assertEqual([m["id"] for m in tail["items"]], [notes[0]["id"]])
        self.assertEqual(self.month(selected="2026-11-16", tz="Asia/Seoul")["day_memos"]["total"], 7)
        west = self.month(selected="2026-11-15", tz="America/New_York")
        self.assertEqual(self.cell(west, "2026-11-15")["memo_count"], 5)
        self.assertEqual(west["day_memos"]["total"], 5)

    def test_memos_sort_by_instant_when_local_clock_repeats(self):
        for title, instant in (("첫 기록", "2026-11-01T05:50:00Z"), ("다음 기록", "2026-11-01T06:10:00Z")):
            store.add_memo(title, created_at=instant, enqueue_enrich=False)
        day = self.month(selected="2026-11-01", tz="America/New_York")["day_memos"]
        self.assertEqual([(m["title"], m["time"]) for m in day["items"]], [("다음 기록", "01:10"), ("첫 기록", "01:50")])

    def test_same_day_task_is_not_counted_twice(self):
        _, task = self.add("금요일까지 보고서 보내야 함")
        todos.update(task["id"], {"review_on": "2026-11-20"})
        result = self.month(selected="2026-11-20")
        self.assertEqual(self.cell(result, "2026-11-20")["count"], 1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["calendar_kinds"], ["due", "review"])

    def test_week_precision_is_a_range_not_seven_tasks(self):
        self.add("다음 주쯤 자료 검토해야 함")
        result = self.month(selected="2026-11-25")
        for day in range(23, 30):
            self.assertEqual(self.cell(result, f"2026-11-{day}")["events"][0]["kinds"], ["range"])
        self.assertEqual(result["month_count"], 1)
        self.assertIsNone(result["items"][0]["due"])
        self.assertEqual(self.cell(result, "2026-11-30")["count"], 0)

    def test_rescheduling_overrides_planned_day_without_moving_deadline(self):
        _, task = self.add("내일 보고서 작성하기")
        todos.update(task["id"], {"review_on": "2026-11-19", "due": "2026-11-20"})
        result = self.month()
        self.assertEqual(self.cell(result, "2026-11-17")["count"], 0)
        self.assertEqual(self.cell(result, "2026-11-19")["count"], 1)
        self.assertEqual(self.cell(result, "2026-11-20")["count"], 1)

    def test_undated_review_and_closed_items_do_not_get_invented_dates(self):
        self.add("김대리가 금요일까지 보고서 보내야 함")
        self.add("자료 검토 필요함")
        self.add("견적 회신 대기")
        _, done = self.add("금요일까지 다른 자료 보내야 함")
        todos.update(done["id"], {"status": "done"})
        _, dismissed = self.add("금요일까지 서류 보내야 함")
        todos.update(dismissed["id"], {"status": "dismissed"})
        result = self.month()
        self.assertFalse(any(day["count"] for day in result["days"]))
        self.assertEqual((result["counts"]["unscheduled"], result["counts"]["review"], result["undated_waiting"]), (1, 1, 1))

    def test_overdue_remains_on_original_date_and_in_today_count(self):
        _, task = self.add("11월 15일까지 보고서 보내야 함")
        todos.update(task["id"], {"review_on": "2026-11-18"})
        result = self.month(selected="2026-11-15")
        self.assertTrue(result["items"][0]["overdue"])
        self.assertEqual(result["overdue_count"], 1)
        self.assertEqual(result["counts"]["today"], 1)

    def test_leap_year_and_adjacent_month_days(self):
        result = self.month(month="2028-02", today="2028-02-29")
        self.assertEqual(result["selected"], "2028-02-29")
        self.assertEqual(len([day for day in result["days"] if day["in_month"]]), 29)
        self.assertEqual(len(result["days"]) % 7, 0)
        self.assertEqual(result["days"][0]["date"], "2028-01-30")
        self.assertEqual(result["days"][-1]["date"], "2028-03-04")

    def test_month_year_boundary_and_outside_dates_are_not_counted_in_month(self):
        self.add("2027-01-01까지 보고서 보내야 함")
        result = self.month(month="2026-12", selected="2027-01-01")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["month_count"], 0)
        self.assertFalse(self.cell(result, "2027-01-01")["in_month"])

    def test_all_event_counts_survive_agenda_pagination(self):
        for index in range(56):
            self.add(f"금요일까지 자료 {index} 보내야 함")
        result = self.month(selected="2026-11-20", limit=50)
        self.assertEqual(self.cell(result, "2026-11-20")["count"], 56)
        self.assertEqual(result["total"], 56)
        self.assertTrue(result["has_more"])
        tail = self.month(selected="2026-11-20", limit=50, offset=50)
        self.assertEqual(len(tail["items"]), 6)
        self.assertFalse({item["id"] for item in result["items"]} & {item["id"] for item in tail["items"]})

    def test_archived_deleted_and_calendar_api_validation(self):
        memo, _ = self.add("금요일까지 보고서 보내야 함")
        store.set_flag(memo["id"], archived=True)
        self.assertEqual(self.month()["month_count"], 0)
        store.delete_memo(memo["id"])
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/todos/calendar?month=2026-11").status_code, 200)
            for query in ("month=2026-13", "month=wrong", "month=0000-01", "month=2026-11&selected=2027-08-01", "month=2026-11&today=wrong"):
                self.assertEqual(client.get("/api/todos/calendar?" + query).status_code, 400)


if __name__ == "__main__":
    unittest.main()
