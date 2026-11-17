"""Schedule-local labels; memo labels are the default until explicitly changed."""
from __future__ import annotations

import json
import re
from collections import Counter

from . import db


def validate(values) -> list[str]:
    from .tags import normalize
    if not isinstance(values, list) or len(values) > 20 or any(not isinstance(v, str) for v in values):
        raise ValueError("일정 태그는 이름 목록으로 최대 20개까지 지정해 주세요.")
    return sorted(set(normalize(value) for value in values))


def filter_tag(value) -> str | None:
    if value in (None, ""):
        return None
    return validate([value])[0]


def for_todo(item: dict) -> list[str]:
    if item.get("tags_json") is not None:
        return json.loads(item["tags_json"])
    return sorted({r["value"] for r in db.query("SELECT value FROM facets WHERE memo_id=? AND kind='tag'", (item["memo_id"],))})


def for_event(source_id: int, event_key: str, defaults: list[str]) -> list[str]:
    row = db.one("SELECT tags_json FROM calendar_event_tags WHERE source_id=? AND event_key=?", (source_id, event_key))
    return json.loads(row["tags_json"]) if row else defaults


def from_ical(event) -> list[str]:
    groups = event.get("CATEGORIES", [])
    groups = groups if isinstance(groups, list) else [groups]
    values = []
    for group in groups:
        for value in getattr(group, "cats", []):
            try:
                values.extend(validate([str(value)]))
            except ValueError:
                continue
    return sorted(set(values))[:20]


def choices() -> list[dict]:
    counts = Counter()
    inherited = {}
    for row in db.query("SELECT memo_id,value FROM facets WHERE kind='tag'"):
        inherited.setdefault(row["memo_id"], set()).add(row["value"])
    for row in db.query("SELECT t.memo_id,t.tags_json FROM todos t JOIN memos m ON t.memo_id=m.id WHERE t.active=1 AND m.archived=0"):
        counts.update(set(json.loads(row["tags_json"])) if row["tags_json"] is not None else inherited.get(row["memo_id"], set()))
    for row in db.query("SELECT e.data_json,t.tags_json FROM calendar_events e LEFT JOIN calendar_event_tags t ON t.source_id=e.source_id AND t.event_key=e.event_key"):
        counts.update(json.loads(row["tags_json"]) if row["tags_json"] is not None else json.loads(row["data_json"]).get("tags", []))
    return [{"name": name, "count": count} for name, count in sorted(counts.items())]


def update(item_id: str, payload: dict) -> dict:
    from . import todos
    if not isinstance(payload, dict) or set(payload) != {"tags"}:
        raise ValueError("변경할 일정 태그를 지정해 주세요.")
    values = validate(payload["tags"])
    if re.fullmatch(r"todo-\d+", item_id):
        return todos.update(int(item_id[5:]), {"tags": values})
    match = re.fullmatch(r"calendar-(\d+)-([a-f0-9]{64})", item_id)
    if not match:
        raise ValueError("일정을 확인해 주세요.")
    source_id, event_key = int(match[1]), match[2]
    conn = db.connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM calendar_events WHERE source_id=? AND event_key=?", (source_id, event_key)).fetchone():
            raise KeyError("일정을 찾을 수 없습니다. 달력을 다시 열어 주세요.")
        conn.execute("INSERT INTO calendar_event_tags(source_id,event_key,tags_json) VALUES(?,?,?) "
                     "ON CONFLICT(source_id,event_key) DO UPDATE SET tags_json=excluded.tags_json",
                     (source_id, event_key, json.dumps(values, ensure_ascii=False)))
    return {"id": item_id, "tags": values}
