"""휴대폰 앱이 QR 로 보낸 일정을 PC 일정으로 넣는다 (휴대폰 → PC 한 방향).

휴대폰이 보내는 꾸러미:
    {"format": "memoinall/schedules", "version": 1, "count": n,
     "schedules": [{"uid", "title", "date", "time", "note", "tags", "done", "created_at", "updated_at"}]}
비밀번호를 정하면 메모와 같은 봉투(exchange.seal 형식)에 담겨 오고, 봉투 겉에 "kind": "schedules" 가 붙는다.

각 일정은 PC 에 직접 등록한 일정(todos.create)과 똑같이 들어간다 — 예정일·태그·끝냄 여부를 살리고,
시각은 이름 앞에, 메모는 함께 저장되는 메모 본문에 둔다. 저장 요청 식별자를 휴대폰 일정의 uid 로 정해 두어
같은 일정을 몇 번 보내도 한 번만 들어간다.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid

from . import db, exchange, tags, todos

FORMAT = "memoinall/schedules"
VERSION = 1
MAX_ITEMS = 500
MAX_CHARS = 2_000_000          # QR 40장은 수십 KB 다. 이보다 크면 사고로 본다.
NAMESPACE = uuid.UUID("5b0f6a2e-4d7c-4a6b-9a65-2f1d0c3e8b71")
TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def request_id(uid: str) -> str:
    """휴대폰 일정 하나 ↔ PC 일정 하나. todos.create 가 같은 식별자는 다시 만들지 않는다."""
    return str(uuid.uuid5(NAMESPACE, "memoinall-phone-schedule:" + uid))


def _tags(values) -> list[str]:
    out = []
    for value in values if isinstance(values, list) else []:
        try:
            name = tags.normalize(str(value))
        except tags.TagError:
            continue                    # 못 쓰는 이름은 그 태그만 버린다
        if name not in out:
            out.append(name)
    return out[:20]


def parse(text: str, password: str = "") -> dict:
    """받은 내용을 검증해 일정 목록으로. 사용자에게 보일 문장으로만 실패한다."""
    if not isinstance(text, str) or not text.strip():
        raise exchange.ExchangeError("받은 일정이 비어 있습니다 — 다시 찍어 주세요.")
    if len(text) > MAX_CHARS:
        raise exchange.ExchangeError("받은 일정이 너무 큽니다 — 나눠서 보내 주세요.")
    data = exchange._load_json(text)
    sealed = exchange.is_sealed(data)
    if sealed:
        data = exchange._load_json(exchange.unseal(data, password))
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise exchange.ExchangeError("휴대폰 앱이 보낸 일정이 아닙니다.")
    try:
        version = int(data.get("version") or VERSION)
    except (TypeError, ValueError):
        version = VERSION
    if version > VERSION:
        raise exchange.ExchangeError(f"더 새로운 형식의 일정입니다 (version {version}). memoinall 을 최신으로 올려주세요.")
    raw = data.get("schedules")
    if not isinstance(raw, list):
        raise exchange.ExchangeError("받은 일정이 손상됐습니다 — 다시 찍어 주세요.")
    if len(raw) > MAX_ITEMS:
        raise exchange.ExchangeError(f"한 번에 {MAX_ITEMS}건까지 받을 수 있습니다 — 나눠서 보내 주세요.")

    items, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 500:
            continue
        try:
            day = todos.validate_date(entry.get("date"), "예정")
        except ValueError:
            continue
        if not day:
            continue
        time = entry.get("time") if isinstance(entry.get("time"), str) and TIME_RE.fullmatch(entry["time"]) else ""
        note = entry.get("note").strip()[:5000] if isinstance(entry.get("note"), str) else ""
        uid = entry.get("uid").strip()[:200] if isinstance(entry.get("uid"), str) else ""
        if not uid:
            # uid 없이 온 일정(예전 휴대폰 앱)은 내용으로 정한다.
            basis = json.dumps([title.strip(), day, time, entry.get("created_at")], ensure_ascii=False)
            uid = hashlib.sha256(basis.encode("utf-8")).hexdigest()
        key = request_id(uid)
        if key in seen:
            continue
        seen.add(key)
        items.append({"key": key, "title": title.strip(), "date": day, "time": time, "note": note,
                      "tags": _tags(entry.get("tags")), "done": entry.get("done") is True})
    return {"items": items, "skipped": len(raw) - len(items), "encrypted": sealed}


def _exists(key: str) -> bool:
    return db.one("SELECT 1 FROM memos WHERE source='task' AND external_id=?", (key,)) is not None


def preview(text: str, password: str = "") -> dict:
    """무엇이 들어올지 먼저 보여준다. 이미 받은 일정은 표시해 둔다."""
    parsed = parse(text, password)
    for item in parsed["items"]:
        item["exists"] = _exists(item["key"])
    return parsed


def apply(text: str, password: str, selected) -> dict:
    """고른 일정만 PC 일정으로 넣는다. 이미 있는 것은 건너뛴다."""
    if not isinstance(selected, list) or not selected or any(not isinstance(key, str) for key in selected):
        raise ValueError("가져올 일정을 하나 이상 고르세요.")
    parsed = preview(text, password)
    chosen = set(selected)
    if chosen - {item["key"] for item in parsed["items"]}:
        raise ValueError("고른 일정이 받은 내용과 다릅니다 — QR을 다시 찍어 주세요.")
    created, skipped = [], 0
    for item in parsed["items"]:
        if item["key"] not in chosen:
            continue
        if item["exists"]:
            skipped += 1
            continue
        title = f"{item['time']} {item['title']}" if item["time"] else item["title"]
        todo = todos.create({"title": title[:500], "scheduled_for": item["date"], "request_id": item["key"],
                             "tags": item["tags"], "note": item["note"]})
        if item["done"]:
            todo = todos.update(todo["id"], {"done": True})
        created.append({"id": todo["id"], "title": todo["title"], "date": item["date"]})
    return {"imported": len(created), "skipped_existing": skipped, "items": created}
