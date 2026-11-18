"""Tag persistence/filtering and reviewed ICS exchange, using an isolated DB."""
import gc
import json
import sys
import tempfile
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from icalendar import Calendar
from memoinall import calendar_import as calendars, config, db, schedule_share as share, schedule_tags as tags, store, todos
from memoinall.api import app
from memoinall import tags as my_tags


async def no_prepare():
    pass


class ScheduleShareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="memoinall-share-", ignore_cleanup_errors=True)
        home = Path(self.tmp.name)
        db.close()
        self.patchers = [patch.object(config, "HOME", home), patch.object(config, "DB_PATH", home / "memo.db"),
                         patch.object(store, "start_worker"), patch.object(store, "enqueue"),
                         patch("memoinall.api._prepare_search_later", new=no_prepare)]
        for p in self.patchers:
            p.start()
        db.init()

    def tearDown(self):
        db.close()
        for p in reversed(self.patchers):
            p.stop()
        gc.collect()
        self.tmp.cleanup()

    def create(self, **kwargs):
        return todos.create({"title": "자료 검토", "scheduled_for": "2026-11-20", "request_id": str(uuid.uuid4()), **kwargs})

    def preview(self, **kwargs):
        return share.preview({"start": "2026-11-01", "end": "2026-11-30", **kwargs})

    def export(self, plan=None, selected=None):
        plan = plan or self.preview()
        return share.export(plan["plan_id"], selected if selected is not None else [item["key"] for item in plan["items"]])

    def external(self, day="20", extra="", name="팀 캘린더", start=None):
        start = start or f"DTSTART;TZID=Asia/Seoul:202611{day}T090000\nDTEND;TZID=Asia/Seoul:202611{day}T100000"
        content = f"BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:team-one\nSUMMARY:팀 회의\n{start}\nCATEGORIES:업무,팀\nDESCRIPTION:외부 비공개 상세\nLOCATION:개인 사무실\n{extra}\nEND:VEVENT\nEND:VCALENDAR"
        plan = calendars.preview({"name": name, "kind": "file", "content": content,
                                  "range_start": "2026-11-01", "range_end": "2026-11-30", "timezone": "Asia/Seoul"})
        calendars.apply(plan["plan_id"])
        return calendars.events_between("2026-11-01", "2026-11-30")[0]

    def test_memo_tag_inheritance_overrides_and_reset(self):
        memo = store.add_memo("금요일까지 자료 검토해야 함\n#업무 #기획", created_at="2026-11-16T09:00:00", enqueue_enrich=False)
        item = todos.board()["items"][0]
        self.assertEqual(item["tags"], ["기획", "업무"])
        self.assertTrue(item["tags_inherited"])
        result = tags.update(f"todo-{item['id']}", {"tags": ["#팀 일정", "팀_일정"]})
        self.assertEqual(result["tags"], ["팀_일정"])
        self.assertFalse(result["tags_inherited"])
        self.assertIn("#업무", store.get_memo(memo["id"])["body"])
        db.execute("UPDATE memos SET todo_version=0")
        self.assertEqual(todos.board()["items"][0]["tags"], ["팀_일정"])
        self.assertEqual(todos.update(item["id"], {"reset": ["tags"]})["tags"], ["기획", "업무"])
        self.assertEqual(todos.update(item["id"], {"tags": []})["tags"], [])

    def test_tag_filter_applies_to_calendar_list_and_counts(self):
        self.create(tags=["업무"])
        self.create(tags=["개인"])
        self.external()
        board = todos.board(tag="업무", today="2026-11-16")
        self.assertEqual(board["total"], 1)
        self.assertEqual(board["counts"]["upcoming"], 1)
        cal = todos.calendar_view(month="2026-11", selected="2026-11-20", tag="업무")
        self.assertEqual((cal["month_count"], cal["external_count"], cal["total"]), (1, 1, 2))
        self.assertEqual(next(day["count"] for day in cal["days"] if day["date"] == "2026-11-20"), 2)
        self.assertIn({"name": "업무", "count": 2}, tags.choices())
        self.assertEqual(todos.board(tag="없음")["total"], 0)

    def test_external_local_tags_survive_refresh_and_disconnect_cleans_them(self):
        event = self.external()
        tags.update(event["id"], {"tags": ["공유팀"]})
        refreshed = self.external(day="21")
        self.assertEqual(refreshed["id"], event["id"])
        self.assertEqual(refreshed["tags"], ["공유팀"])
        calendars.disconnect(calendars.sources()[0]["id"])
        self.assertFalse(db.query("SELECT * FROM calendar_event_tags"))
        with self.assertRaises(KeyError):
            tags.update(event["id"], {"tags": ["업무"]})

    def test_invalid_tags_do_not_create_or_partially_update(self):
        for value in ["업무", [1], [""], ["<script>"], ["x" * 41], ["x"] * 21]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.create(tags=value)
        self.assertFalse(db.query("SELECT * FROM memos"))
        item = self.create(tags=["원래"])
        with self.assertRaises(ValueError):
            todos.update(item["id"], {"tags": ["변경"], "schedule_end": "2026-11-01"})
        self.assertEqual(todos.board()["items"][0]["tags"], ["원래"])

    def test_preview_selection_privacy_and_tag_round_trip(self):
        for tag in ("업무", "팀"):
            my_tags.add(tag)
        keep = self.create(tags=["팀", "업무"], title="공유할 제목")
        self.create(tags=["개인"], title="보내지 않을 제목")
        db.execute("UPDATE memos SET body=? WHERE id=?", ("비밀 메모 원문 private-TOKEN", keep["memo_id"]))
        self.external()
        plan = self.preview()
        self.assertEqual(plan["total"], 2)
        self.assertFalse(any(item["external"] for item in plan["items"]))
        self.assertEqual(db.one("SELECT count(*) n FROM calendar_events")["n"], 1)
        key = next(item["key"] for item in plan["items"] if item["title"] == "공유할 제목")
        result = self.export(plan, [key])
        self.assertEqual(result["count"], 1)
        for secret in ["비밀 메모", "private-TOKEN", "보내지 않을 제목", "외부 비공개", "개인 사무실"]:
            self.assertNotIn(secret, result["content"])
        parsed = calendars.parse_calendar(result["content"].encode(), "2026-11-01", "2026-11-30", "Asia/Seoul")["events"]
        self.assertEqual(parsed[0]["tags"], ["업무", "팀"])
        self.assertEqual(parsed[0]["start_day"], "2026-11-20")
        self.assertEqual(parsed[0]["end_day"], "2026-11-20")

    def test_export_checks_reviewed_items_and_changed_or_deleted_schedules(self):
        my_tags.add("변경")
        item = self.create()
        plan = self.preview()
        for selected in [None, [], "bad", [1], ["not-reviewed"], [plan["items"][0]["key"]] * 2]:
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                share.export(plan["plan_id"], selected)
        todos.update(item["id"], {"tags": ["변경"]})
        with self.assertRaises(calendars.Conflict):
            self.export(plan)
        plan = self.preview()
        todos.update(item["id"], {"status": "done"})
        with self.assertRaises(calendars.Conflict):
            self.export(plan)

    def test_new_items_cannot_be_silently_added_to_export(self):
        self.create(title="확인한 일정")
        plan = self.preview()
        self.create(title="나중에 추가된 일정")
        exported = self.export(plan)
        self.assertEqual(exported["count"], 1)
        self.assertNotIn("나중에 추가된 일정", exported["content"])

    def test_filter_and_undated_completed_exclusions(self):
        self.create(tags=["팀"], scheduled_for=None)
        self.create(tags=["팀"])
        self.create(tags=["개인"])
        for status in ("done", "dismissed", "review"):
            item = self.create(tags=["팀"])
            todos.update(item["id"], {"status": status})
        plan = self.preview(tag="팀")
        self.assertEqual((plan["total"], plan["undated"]), (1, 1))

    def test_registered_share_tags_apply_to_native_and_external_files(self):
        for tag in ("업무", "기획"):
            my_tags.add(tag)
        self.create(tags=["기획", "미등록"])
        self.create(tags=["미등록"])
        self.external()
        plan = self.preview(tags=["업무", "기획"], include_external=True)
        self.assertEqual(sorted(item["tags"] for item in plan["items"]), [["기획"], ["업무"]])
        events = Calendar.from_ical(self.export(plan)["content"]).walk("VEVENT")
        self.assertEqual(sorted([str(t) for t in event["CATEGORIES"].cats] for event in events), [["기획"], ["업무"]])
        whole = self.preview(tags=[], include_external=True)
        self.assertEqual(whole["total"], 3)
        self.assertIn([], [item["tags"] for item in whole["items"]])
        with self.assertRaises(ValueError):
            self.preview(tags=["미등록"])
        my_tags.remove("기획")
        with self.assertRaises(calendars.Conflict):
            self.export(plan)

    def test_period_overlap_all_day_exclusive_end_and_uid_stability(self):
        item = self.create(scheduled_for="2026-10-30")
        todos.update(item["id"], {"schedule_end": "2026-11-03"})
        first = Calendar.from_ical(self.export()["content"]).walk("VEVENT")[0]
        self.assertEqual(first.decoded("dtstart"), date(2026, 10, 30))
        self.assertEqual(first.decoded("dtend"), date(2026, 11, 4))
        todos.update(item["id"], {"scheduled_for": "2026-11-02", "schedule_end": "2026-11-05"})
        second = Calendar.from_ical(self.export()["content"]).walk("VEVENT")[0]
        self.assertEqual(first["uid"], second["uid"])
        self.assertTrue(str(first["uid"]).endswith("@memoinall"))

    def test_due_review_and_planned_dates_match_calendar(self):
        item = self.create(due="2026-11-22")
        todos.update(item["id"], {"review_on": "2026-11-21"})
        self.assertEqual({e["start_day"] for e in self.preview()["items"]}, {"2026-11-21", "2026-11-22"})
        todos.update(item["id"], {"review_on": "2026-11-22"})
        self.assertEqual(self.preview()["total"], 1)

    def test_external_details_are_opt_in_and_times_are_utc(self):
        self.external()
        exported = self.export(self.preview(include_external=True))
        self.assertNotIn("외부 비공개", exported["content"])
        self.assertNotIn("개인 사무실", exported["content"])
        item = Calendar.from_ical(exported["content"]).walk("VEVENT")[0]
        self.assertEqual(item.decoded("dtstart"), datetime(2026, 11, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(item.decoded("dtend"), datetime(2026, 11, 20, 1, tzinfo=timezone.utc))
        detailed = self.export(self.preview(include_external=True, include_details=True))["content"]
        self.assertIn("외부 비공개 상세", detailed)
        self.assertIn("개인 사무실", detailed)

    def test_zero_duration_event_omits_dtend(self):
        self.external(start="DTSTART;TZID=Asia/Seoul:20261120T090000")
        item = Calendar.from_ical(self.export(self.preview(include_external=True))["content"]).walk("VEVENT")[0]
        self.assertNotIn("DTEND", item)

    def test_old_import_end_time_is_preserved(self):
        self.external(start="DTSTART;TZID=Asia/Seoul:20261120T230000\nDTEND;TZID=Asia/Seoul:20261121T000000")
        row = db.one("SELECT * FROM calendar_events")
        data = json.loads(row["data_json"])
        data.pop("end_time")
        db.execute("UPDATE calendar_events SET data_json=?", (json.dumps(data),))
        item = Calendar.from_ical(self.export(self.preview(include_external=True))["content"]).walk("VEVENT")[0]
        self.assertEqual(item.decoded("dtend") - item.decoded("dtstart"), timedelta(hours=1))

    def test_expired_preview_and_bad_periods(self):
        self.create()
        plan = self.preview()
        db.execute("UPDATE schedule_share_plans SET created_at='2000-01-01'")
        with self.assertRaises(calendars.Conflict):
            self.export(plan)
        for kwargs in [{"end": "2026-10-01"}, {"end": "2028-11-01"}, {"start": None}, {"start": "2026-02-30"}, {"include_external": "yes"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.preview(**kwargs)

    def test_api_and_invalid_ids(self):
        item = self.create()
        with TestClient(app) as client:
            result = client.patch(f"/api/schedule/items/todo-{item['id']}/tags", json={"tags": ["업무"]})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(client.get("/api/schedule/tags").json()["items"], [{"name": "업무", "count": 1}])
            self.assertEqual(client.get("/api/todos", params={"tag": "업무"}).json()["total"], 1)
            self.assertEqual(client.patch("/api/schedule/items/todo-9999/tags", json={"tags": []}).status_code, 404)
            self.assertEqual(client.patch("/api/schedule/items/bad/tags", json={"tags": []}).status_code, 400)
            response = client.post("/api/schedule/share/preview", json={"start": "2026-11-01", "end": "2026-11-30"})
            self.assertEqual(response.status_code, 200)
            plan = response.json()
            endpoint = f"/api/schedule/share/{plan['plan_id']}/export"
            self.assertEqual(client.post(endpoint, json={"selected": []}).status_code, 400)
            self.assertEqual(client.post(endpoint, json={"selected": [plan["items"][0]["key"]]}).status_code, 200)
            todos.update(item["id"], {"title": "달라진 제목"})
            self.assertEqual(client.post(endpoint, json={"selected": [plan["items"][0]["key"]]}).status_code, 409)

    def test_native_ics_save_uses_calendar_filter_and_preserves_crlf(self):
        from memoinall import desktop
        self.create(tags=["공유"])
        exported = self.export()
        destination = Path(self.tmp.name) / exported["filename"]
        window = MagicMock()
        window.create_file_dialog.return_value = [str(destination)]
        desktop._install_downloader(window)
        save = window.expose.call_args.args[0]
        self.assertTrue(save(exported["content"], exported["filename"]))
        self.assertIn("일정 파일 (*.ics)", window.create_file_dialog.call_args.kwargs["file_types"])
        self.assertEqual(destination.read_bytes(), exported["content"].encode("utf-8"))
        window.create_file_dialog.return_value = None
        self.assertFalse(save("replacement", exported["filename"]))
        self.assertEqual(destination.read_bytes(), exported["content"].encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
