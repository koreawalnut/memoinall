"""Preview/select/export dated schedules as portable ICS snapshots."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone

from . import calendar_import, db, schedule_tags, tags as my_tags, todos

LABELS = {"due": "마감", "review": "다시 보기", "scheduled": "예정", "range": "기간 중", "external": "외부 일정"}
MAX_ITEMS = 5000


def _options(payload: dict) -> dict:
    if set(payload) - {"start", "end", "tag", "tags", "include_external", "include_details"} or ("tag" in payload and "tags" in payload):
        raise ValueError("공유할 일정 조건을 확인해 주세요.")
    start, end = todos.validate_date(payload.get("start"), "시작"), todos.validate_date(payload.get("end"), "종료")
    if not start or not end or not "1970-01-01" <= start <= end <= "2100-12-31" or (date.fromisoformat(end) - date.fromisoformat(start)).days > 366:
        raise ValueError("공유 기간은 1970~2100년 사이에서 최대 1년으로 지정해 주세요.")
    opts = {"start": start, "end": end}
    if "tag" in payload:
        opts["tag"] = schedule_tags.filter_tag(payload["tag"])
    else:
        opts["tags"] = schedule_tags.validate(payload.get("tags", []))
        if set(opts["tags"]).difference(my_tags.names()):
            raise ValueError("설정의 내 태그가 변경되었습니다. 일정 화면을 다시 열고 태그를 선택해 주세요.")
    for field, default in (("include_external", False), ("include_details", False)):
        value = payload.get(field, default)
        if type(value) is not bool:
            raise ValueError("외부 일정·상세 내용 포함 여부를 확인해 주세요.")
        opts[field] = value
    return opts


def _external_end(item: dict) -> str:
    """Older imports kept only a display label; preserve their timed duration."""
    if item.get("end_time"):
        return item["end_time"]
    begin = datetime.fromisoformat(item["sort_time"])
    if "–" not in item["time_label"]:
        return begin.isoformat()
    match = re.search(r"(\d{2}):(\d{2})$", item["time_label"])
    if not match:
        raise ValueError("일정 종료 시각을 확인할 수 없습니다. 원본 캘린더를 새로고침해 주세요.")
    finish = datetime.combine(date.fromisoformat(item["end_day"]), begin.timetz()).replace(hour=int(match[1]), minute=int(match[2]), second=0, microsecond=0)
    if (finish.hour, finish.minute) == (0, 0) or finish < begin:
        finish += timedelta(days=1)
    return finish.isoformat()


def _items(opts: dict) -> tuple[list[dict], int]:
    todos.refresh_pending()
    items, undated = [], 0
    # Older clients and saved previews use the single schedule-tag filter.
    registered = set(my_tags.names()) if "tags" in opts else None
    wanted = set(opts.get("tags", []))
    if opts.get("tag"):
        wanted.add(opts["tag"])

    def shared_tags(values):
        return [tag for tag in values if registered is None or tag in registered]

    for row in db.query("SELECT t.* FROM todos t JOIN memos m ON m.id=t.memo_id WHERE t.active=1 AND m.archived=0 AND t.status IN ('open','waiting')"):
        item = dict(row)
        tags = shared_tags(schedule_tags.for_todo(item))
        if wanted and wanted.isdisjoint(tags):
            continue
        spans = {}
        for field, kind in (("due", "due"), ("review_on", "review")):
            if item[field]:
                spans.setdefault((item[field], item[field]), []).append(kind)
        if item["scheduled_for"] and not item["review_on"]:
            begin, finish = item["scheduled_for"], item["schedule_end"] or item["scheduled_for"]
            spans.setdefault((begin, finish), []).append("range" if finish != begin else "scheduled")
        if not spans:
            undated += 1
        for (begin, finish), kinds in spans.items():
            if finish < opts["start"] or begin > opts["end"]:
                continue
            label = "·".join(LABELS[k] for k in kinds)
            items.append({"key": f"todo-{item['id']}-{'-'.join(kinds)}", "title": item["title"], "kind": label,
                          "start_day": begin, "end_day": finish, "all_day": True, "start_time": None, "end_time": None,
                          "tags": tags, "location": "", "description": f"{label} 일정", "external": False})
    if opts["include_external"]:
        for item in calendar_import.events_between(opts["start"], opts["end"]):
            tags = shared_tags(item["tags"])
            if wanted and wanted.isdisjoint(tags):
                continue
            items.append({"key": item["id"], "title": item["title"], "kind": "외부 일정", "start_day": item["start_day"],
                          "end_day": item["end_day"], "all_day": item["all_day"], "start_time": item["sort_time"] or None,
                          "end_time": _external_end(item) if not item["all_day"] else None, "tags": tags,
                          "location": item["location"] if opts["include_details"] else "",
                          "description": item["description"] if opts["include_details"] else "", "external": True})
    if len(items) > MAX_ITEMS:
        raise ValueError("공유 일정이 5,000건을 넘습니다. 기간이나 태그를 좁혀 주세요.")
    items.sort(key=lambda e: (e["start_day"], e["start_time"] or "", e["title"], e["key"]))
    return items, undated


def preview(payload: dict) -> dict:
    opts = _options(payload)
    items, undated = _items(opts)
    now, plan_id = datetime.now(), str(uuid.uuid4())
    conn = db.connect()
    with conn:
        conn.execute("DELETE FROM schedule_share_plans WHERE created_at<?", ((now - timedelta(hours=2)).isoformat(),))
        conn.execute("INSERT INTO schedule_share_plans VALUES(?,?,?)", (plan_id, now.isoformat(), json.dumps({"options": opts, "items": items}, ensure_ascii=False)))
    return {"plan_id": plan_id, "items": items, "undated": undated, "total": len(items), "options": opts}


def export(plan_id: str, selected) -> dict:
    from icalendar import Calendar, Event
    row = db.one("SELECT * FROM schedule_share_plans WHERE id=?", (plan_id,))
    if not row or row["created_at"] < (datetime.now() - timedelta(hours=2)).isoformat():
        raise calendar_import.Conflict("공유 미리보기가 만료되었습니다. 다시 확인해 주세요.")
    if not isinstance(selected, list) or not selected or len(selected) > MAX_ITEMS or any(not isinstance(k, str) for k in selected) or len(set(selected)) != len(selected):
        raise ValueError("파일로 보낼 일정을 한 개 이상 선택해 주세요.")
    plan = json.loads(row["data_json"])
    reviewed = {item["key"]: item for item in plan["items"]}
    current = {item["key"]: item for item in _items(plan["options"])[0]}
    if any(key not in reviewed for key in selected):
        raise ValueError("미리보기에서 확인한 일정만 공유할 수 있습니다.")
    if any(current.get(key) != reviewed[key] for key in selected):
        raise calendar_import.Conflict("선택한 일정이 변경되었습니다. 미리보기를 다시 확인해 주세요.")
    conn = db.connect()
    with conn:
        conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schedule_share_namespace',?)", (str(uuid.uuid4()),))
        namespace = conn.execute("SELECT value FROM meta WHERE key='schedule_share_namespace'").fetchone()[0]
    cal = Calendar()
    cal.add("prodid", "-//memoinall//Schedule sharing//KO")
    cal.add("version", "2.0")
    scope = ", ".join(plan["options"].get("tags", [])) or plan["options"].get("tag")
    cal.add("x-wr-calname", "공유 일정" + (f" · {scope}" if scope else ""))
    stamp = datetime.now(timezone.utc)
    for key in selected:
        item = reviewed[key]
        event = Event()
        event.add("uid", hashlib.sha256((namespace + ":" + key).encode()).hexdigest() + "@memoinall")
        event.add("dtstamp", stamp)
        event.add("summary", item["title"] if item["external"] else f"[{item['kind']}] {item['title']}")
        event.add("transp", "TRANSPARENT" if not item["external"] else "OPAQUE")
        if item["all_day"]:
            start, end = date.fromisoformat(item["start_day"]), date.fromisoformat(item["end_day"])
            if end == date.max:
                raise ValueError("일정 종료일이 공유 가능한 범위를 넘습니다.")
            event.add("dtstart", start)
            event.add("dtend", end + timedelta(days=1))
        else:
            start = datetime.fromisoformat(item["start_time"]).astimezone(timezone.utc)
            end = datetime.fromisoformat(item["end_time"]).astimezone(timezone.utc)
            event.add("dtstart", start)
            if end > start:
                event.add("dtend", end)
        if item["tags"]:
            event.add("categories", item["tags"])
        if item["location"]:
            event.add("location", item["location"])
        description = item["description"]
        if item["tags"]:
            description += ("\n" if description else "") + "태그: " + " ".join("#" + tag for tag in item["tags"])
        if description:
            event.add("description", description)
        cal.add_component(event)
    content = cal.to_ical()
    if len(content) > calendar_import.MAX_BYTES:
        raise ValueError("공유 파일이 2MB를 넘습니다. 일정 수를 줄여 주세요.")
    return {"content": content.decode("utf-8"), "filename": f"일정공유-{plan['options']['start']}-{plan['options']['end']}.ics", "count": len(selected)}
