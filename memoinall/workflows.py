"""Reusable AI task settings. Shared files contain settings, never memo snapshots."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from . import db, store, tags

PERIODS = {"all": "전체 기간", "this_week": "이번 주", "last_week": "지난주",
           "this_month": "이번 달", "last_30_days": "최근 30일", "custom": "날짜 지정"}
FIELDS = ("name", "instruction", "format", "tags", "period", "since", "until", "person", "budget")
FILE_FORMAT = "memoinall/workflow"
MAX_FILE_BYTES = 100_000


class Conflict(ValueError):
    pass


def today() -> date:
    return date.today()


def period_bounds(period: str, since=None, until=None) -> tuple[str | None, str | None]:
    if not isinstance(period, str) or period not in PERIODS:
        raise ValueError("기간을 목록에서 선택하세요.")
    if period == "all":
        return None, None
    if period == "custom":
        try:
            start = date.fromisoformat(since) if since else None
            end = date.fromisoformat(until) if until else None
        except (ValueError, TypeError):
            raise ValueError("날짜를 YYYY-MM-DD 형식으로 입력하세요.")
        if not start and not end:
            raise ValueError("시작일이나 종료일을 입력하세요.")
        if start and end and start > end:
            raise ValueError("종료일은 시작일보다 빠를 수 없습니다.")
        return start.isoformat() if start else None, end.isoformat() if end else None
    end = today()
    if period == "this_week":
        start = end - timedelta(days=end.weekday())
    elif period == "last_week":
        end = end - timedelta(days=end.weekday() + 1)
        start = end - timedelta(days=6)
    elif period == "this_month":
        start = end.replace(day=1)
    else:
        start = end - timedelta(days=29)
    return start.isoformat(), end.isoformat()


def _text(value, label, maximum, *, required=False):
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f"{label}을(를) {maximum:,}자 이내로 입력하세요.")
    return value.strip()


def normalize(payload: dict) -> dict:
    from .generate import FORMATS

    if not isinstance(payload, dict) or set(payload) - set(FIELDS):
        raise ValueError("업무 설정에 지원하지 않는 항목이 있습니다.")
    name = _text(payload.get("name"), "업무 이름", 80, required=True)
    instruction = _text(payload.get("instruction"), "정리할 내용", 10_000, required=True)
    fmt, period = payload.get("format", "auto"), payload.get("period", "all")
    if not isinstance(fmt, str) or fmt not in FORMATS:
        raise ValueError("정리 형식을 목록에서 선택하세요.")
    if not isinstance(period, str):
        raise ValueError("기간을 목록에서 선택하세요.")
    since, until = period_bounds(period, payload.get("since"), payload.get("until"))
    raw_tags = payload.get("tags", [])
    if not isinstance(raw_tags, list) or len(raw_tags) > 50 or any(not isinstance(t, str) for t in raw_tags):
        raise ValueError("태그는 50개 이하의 목록으로 입력하세요.")
    names = list(dict.fromkeys(tags.normalize(t) for t in raw_tags))
    budget = payload.get("budget", 6000)
    if type(budget) is not int or budget not in (6000, 12000, 24000):
        raise ValueError("읽을 분량을 목록에서 선택하세요.")
    return {"name": name, "instruction": instruction, "format": fmt, "tags": names, "period": period,
            "since": since if period == "custom" else None,
            "until": until if period == "custom" else None,
            "person": _text(payload.get("person"), "사람 조건", 100).lstrip("@") or None,
            "budget": budget}


def _item(row) -> dict:
    return {**json.loads(row["settings_json"]), "id": row["id"], "version": row["version"]}


def all_workflows() -> list[dict]:
    return [_item(row) for row in db.query("SELECT * FROM workflows ORDER BY updated_at DESC,id DESC")]


def get(workflow_id: int) -> dict:
    row = db.one("SELECT * FROM workflows WHERE id=?", (workflow_id,))
    if row is None:
        raise KeyError("저장한 업무를 찾을 수 없습니다.")
    return _item(row)


def save(payload: dict, workflow_id: int | None = None, version=None) -> dict:
    data = normalize(payload)
    conn = db.connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if workflow_id is not None:
            existing = get(workflow_id)
            if type(version) is not int or version != existing["version"]:
                raise Conflict("다른 곳에서 업무 설정이 바뀌었습니다. 다시 열어 확인하세요.")
        elif conn.execute("SELECT COUNT(*) FROM workflows").fetchone()[0] >= 200:
            raise ValueError("업무는 200개까지 저장할 수 있습니다. 사용하지 않는 업무를 정리하세요.")
        duplicate = conn.execute("SELECT id FROM workflows WHERE name=?", (data["name"],)).fetchone()
        if duplicate and duplicate["id"] != workflow_id:
            raise ValueError("같은 이름의 업무가 있습니다. 다른 이름으로 저장하세요.")
        encoded, now = json.dumps(data, ensure_ascii=False), store.now_iso()
        if workflow_id is None:
            workflow_id = conn.execute("INSERT INTO workflows(name,settings_json,updated_at) VALUES(?,?,?)",
                                       (data["name"], encoded, now)).lastrowid
        else:
            conn.execute("UPDATE workflows SET name=?,settings_json=?,updated_at=?,version=version+1 WHERE id=?",
                         (data["name"], encoded, now, workflow_id))
    return get(workflow_id)


def delete(workflow_id: int, version) -> dict:
    conn = db.connect()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        current = get(workflow_id)
        if type(version) is not int or version != current["version"]:
            raise Conflict("다른 곳에서 업무 설정이 바뀌었습니다. 다시 열어 확인하세요.")
        conn.execute("DELETE FROM workflows WHERE id=?", (workflow_id,))
    return {"ok": True}


def export(payload: dict) -> dict:
    data = normalize(payload)
    filename = re.sub(r'[^0-9A-Za-z가-힣_-]+', "_", data["name"]).strip("_")[:60] or "업무"
    return {"filename": f"memoinall-업무-{filename}.json",
            "content": json.dumps({"format": FILE_FORMAT, "version": 1, "workflow": data}, ensure_ascii=False, indent=2)}


def preview(content: str) -> dict:
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("업무 설정 파일은 100KB 이하로 선택하세요.")
    try:
        document = json.loads(content)
    except (ValueError, RecursionError):
        raise ValueError("업무 설정 파일을 읽을 수 없습니다.")
    if (not isinstance(document, dict) or set(document) != {"format", "version", "workflow"}
            or document.get("format") != FILE_FORMAT or type(document.get("version")) is not int
            or document.get("version") != 1):
        raise ValueError("memoinall에서 ‘파일로 공유’한 업무 설정 파일을 선택하세요.")
    data = normalize(document["workflow"])
    names = {row["name"] for row in db.query("SELECT name FROM workflows")}
    base, suffix = data["name"], 2
    while data["name"] in names:
        data["name"] = f"{base[:70]} ({suffix})"
        suffix += 1
    available = {tag["name"] for tag in tags.filter_choices()}
    return {"workflow": data, "missing_tags": [tag for tag in data["tags"] if tag not in available]}
