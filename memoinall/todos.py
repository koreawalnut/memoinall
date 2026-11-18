"""Persistent task decisions over regenerable extraction results."""
from __future__ import annotations

import json
import re
import calendar
from collections import Counter
from datetime import date, datetime

from . import db, todo_extract, schedule_tags, tags

STATUSES = {"open", "waiting", "review", "done", "dismissed"}
GROUPS = ("today", "upcoming", "waiting", "unscheduled", "review", "done", "dismissed")
FIELDS = ("title", "status", "due", "review_on", "scheduled_for", "schedule_end", "date_label", "reason")
EDITABLE = {"title", "status", "due", "review_on", "scheduled_for", "schedule_end"}


def _base(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed


def sync(conn, memo: dict, extracted: list[dict] | None = None) -> None:
    """Called within the memo's write transaction. IDs and overrides survive edits.

    Match exact evidence first, then a unique action identity within this memo.
    Removed evidence becomes inactive, so restoring it also restores decisions.
    """
    base = _base(memo["created_at"])
    extracted = extracted if extracted is not None else todo_extract.extract_todos(memo["body"], base, memo["source"])
    old = [dict(row) for row in conn.execute("SELECT * FROM todos WHERE memo_id=? AND origin='rule' ORDER BY active DESC,id", (memo["id"],))]
    # An explicitly accepted AI/manual task survives rule reindexing. Don't add its
    # exact evidence again when a later rule learns to recognize that sentence.
    ai_rows = list(conn.execute("SELECT text,identity_key FROM todos WHERE memo_id=? AND origin IN ('ai','manual')", (memo["id"],)))
    extracted = [item for item in extracted if not any(
        item["text"].strip().rstrip(".! ") == row["text"].strip().rstrip(".! ")
        or item["identity_key"] == row["identity_key"] for row in ai_rows)]
    for row in old:
        if not row["identity_key"]:
            row["identity_key"] = todo_extract.identity(row["text"], base)
    new_counts = Counter(item["identity_key"] for item in extracted)
    old_counts = Counter(row["identity_key"] for row in old)
    used = set()
    conn.execute("UPDATE todos SET active=0 WHERE memo_id=? AND origin='rule'", (memo["id"],))
    for item in extracted:
        previous = next((row for row in old if row["id"] not in used and (
            row["text"] == item["text"] or row["derived_json"] == "{}" and row["text"] == item["text"][:500]
        )), None)
        key = item["identity_key"]
        if previous is None and key and new_counts[key] == old_counts[key] == 1:
            previous = next(row for row in old if row["identity_key"] == key)
        derived = {field: item.get(field) for field in FIELDS}
        overrides = json.loads(previous["overrides_json"]) if previous else {}
        ai_values = json.loads(previous["ai_values_json"]) if previous else {}
        effective = {**derived, **ai_values, **overrides}
        values = [effective[field] for field in FIELDS]
        data = (item["text"], *values, int(effective["status"] == "done"), key,
                json.dumps(derived, ensure_ascii=False), json.dumps(overrides, ensure_ascii=False))
        columns = "text," + ",".join(FIELDS) + ",done,identity_key,derived_json,overrides_json"
        if previous:
            used.add(previous["id"])
            assignment = ",".join(f"{column}=?" for column in columns.split(","))
            conn.execute(f"UPDATE todos SET {assignment},active=1 WHERE id=?", (*data, previous["id"]))
        else:
            marks = ",".join("?" for _ in data)
            conn.execute(f"INSERT INTO todos(memo_id,created_at,{columns}) VALUES(?,?,{marks})",
                         (memo["id"], memo["created_at"], *data))
    conn.execute("UPDATE memos SET todo_version=? WHERE id=?", (todo_extract.VERSION, memo["id"]))


def refresh_pending() -> int:
    """Upgrade old extraction without loading embeddings or changing memo dates."""
    count = 0
    for row in db.query("SELECT * FROM memos WHERE todo_version < ?", (todo_extract.VERSION,)):
        memo = dict(row)
        items = todo_extract.extract_todos(memo["body"], _base(memo["created_at"]), memo["source"])
        conn = db.connect()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT body,created_at,todo_version FROM memos WHERE id=?", (memo["id"],)).fetchone()
            if not current or current["body"] != memo["body"] or current["created_at"] != memo["created_at"] or current["todo_version"] >= todo_extract.VERSION:
                continue
            sync(conn, memo, items)
            count += 1
    return count


def validate_date(value, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{field} 날짜는 YYYY-MM-DD 형식으로 입력해 주세요.")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} 날짜를 확인해 주세요.") from None
    return value


def update(todo_id: int, payload: dict) -> dict:
    allowed = EDITABLE | {"done", "reset", "tags"}
    if not payload or set(payload) - allowed:
        raise ValueError("수정할 할 일 항목을 확인해 주세요.")
    changes = dict(payload)
    tag_values = schedule_tags.validate(changes.pop("tags")) if "tags" in changes else None
    if "done" in changes:
        if type(changes["done"]) is not bool or "status" in changes:
            raise ValueError("완료 여부는 true 또는 false로 지정해 주세요.")
        changes["status"] = "done" if changes.pop("done") else "open"
    reset = changes.pop("reset", [])
    if not isinstance(reset, list) or any(not isinstance(key, str) or key not in EDITABLE | {"tags"} for key in reset):
        raise ValueError("되돌릴 항목을 확인해 주세요.")
    if "status" in changes and (not isinstance(changes["status"], str) or changes["status"] not in STATUSES):
        raise ValueError("할 일 상태를 확인해 주세요.")
    if "title" in changes:
        if not isinstance(changes["title"], str) or not 1 <= len(changes["title"].strip()) <= 500:
            raise ValueError("할 일 이름은 1~500자로 입력해 주세요.")
        changes["title"] = changes["title"].strip()
    for field in ("due", "review_on", "scheduled_for", "schedule_end"):
        if field in changes:
            changes[field] = validate_date(changes[field], "마감" if field == "due" else "다시 볼")
    conn = db.connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM todos WHERE id=? AND active=1", (todo_id,)).fetchone()
        if not row:
            raise KeyError("할 일을 찾을 수 없습니다. 목록을 새로 열어 주세요.")
        if tag_values is not None or "tags" in reset:
            conn.execute("UPDATE todos SET tags_json=? WHERE id=?", (json.dumps(tag_values, ensure_ascii=False) if tag_values is not None else None, todo_id))
        overrides = json.loads(row["overrides_json"])
        ai_values = json.loads(row["ai_values_json"])
        derived = json.loads(row["derived_json"]) or {field: row[field] for field in FIELDS}
        for field in reset:
            overrides.pop(field, None)
        overrides.update(changes)
        effective = {**derived, **ai_values, **overrides}
        if effective.get("schedule_end") and (not effective.get("scheduled_for") or effective["schedule_end"] < effective["scheduled_for"]):
            raise ValueError("예정 종료일은 시작일 이후로 지정해 주세요.")
        assignment = ",".join(f"{field}=?" for field in FIELDS)
        conn.execute(f"UPDATE todos SET {assignment}, done=?, overrides_json=?,ai_values_json=? WHERE id=?",
                     (*[effective.get(field) for field in FIELDS], int(effective["status"] == "done"),
                      json.dumps(overrides, ensure_ascii=False), json.dumps(ai_values, ensure_ascii=False), todo_id))
        return public(dict(conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()))


def create(payload: dict) -> dict:
    """A direct task and its searchable memo are committed together, once."""
    from . import store
    import uuid

    if set(payload) - {"title", "scheduled_for", "due", "request_id", "tags"}:
        raise ValueError("추가할 할 일 항목을 확인해 주세요.")
    title = payload.get("title")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 500:
        raise ValueError("할 일 이름은 1~500자로 입력해 주세요.")
    title = title.strip()
    tag_values = schedule_tags.validate(payload.get("tags", []))
    planned = validate_date(payload.get("scheduled_for"), "예정")
    due = validate_date(payload.get("due"), "마감")
    try:
        token = str(uuid.UUID(payload.get("request_id", "")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("저장 요청 식별자가 올바르지 않습니다.") from None
    conn = db.connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute("SELECT t.* FROM todos t JOIN memos m ON m.id=t.memo_id "
                                "WHERE m.source='task' AND m.external_id=?", (token,)).fetchone()
        if previous:
            return public(dict(previous))
        now = store.now_iso()
        body = title + (f"\n예정일: {planned}" if planned else "") + (f"\n마감일: {due}" if due else "")
        memo_id = conn.execute("INSERT INTO memos(body,title,source,external_id,created_at,updated_at) VALUES(?,?,'task',?,?,?)",
                               (body, title, token, now, now)).lastrowid
        store._index_body(conn, memo_id, body)
        values = dict.fromkeys(FIELDS)
        values.update(title=title, status="open", scheduled_for=planned, due=due, date_label="", reason="")
        columns = ",".join(FIELDS)
        data = json.dumps(values, ensure_ascii=False)
        tid = conn.execute(f"INSERT INTO todos(memo_id,text,created_at,origin,identity_key,{columns},derived_json,overrides_json) "
                           f"VALUES(?,?,?,'manual',?,{','.join('?' for _ in FIELDS)},?,?)",
                           (memo_id, title, now, todo_extract.identity(title, datetime.now()),
                            *values.values(), data, json.dumps({k: values[k] for k in ('title','scheduled_for','due')}, ensure_ascii=False))).lastrowid
        conn.execute("UPDATE memos SET todo_version=? WHERE id=?", (todo_extract.VERSION, memo_id))
        conn.execute("UPDATE todos SET tags_json=? WHERE id=?", (json.dumps(tag_values, ensure_ascii=False), tid))
        result = public(dict(conn.execute("SELECT * FROM todos WHERE id=?", (tid,)).fetchone()))
    store.enqueue(memo_id)
    return result


def group_for(item: dict, today: str) -> str:
    if item["status"] in {"done", "dismissed", "review"}:
        return item["status"]
    # Snoozing is never allowed to conceal an actual due/overdue deadline.
    if item["due"] and item["due"] <= today:
        return "today"
    if item["review_on"]:
        return "today" if item["review_on"] <= today else "upcoming"
    if item["status"] == "waiting":
        return "waiting"
    if item["scheduled_for"] and item["scheduled_for"] <= today:
        return "today"
    if item["due"] or item["scheduled_for"]:
        return "upcoming"
    return "unscheduled"


def public(item: dict) -> dict:
    item["manual_fields"] = sorted(json.loads(item.pop("overrides_json", "{}")))
    item["tags"] = schedule_tags.for_todo(item)
    item["tags_inherited"] = item.pop("tags_json", None) is None
    if not item["tags_inherited"]:
        item["manual_fields"].append("tags")
    item["ai_fields"] = sorted(json.loads(item.pop("ai_values_json", "{}")))
    if "status" in item["manual_fields"] and item["status"] == "open":
        item["reason"] = ""
    item.pop("derived_json", None)
    item.pop("identity_key", None)
    sources = json.loads(item.pop("ai_sources_json", "[]"))
    item["ai_sources"] = []
    item["source_changed"] = False
    for source in sources:
        memo = db.one("SELECT title,body,created_at FROM memos WHERE id=?", (source["memo_id"],))
        changed = not memo or source["quote"] not in memo["body"] or source.get("created_at") != memo["created_at"]
        item["source_changed"] |= bool(changed)
        item["ai_sources"].append({"memo_id": source["memo_id"], "quote": source["quote"],
                                   "title": memo["title"] if memo else "삭제된 메모", "missing": memo is None})
    return item


def board(*, today: str | None = None, view: str = "all", limit: int = 100, offset: int = 0, tag: str | None = None) -> dict:
    tag = schedule_tags.filter_tag(tag)
    today = validate_date(today, "기준") or date.today().isoformat()
    if view not in (*GROUPS, "all") or not 1 <= limit <= 500 or offset < 0:
        raise ValueError("할 일 조회 조건을 확인해 주세요.")
    refresh_pending()
    rows = db.query("SELECT t.*, m.title AS memo_title FROM todos t JOIN memos m ON m.id=t.memo_id "
                    "WHERE t.active=1 AND m.archived=0")
    counts = {group: 0 for group in GROUPS}
    items = []
    for row in rows:
        item = dict(row)
        if tag and tag not in schedule_tags.for_todo(item):
            continue
        group = group_for(item, today)
        counts[group] += 1
        if group == view or view == "all" and group not in {"done", "dismissed"}:
            item["group"] = group
            item["overdue"] = bool(item["due"] and item["due"] < today and item["status"] not in {"done", "dismissed"})
            items.append(public(item))

    def sort_key(item):
        dates = [item[key] for key in ("due", "review_on", "scheduled_for") if item[key]]
        due_now = bool(item["due"] and item["due"] <= today)
        return (GROUPS.index(item["group"]), not item["overdue"], not due_now, min(dates, default="9999-12-31"), -item["id"])

    items.sort(key=sort_key)
    total = len(items)
    return {"items": items[offset:offset + limit], "counts": counts, "total": total, "tag": tag, "tag_choices": schedule_tags.choices(),
            "registered_tags": tags.all_tags(with_counts=False),
            "today": today, "offset": offset, "has_more": offset + limit < total}


def calendar_view(*, month: str, selected: str | None = None, today: str | None = None,
                  limit: int = 50, offset: int = 0, tag: str | None = None) -> dict:
    """Calendar dates are evidence/decisions, never synthetic workload dates.

    One task can have a deadline and a follow-up on different days. On the same
    day it appears once with both labels. Week precision remains a date range.
    """
    tag = schedule_tags.filter_tag(tag)
    today = validate_date(today, "기준") or date.today().isoformat()
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("달력의 월을 YYYY-MM 형식으로 지정해 주세요.")
    year, number = map(int, month.split("-"))
    if not 2 <= year <= 9998 or not 1 <= number <= 12 or not 1 <= limit <= 500 or offset < 0:
        raise ValueError("달력 조회 범위를 확인해 주세요.")
    grid = list(calendar.Calendar(firstweekday=6).itermonthdates(year, number))
    selected = validate_date(selected, "선택한") or (today if today.startswith(month) else month + "-01")
    if selected < grid[0].isoformat() or selected > grid[-1].isoformat():
        raise ValueError("선택한 날짜가 표시 중인 달력 범위에 없습니다.")
    refresh_pending()
    rows = db.query("SELECT t.*,m.title AS memo_title FROM todos t JOIN memos m ON m.id=t.memo_id "
                    "WHERE t.active=1 AND m.archived=0")
    days = {day.isoformat(): {} for day in grid}
    counts = {group: 0 for group in GROUPS}
    items, overdue, undated_waiting, month_ids = [], 0, 0, set()
    for row in rows:
        item = dict(row)
        if tag and tag not in schedule_tags.for_todo(item):
            continue
        item["group"] = group_for(item, today)
        counts[item["group"]] += 1
        if item["status"] not in {"open", "waiting"}:
            continue
        item["overdue"] = bool(item["due"] and item["due"] < today)
        overdue += item["overdue"]
        if item["status"] == "waiting" and not any(item[key] for key in ("due", "review_on", "scheduled_for")):
            undated_waiting += 1
        event_dates = {}
        for key, kind in (("due", "due"), ("review_on", "review")):
            if item[key] in days:
                event_dates.setdefault(item[key], []).append(kind)
        if item["scheduled_for"] and not item["review_on"]:
            start, end = item["scheduled_for"], item["schedule_end"] or item["scheduled_for"]
            for day in days:
                if start <= day <= end:
                    event_dates.setdefault(day, []).append("range" if start != end else "scheduled")
        for day, kinds in event_dates.items():
            days[day][item["id"]] = {"id": item["id"], "title": item["title"], "kinds": kinds,
                                     "overdue": item["overdue"] and "due" in kinds, "status": item["status"]}
            if day.startswith(month):
                month_ids.add(item["id"])
        if selected in event_dates:
            item["calendar_kinds"] = event_dates[selected]
            items.append(public(item))
    from . import calendar_import
    external_ids = set()
    for event in calendar_import.events_between(grid[0].isoformat(), grid[-1].isoformat()):
        if tag and tag not in event["tags"]:
            continue
        for day in days:
            if event["start_day"] <= day <= event["end_day"]:
                days[day][event["id"]] = {"id": event["id"], "title": event["title"], "kinds": ["external"],
                                          "time_label": event["time_label"], "sort_time": event["sort_time"], "overdue": False, "status": "event"}
                if day.startswith(month):
                    external_ids.add(event["id"])
        if event["start_day"] <= selected <= event["end_day"]:
            items.append(event)
    def order(item):
        kinds = item.get("kinds", item.get("calendar_kinds", []))
        return ("due" not in kinds, "review" not in kinds, "external" in kinds, item.get("sort_time", ""), item["title"], str(item["id"]))
    cells = [{"date": day, "in_month": day.startswith(month), "is_today": day == today,
              "events": sorted(events.values(), key=order), "count": len(events)} for day, events in days.items()]
    items.sort(key=order)
    return {"month": month, "selected": selected, "today": today, "days": cells, "counts": counts, "tag": tag, "tag_choices": schedule_tags.choices(),
            "registered_tags": tags.all_tags(with_counts=False),
            "month_count": len(month_ids), "external_count": len(external_ids), "sources": calendar_import.sources(),
            "overdue_count": overdue, "undated_waiting": undated_waiting,
            "items": items[offset:offset + limit], "total": len(items), "offset": offset,
            "has_more": offset + limit < len(items)}
