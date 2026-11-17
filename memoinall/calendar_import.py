"""Read-only iCalendar imports. External events never become tasks implicitly."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import time
import uuid
from datetime import date, datetime, time as daytime, timedelta, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db, todos, schedule_tags

MAX_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 5000


class Conflict(ValueError):
    pass


def _range(start, end, zone):
    start, end = todos.validate_date(start, "시작"), todos.validate_date(end, "종료")
    if not start or not end or not 0 <= (date.fromisoformat(end) - date.fromisoformat(start)).days <= 366:
        raise ValueError("가져올 기간은 시작일부터 최대 1년으로 지정해 주세요.")
    if not "1970-01-01" <= start <= end <= "2100-12-31":
        raise ValueError("가져올 기간은 1970~2100년 사이로 지정해 주세요.")
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("시간대를 확인해 주세요. 예: Asia/Seoul") from None
    return date.fromisoformat(start), date.fromisoformat(end), tz


def _url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 33 for c in value):
        raise ValueError("iCal 주소를 확인해 주세요.")
    if value.startswith("webcal://"):
        value = "https://" + value[9:]
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.port not in (None, 443) or parts.fragment:
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        raise ValueError("외부 캘린더의 HTTPS iCal 주소를 입력해 주세요.") from None
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Pin the validated address while retaining TLS hostname verification.
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def fetch_calendar(url: str) -> bytes:
    url = _url(url)
    deadline = time.monotonic() + 25
    try:
        for _ in range(4):
            parts = urlsplit(url)
            addresses = {entry[4][0] for entry in socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)}
            if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                raise ValueError("로컬·내부 네트워크 주소는 캘린더 연결에 사용할 수 없습니다.")
            conn = _PinnedHTTPS(parts.hostname, sorted(addresses)[0], timeout=8)
            try:
                conn.request("GET", parts.path + ("?" + parts.query if parts.query else ""),
                             headers={"Accept": "text/calendar", "Accept-Encoding": "identity", "User-Agent": "memoinall-calendar/1"})
                response = conn.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    url = _url(urljoin(url, response.getheader("Location", "")))
                    continue
                if response.status != 200:
                    raise ValueError("일정을 받지 못했습니다. iCal 주소의 접근 권한과 유효 여부를 확인해 주세요.")
                chunks, size = [], 0
                while True:
                    if time.monotonic() > deadline:
                        raise ValueError("캘린더 응답이 지연되었습니다. 잠시 후 다시 시도해 주세요.")
                    chunk = response.read(65536)
                    if not chunk:
                        return b"".join(chunks)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("일정 파일은 2MB 이하만 가져올 수 있습니다.")
                    chunks.append(chunk)
            finally:
                conn.close()
        raise ValueError("주소 이동이 너무 많습니다. 최종 iCal 주소를 입력해 주세요.")
    except (OSError, http.client.HTTPException):
        # Exception text can contain a private subscription token: never expose it.
        raise ValueError("캘린더에 연결하지 못했습니다. 인터넷 연결과 iCal 주소를 확인해 주세요.") from None


def parse_calendar(content: bytes, start: str, end: str, zone: str) -> dict:
    from icalendar import Calendar
    import recurring_ical_events

    first, last, tz = _range(start, end, zone)
    if not content or len(content) > MAX_BYTES:
        raise ValueError("비어 있지 않은 2MB 이하의 .ics 파일을 선택해 주세요.")
    try:
        cal = Calendar.from_ical(content)
        if cal.name != "VCALENDAR" or cal.get("METHOD") == "CANCEL":
            raise ValueError
        components = cal.walk("VEVENT")
        if len(components) > MAX_EVENTS:
            raise ValueError("일정이 너무 많습니다. 캘린더를 나누어 가져와 주세요.")
        recurring_ids = {str(e.get("UID")) for e in components if any(k in e for k in ("RRULE", "RDATE", "RECURRENCE-ID"))}
        for event in components:
            if event.errors or not event.get("UID") or (not event.get("DTSTART") and event.get("STATUS") != "CANCELLED"):
                raise ValueError("일정의 식별자 또는 날짜가 올바르지 않습니다.")
            for key in ("DTSTART", "DTEND", "RECURRENCE-ID"):
                prop = event.get(key)
                if prop and isinstance(prop.dt, datetime) and prop.params.get("TZID") and not prop.dt.tzinfo:
                    raise ValueError("알 수 없는 일정 시간대가 있습니다.")
            rule = event.get("RRULE")
            if rule:
                if int(rule.get("INTERVAL", [1])[0]) < 1 or int(rule.get("COUNT", [1])[0]) < 1:
                    raise ValueError("반복 간격과 횟수는 1 이상이어야 합니다.")
                # Dense recurrence is not useful in a day calendar; bound expansion.
                if str(rule.get("FREQ", [""])[0]) not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"} or any(k in rule for k in ("BYSECOND", "BYMINUTE", "BYHOUR")):
                    raise ValueError("시간·분·초 단위 반복은 지원하지 않습니다. 일 단위 이상의 일정으로 내보내 주세요.")
                origin = event.decoded("DTSTART")
                if origin.year < 1970:
                    raise ValueError("1970년 이전에 시작한 반복 일정은 지원하지 않습니다.")
        lower = datetime.combine(first, daytime.min, tz)
        upper = datetime.combine(last + timedelta(days=1), daytime.min, tz)
        # Expand one series at a time so many repeating events cannot allocate
        # an unbounded list before the global occurrence limit is checked.
        groups = {}
        for event in components:
            groups.setdefault(str(event["UID"]), []).append(event)
        expanded = []
        for group in groups.values():
            single = Calendar(cal)
            single.subcomponents = [c for c in cal.subcomponents if c.name == "VTIMEZONE"] + group
            expanded.extend(recurring_ical_events.of(single).between(lower, upper))
            if len(expanded) > MAX_EVENTS:
                raise ValueError("이 기간의 일정이 5,000건을 넘습니다. 가져올 기간을 줄여 주세요.")
        result = {}
        for event in expanded:
            if str(event.get("STATUS", "")) == "CANCELLED":
                continue
            begin = event.decoded("DTSTART")
            finish = event.decoded("DTEND", begin)
            all_day = not isinstance(begin, datetime)
            if all_day:
                if isinstance(finish, datetime):
                    raise ValueError("종일 일정의 종료일 형식이 올바르지 않습니다.")
                if finish < begin:
                    raise ValueError("일정 종료일이 시작보다 빠릅니다.")
                start_day = begin
                end_day = max(begin, finish - timedelta(days=1))
                label = "종일"
                sort_time = ""
            else:
                begin = begin.replace(tzinfo=tz) if not begin.tzinfo else begin.astimezone(tz)
                if not isinstance(finish, datetime):
                    raise ValueError("시간 일정의 종료일 형식이 올바르지 않습니다.")
                finish = finish.replace(tzinfo=tz) if not finish.tzinfo else finish.astimezone(tz)
                if finish < begin:
                    raise ValueError("일정 종료 시각이 시작보다 빠릅니다.")
                start_day, end_day = begin.date(), max(begin, finish - timedelta(microseconds=1)).date()
                label = begin.strftime("%H:%M")
                if finish > begin:
                    label += "–" + finish.strftime("%H:%M" if finish.date() == begin.date() else "%m/%d %H:%M")
                sort_time = begin.isoformat()
            if end_day < first or start_day > last:
                continue
            recurrence = event.decoded("RECURRENCE-ID", event.decoded("DTSTART"))
            if isinstance(recurrence, datetime) and recurrence.tzinfo:
                recurrence = recurrence.astimezone(timezone.utc)
            suffix = recurrence.isoformat() if str(event["UID"]) in recurring_ids else ""
            key = hashlib.sha256((str(event["UID"]) + "\0" + suffix).encode()).hexdigest()
            result[key] = {"event_key": key, "title": str(event.get("SUMMARY", "")).strip()[:500] or "제목 없는 일정",
                           "start_day": start_day.isoformat(), "end_day": end_day.isoformat(),
                           "time_label": label, "sort_time": sort_time, "all_day": all_day,
                           "end_time": finish.isoformat() if not all_day else None,
                           "tags": schedule_tags.from_ical(event),
                           "location": str(event.get("LOCATION", ""))[:1000],
                           "description": str(event.get("DESCRIPTION", ""))[:4000]}
        return {"events": sorted(result.values(), key=lambda e: (e["start_day"], e["sort_time"], e["title"])),
                "ignored": len(cal.walk("VTODO")) + len(cal.walk("VJOURNAL"))}
    except ValueError as exc:
        if str(exc).endswith(".") and any("가" <= c <= "힣" for c in str(exc)):
            raise
        raise ValueError("올바른 iCalendar(.ics) 파일을 읽지 못했습니다. 캘린더에서 다시 내보내 주세요.") from None
    except Exception:
        raise ValueError("일정의 날짜나 반복 규칙을 읽지 못했습니다. 캘린더에서 다시 내보내 주세요.") from None


def sources() -> list[dict]:
    return [dict(row) for row in db.query("SELECT s.id,s.name,s.kind,s.range_start,s.range_end,s.timezone,s.updated_at,"
                                         "COUNT(e.event_key) AS count FROM calendar_sources s LEFT JOIN calendar_events e ON e.source_id=s.id "
                                         "GROUP BY s.id ORDER BY s.name")]


def preview(payload: dict) -> dict:
    name = payload.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("캘린더 이름은 1~80자로 입력해 주세요.")
    name = name.strip()
    start, end, zone = payload.get("range_start"), payload.get("range_end"), payload.get("timezone", "Asia/Seoul")
    _range(start, end, zone)
    kind = payload.get("kind")
    url = ""
    if kind == "url":
        url = _url(payload.get("url"))
        content = fetch_calendar(url)
    elif kind == "file" and isinstance(payload.get("content"), str):
        content = payload["content"].encode("utf-8")
    else:
        raise ValueError(".ics 파일 또는 iCal 주소를 선택해 주세요.")
    parsed = parse_calendar(content, start, end, zone)
    current = db.one("SELECT * FROM calendar_sources WHERE name=? OR (url<>'' AND url=?) ORDER BY id LIMIT 1", (name, url))
    if current and (current["name"] != name or current["kind"] != kind or current["url"] != url):
        raise ValueError("이미 등록된 이름 또는 주소입니다. 등록된 캘린더 이름으로 다시 가져오거나 다른 이름을 사용해 주세요.")
    old = {r["event_key"]: json.loads(r["data_json"]) for r in db.query("SELECT event_key,data_json FROM calendar_events WHERE source_id=?", (current["id"],))} if current else {}
    incoming = {e["event_key"]: e for e in parsed["events"]}
    counts = {"added": len(incoming.keys() - old.keys()), "removed": len(old.keys() - incoming.keys()),
              "updated": sum(old[k] != incoming[k] for k in old.keys() & incoming.keys()),
              "unchanged": sum(old[k] == incoming[k] for k in old.keys() & incoming.keys())}
    plan = {"name": name, "kind": kind, "url": url, "range_start": start, "range_end": end, "timezone": zone,
            "source_id": current["id"] if current else None, "revision": current["revision"] if current else None,
            "events": parsed["events"], "counts": counts}
    plan_id, now = str(uuid.uuid4()), datetime.now().isoformat()
    conn = db.connect()
    with conn:
        conn.execute("DELETE FROM calendar_import_plans WHERE created_at<?", ((datetime.now() - timedelta(hours=2)).isoformat(),))
        conn.execute("INSERT INTO calendar_import_plans(id,created_at,data_json) VALUES(?,?,?)", (plan_id, now, json.dumps(plan, ensure_ascii=False)))
    return {"plan_id": plan_id, "name": name, "range_start": start, "range_end": end, "timezone": zone,
            "total": len(parsed["events"]), "sample": parsed["events"][:30], "counts": counts, "ignored": parsed["ignored"]}


def refresh_preview(source_id: int, changes: dict | None = None) -> dict:
    row = db.one("SELECT * FROM calendar_sources WHERE id=?", (source_id,))
    if not row:
        raise KeyError("연결한 캘린더를 찾을 수 없습니다.")
    if row["kind"] != "url":
        raise ValueError("파일 캘린더는 같은 이름으로 새 .ics 파일을 가져와 주세요.")
    changes = changes or {}
    if set(changes) - {"range_start", "range_end", "timezone"}:
        raise ValueError("새로고침할 기간과 시간대를 확인해 주세요.")
    return preview({**dict(row), **changes})


def apply(plan_id: str) -> dict:
    conn = db.connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM calendar_import_plans WHERE id=?", (plan_id,)).fetchone()
        if not row or row["created_at"] < (datetime.now() - timedelta(hours=2)).isoformat():
            raise Conflict("미리보기가 만료되었습니다. 다시 불러와 주세요.")
        if row["applied_json"]:
            return json.loads(row["applied_json"])
        plan = json.loads(row["data_json"])
        source_id = plan["source_id"]
        if source_id:
            current = conn.execute("SELECT revision FROM calendar_sources WHERE id=?", (source_id,)).fetchone()
            if not current or current["revision"] != plan["revision"]:
                raise Conflict("캘린더가 변경되었습니다. 미리보기를 다시 불러와 주세요.")
        elif conn.execute("SELECT id FROM calendar_sources WHERE name=? OR (url<>'' AND url=?)", (plan["name"], plan["url"])).fetchone():
            raise Conflict("이미 추가된 캘린더입니다. 미리보기를 다시 불러와 주세요.")
        values = tuple(plan[k] for k in ("name", "kind", "url", "range_start", "range_end", "timezone")) + (datetime.now().isoformat(),)
        if source_id:
            conn.execute("UPDATE calendar_sources SET name=?,kind=?,url=?,range_start=?,range_end=?,timezone=?,updated_at=?,revision=revision+1 WHERE id=?", (*values, source_id))
        else:
            source_id = conn.execute("INSERT INTO calendar_sources(name,kind,url,range_start,range_end,timezone,updated_at) VALUES(?,?,?,?,?,?,?)", values).lastrowid
        conn.execute("DELETE FROM calendar_events WHERE source_id=?", (source_id,))
        conn.executemany("INSERT INTO calendar_events(source_id,event_key,start_day,end_day,data_json) VALUES(?,?,?,?,?)",
                         [(source_id, e["event_key"], e["start_day"], e["end_day"], json.dumps(e, ensure_ascii=False)) for e in plan["events"]])
        result = {"source_id": source_id, "count": len(plan["events"]), **plan["counts"]}
        conn.execute("UPDATE calendar_import_plans SET applied_json=? WHERE id=?", (json.dumps(result), plan_id))
        return result


def disconnect(source_id: int) -> dict:
    conn = db.connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute("SELECT * FROM calendar_sources WHERE id=?", (source_id,)).fetchone()
        if not source:
            raise KeyError("연결한 캘린더를 찾을 수 없습니다.")
        for row in conn.execute("SELECT id,data_json FROM calendar_import_plans").fetchall():
            if json.loads(row["data_json"])["name"] == source["name"]:
                conn.execute("DELETE FROM calendar_import_plans WHERE id=?", (row["id"],))
        conn.execute("DELETE FROM calendar_sources WHERE id=?", (source_id,))
    return {"ok": True}


def events_between(start: str, end: str) -> list[dict]:
    rows = db.query("SELECT e.*,s.name,s.timezone FROM calendar_events e JOIN calendar_sources s ON s.id=e.source_id "
                    "WHERE e.start_day<=? AND e.end_day>=?", (end, start))
    items = []
    for row in rows:
        item = json.loads(row["data_json"])
        item.update(id=f"calendar-{row['source_id']}-{row['event_key']}", source_name=row["name"],
                    timezone=row["timezone"], external=True, calendar_kinds=["external"])
        item["tags"] = schedule_tags.for_event(row["source_id"], row["event_key"], item.get("tags", []))
        items.append(item)
    return items
