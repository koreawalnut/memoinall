"""Direct tasks and read-only calendar import through isolated DB/API calls."""
import gc
import json
import os
import socket
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from memoinall import calendar_import as calendars, config, db, store, todos
from memoinall.api import app


def event(uid="one", title="회의", start="DTSTART;VALUE=DATE:20261120", extra=""):
    return f"BEGIN:VEVENT\nUID:{uid}\nSUMMARY:{title}\n{start}\n{extra}\nEND:VEVENT\n"


def ics(*events):
    return ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//KO\n" + "".join(events) + "END:VCALENDAR\n").encode()


async def no_prepare():
    pass


class CalendarImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="memoinall-calendar-import-", ignore_cleanup_errors=True)
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

    def parse(self, content):
        return calendars.parse_calendar(content, "2026-11-01", "2026-11-30", "Asia/Seoul")["events"]

    def preview(self, content, **kwargs):
        return calendars.preview({"name": "내 캘린더", "kind": "file", "content": content.decode(),
                                  "range_start": "2026-11-01", "range_end": "2026-11-30", "timezone": "Asia/Seoul", **kwargs})

    def create(self, **kwargs):
        return todos.create({"title": "보고서 보내야 함", "scheduled_for": "2026-11-20", "request_id": str(uuid.uuid4()), **kwargs})

    def test_create_is_atomic_idempotent_searchable_and_survives_reindex(self):
        token = str(uuid.uuid4())
        task = self.create(request_id=token, due="2026-11-21")
        self.assertEqual(task["id"], self.create(request_id=token)["id"])
        self.assertEqual(db.one("SELECT count(*) n FROM memos")["n"], 1)
        self.assertEqual(db.one("SELECT count(*) n FROM memo_fts")["n"], 1)
        todos.update(task["id"], {"status": "done", "scheduled_for": "2026-11-22"})
        db.execute("UPDATE memos SET todo_version=0")
        todos.refresh_pending()
        rows = db.query("SELECT * FROM todos WHERE active=1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "done")
        self.assertEqual(rows[0]["scheduled_for"], "2026-11-22")

    def test_native_create_validation_and_schedule_edit(self):
        for kwargs in [{"title": " "}, {"title": 10}, {"scheduled_for": "2026-02-30"}, {"request_id": "bad"}, {"due": True}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.create(**kwargs)
        self.assertFalse(db.query("SELECT * FROM memos"))
        task = self.create()
        with self.assertRaises(ValueError):
            todos.update(task["id"], {"schedule_end": "2026-11-19"})
        updated = todos.update(task["id"], {"scheduled_for": "2026-11-18", "schedule_end": "2026-11-19"})
        self.assertEqual(updated["schedule_end"], "2026-11-19")

    def test_all_day_exclusive_end_and_single_day(self):
        events = self.parse(ics(event(extra="DTEND;VALUE=DATE:20261123"), event("two")))
        self.assertEqual([(e["start_day"], e["end_day"]) for e in events], [("2026-11-20", "2026-11-22"), ("2026-11-20", "2026-11-20")])

    def test_timezone_midnight_and_floating_times(self):
        data = ics(event(start="DTSTART:20261119T233000Z", extra="DTEND:20261120T010000Z"),
                   event("two", start="DTSTART;TZID=Asia/Seoul:20261120T230000", extra="DTEND;TZID=Asia/Seoul:20261121T000000"),
                   event("three", start="DTSTART:20261122T090000", extra="DTEND:20261122T100000"))
        items = self.parse(data)
        self.assertEqual(items[0]["time_label"], "08:30–10:00")
        self.assertEqual(items[0]["start_day"], "2026-11-20")
        self.assertEqual(items[1]["end_day"], "2026-11-20")
        self.assertEqual(items[2]["time_label"], "09:00–10:00")

    def test_folding_escaping_unicode(self):
        data = ics(event(title="교육계획\n 안 검토\\, 공유", extra="DESCRIPTION:첫 줄\\n둘째 줄\nLOCATION:회의실\\, 2층"))
        item = self.parse(data)[0]
        self.assertEqual(item["title"], "교육계획안 검토, 공유")
        self.assertEqual(item["description"], "첫 줄\n둘째 줄")
        self.assertEqual(item["location"], "회의실, 2층")

    def test_repeat_exception_rescheduled_occurrence(self):
        data = ics(event(start="DTSTART;VALUE=DATE:20261116", extra="RRULE:FREQ=DAILY;COUNT=5\nEXDATE;VALUE=DATE:20261117"),
                   event(title="옮긴 회의", start="DTSTART;VALUE=DATE:20261122", extra="RECURRENCE-ID;VALUE=DATE:20261118"))
        items = self.parse(data)
        self.assertEqual([e["start_day"] for e in items], ["2026-11-16", "2026-11-19", "2026-11-20", "2026-11-22"])
        self.assertEqual(items[-1]["title"], "옮긴 회의")
        self.assertEqual(len({e["event_key"] for e in items}), 4)

    def test_cancelled_event_and_occurrence(self):
        data = ics(event("cancelled", extra="STATUS:CANCELLED"),
                   event("repeat", start="DTSTART;VALUE=DATE:20261116", extra="RRULE:FREQ=DAILY;COUNT=3"),
                   event("repeat", start="DTSTART;VALUE=DATE:20261117", extra="RECURRENCE-ID;VALUE=DATE:20261117\nSTATUS:CANCELLED"))
        self.assertEqual([e["start_day"] for e in self.parse(data)], ["2026-11-16", "2026-11-18"])

    def test_nonrecurring_identity_stays_when_moved(self):
        first = self.parse(ics(event()))[0]
        moved = self.parse(ics(event(start="DTSTART;VALUE=DATE:20261125")))[0]
        self.assertEqual(first["event_key"], moved["event_key"])

    def test_import_preview_apply_repeat_and_refresh(self):
        native = self.create()
        data = ics(event(), event("two"))
        plan = self.preview(data)
        self.assertEqual(plan["counts"]["added"], 2)
        self.assertEqual(calendars.sources(), [])
        result = calendars.apply(plan["plan_id"])
        self.assertEqual(calendars.apply(plan["plan_id"]), result)
        again = self.preview(data)
        self.assertEqual(again["counts"], {"added": 0, "removed": 0, "updated": 0, "unchanged": 2})
        calendars.apply(again["plan_id"])
        changed = self.preview(ics(event(title="변경한 회의", start="DTSTART;VALUE=DATE:20261121")))
        self.assertEqual(changed["counts"], {"added": 0, "removed": 1, "updated": 1, "unchanged": 0})
        calendars.apply(changed["plan_id"])
        self.assertEqual(db.one("SELECT count(*) n FROM calendar_events")["n"], 1)
        self.assertEqual(db.one("SELECT title FROM todos WHERE id=?", (native["id"],))["title"], native["title"])

    def test_calendar_separates_external_events_from_task_counts_and_lists(self):
        self.create()
        calendars.apply(self.preview(ics(event()))["plan_id"])
        board = todos.calendar_view(month="2026-11", selected="2026-11-20", today="2026-11-16")
        self.assertEqual(board["month_count"], 1)
        self.assertEqual(board["external_count"], 1)
        self.assertEqual(board["total"], 2)
        self.assertEqual(board["counts"]["upcoming"], 1)
        self.assertEqual(todos.board()["total"], 1)
        cell = next(d for d in board["days"] if d["date"] == "2026-11-20")
        self.assertEqual(cell["events"][1]["kinds"], ["external"])

    def test_stale_preview_and_disconnect(self):
        initial = self.preview(ics(event()))
        source = calendars.apply(initial["plan_id"])["source_id"]
        first, second = self.preview(ics(event(title="first"))), self.preview(ics(event(title="second")))
        calendars.apply(first["plan_id"])
        with self.assertRaises(calendars.Conflict):
            calendars.apply(second["plan_id"])
        calendars.disconnect(source)
        self.assertFalse(db.query("SELECT * FROM calendar_events"))
        self.assertFalse(db.query("SELECT * FROM calendar_import_plans"))
        with self.assertRaises(calendars.Conflict):
            calendars.apply(initial["plan_id"])

    def test_invalid_file_does_not_overwrite_and_zero_preview_is_explicit(self):
        calendars.apply(self.preview(ics(event()))["plan_id"])
        for content in [b"<html>login</html>", b"", ics(event(extra="RRULE:FREQ=MINUTELY")),
                        ics(event(extra="RRULE:FREQ=DAILY;INTERVAL=0")), ics(event(extra="RRULE:FREQ=WEEKLY;COUNT=0")),
                        ics(event(start="DTSTART:bad")), b"x" * (calendars.MAX_BYTES + 1)]:
            with self.subTest(content=content[:50]), self.assertRaises(ValueError):
                self.preview(content)
        self.assertEqual(db.one("SELECT count(*) n FROM calendar_events")["n"], 1)
        empty = self.preview(ics())
        self.assertEqual(empty["total"], 0)
        self.assertEqual(empty["counts"]["removed"], 1)
        self.assertEqual(db.one("SELECT count(*) n FROM calendar_events")["n"], 1)

    def test_url_refresh_has_no_secret_in_public_data(self):
        secret = "https://calendar.google.com/calendar/ical/private-TOKEN/basic.ics"
        with patch.object(calendars, "fetch_calendar", return_value=ics(event())):
            plan = self.preview(ics(), kind="url", url=secret)
            self.assertNotIn("TOKEN", json.dumps(plan))
            source = calendars.apply(plan["plan_id"])["source_id"]
            self.assertNotIn("TOKEN", json.dumps(calendars.sources()))
            again = calendars.refresh_preview(source)
            self.assertEqual(again["counts"]["unchanged"], 1)
        with patch.object(calendars, "fetch_calendar", side_effect=ValueError("연결 실패")):
            with self.assertRaises(ValueError):
                calendars.refresh_preview(source)
        self.assertEqual(db.one("SELECT count(*) n FROM calendar_events")["n"], 1)

    def test_rejects_internal_urls_credentials_ports_and_schemes(self):
        for url in ["file:///etc/passwd", "http://example.com/x", "https://a:b@example.com/x", "https://example.com:8443/x", "https://example.com/x\nX:header"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                calendars.fetch_calendar(url)
        for ip in ["127.0.0.1", "192.168.0.1", "169.254.169.254", "::1"]:
            with self.subTest(ip=ip), patch.object(socket, "getaddrinfo", return_value=[(0, 0, 0, "", (ip, 443))]), self.assertRaises(ValueError):
                calendars.fetch_calendar("https://example.com/calendar")

    def test_range_timezone_validation(self):
        for values in [{"range_end": "2028-01-01"}, {"range_end": "2026-10-01"}, {"timezone": "Unknown/Zone"}, {"range_start": None}]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.preview(ics(event()), **values)

    def test_refresh_range_can_change_without_resupplying_private_url(self):
        with patch.object(calendars, "fetch_calendar", return_value=ics(event())) as fetch:
            plan = self.preview(ics(), kind="url", url="https://example.com/private-TOKEN.ics")
            source = calendars.apply(plan["plan_id"])["source_id"]
            changed = calendars.refresh_preview(source, {"range_start": "2026-11-21", "range_end": "2026-12-20"})
            self.assertEqual(changed["counts"]["removed"], 1)
            self.assertEqual(changed["total"], 0)
            calendars.apply(changed["plan_id"])
            self.assertEqual(calendars.sources()[0]["range_start"], "2026-11-21")
            self.assertEqual(fetch.call_args.args[0], "https://example.com/private-TOKEN.ics")

    def test_monthly_and_rdate_with_daylight_saving(self):
        data = ics(event(start="DTSTART;TZID=America/New_York:20261026T090000", extra="RRULE:FREQ=WEEKLY;COUNT=4"),
                   event("two", start="DTSTART;VALUE=DATE:20260901", extra="RRULE:FREQ=MONTHLY;COUNT=3\nRDATE;VALUE=DATE:20261115"))
        events = self.parse(data)
        self.assertEqual([e["start_day"] for e in events], ["2026-11-01", "2026-11-02", "2026-11-09", "2026-11-15", "2026-11-16"])
        self.assertEqual(events[1]["time_label"], "23:00")

    def test_public_redirect_is_revalidated_before_connecting_to_private_host(self):
        from unittest.mock import MagicMock
        conn = MagicMock()
        response = conn.getresponse.return_value
        response.status = 302
        response.getheader.return_value = "https://internal.example/calendar"
        with patch.object(socket, "getaddrinfo", side_effect=[[(0, 0, 0, "", ("8.8.8.8", 443))], [(0, 0, 0, "", ("127.0.0.1", 443))]]), patch.object(calendars, "_PinnedHTTPS", return_value=conn) as https:
            with self.assertRaises(ValueError):
                calendars.fetch_calendar("https://example.com/calendar")
            self.assertEqual(https.call_count, 1)
            conn.close.assert_called_once()

    def test_fetch_is_size_bounded_and_sanitizes_network_errors(self):
        from unittest.mock import MagicMock
        conn = MagicMock()
        response = conn.getresponse.return_value
        response.status = 200
        response.read.return_value = b"x" * 65536
        with patch.object(socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("8.8.8.8", 443))]), patch.object(calendars, "_PinnedHTTPS", return_value=conn):
            with self.assertRaisesRegex(ValueError, "2MB"):
                calendars.fetch_calendar("https://example.com/private-TOKEN")
            conn.request.side_effect = OSError("private-TOKEN")
            with self.assertRaises(ValueError) as exc:
                calendars.fetch_calendar("https://example.com/private-TOKEN")
            self.assertNotIn("TOKEN", str(exc.exception))

    def test_api_validation_create_import_and_delete(self):
        with TestClient(app) as client:
            payload = {"title": "회의 준비", "scheduled_for": "2026-11-20", "request_id": str(uuid.uuid4())}
            response = client.post("/api/todos", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["item"]["origin"], "manual")
            self.assertEqual(client.post("/api/todos", json={}).status_code, 400)
            request = {"name": "네이버", "kind": "file", "content": ics(event()).decode(), "range_start": "2026-11-01", "range_end": "2026-11-30"}
            preview = client.post("/api/calendars/preview", json=request)
            self.assertEqual(preview.status_code, 200, preview.text)
            apply = client.post(f"/api/calendars/plans/{preview.json()['plan_id']}/apply")
            self.assertEqual(apply.status_code, 200, apply.text)
            self.assertEqual(client.get("/api/calendars").json()["items"][0]["count"], 1)
            self.assertEqual(client.post("/api/calendars/preview", json=[]).status_code, 400)
            self.assertEqual(client.post("/api/calendars/plans/missing/apply").status_code, 409)
            self.assertEqual(client.delete(f"/api/calendars/{apply.json()['source_id']}").status_code, 200)
            self.assertEqual(client.delete("/api/calendars/999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
