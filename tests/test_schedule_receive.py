"""휴대폰 앱이 QR 로 보낸 일정을 PC 일정으로 넣기.  python tests/test_schedule_receive.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.TemporaryDirectory(prefix="memoinall-sched-rx-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import patch  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from memoinall import db, exchange, schedule_receive, store, todos  # noqa: E402
from memoinall.api import app  # noqa: E402

FAILS = 0


def check(ok, name):
    global FAILS
    print(("PASS " if ok else "FAIL ") + name)
    FAILS += 0 if ok else 1


def pack(items):
    return {"format": "memoinall/schedules", "version": 1, "count": len(items), "schedules": items}


ITEMS = [
    {"uid": "u-1", "title": "결제 시스템 점검 회의", "date": "2026-11-20", "time": "15:00", "note": "자료는 김과장",
     "tags": ["회의", "#결제"], "done": False, "created_at": "2026-11-17T08:00:00"},
    {"uid": "u-2", "title": "보고서 제출", "date": "2026-11-21", "time": "", "note": "", "tags": [], "done": True,
     "created_at": "2026-11-17T08:01:00"},
    {"uid": "u-3", "title": "  ", "date": "2026-11-22"},                       # 이름 없음
    {"uid": "u-4", "title": "날짜 이상", "date": "2026-13-40"},                 # 없는 날짜
    {"uid": "u-5", "title": "시각 이상", "date": "2026-11-23", "time": "25:99", "tags": ["좋은태그", "나쁜<태그>"]},
    {"uid": "u-1", "title": "같은 uid 두 번", "date": "2026-11-24"},            # 중복
    {"title": "uid 없는 예전 일정", "date": "2026-11-25", "created_at": "2026-11-01T00:00:00"},
]


def main():
    with patch.object(store, "start_worker"), patch.object(store, "enqueue"), TestClient(app) as client:
        db.init() if hasattr(db, "init") else None
        text = json.dumps(pack(ITEMS), ensure_ascii=False)

        p = schedule_receive.parse(text)
        titles = [i["title"] for i in p["items"]]
        check(titles == ["결제 시스템 점검 회의", "보고서 제출", "시각 이상", "uid 없는 예전 일정"] and p["skipped"] == 3,
              "invalid entries (no title, bad date, repeated uid) are skipped and counted")
        first = p["items"][0]
        check(first["time"] == "15:00" and first["tags"] == ["회의", "결제"] and first["note"] == "자료는 김과장",
              "time, tags (# stripped) and note are kept")
        third = p["items"][2]
        check(third["time"] == "" and third["tags"] == ["좋은태그"], "a bad time is dropped and unusable tag names are left out")
        check(p["items"][3]["key"] == schedule_receive.parse(text)["items"][3]["key"],
              "an entry without uid still gets a stable key from its content")

        bad = []
        for wrong in ('{"format":"memoinall/memos","memos":[]}', "not json", json.dumps(pack([{}] * 501))):
            try:
                schedule_receive.parse(wrong)
                bad.append(False)
            except exchange.ExchangeError:
                bad.append(True)
        check(all(bad), "memo files, broken text and over-500 batches are refused with a message")

        sealed = exchange.dumps(exchange.seal(pack(ITEMS[:2]), "현장 비번"))
        r = client.post("/api/schedule/receive/preview", json={"content": sealed})
        check(r.status_code == 200 and r.json().get("needs_password") is True, "a locked batch asks for the password")
        r = client.post("/api/schedule/receive/preview", json={"content": sealed, "password": "틀림"})
        check(r.status_code == 400 and "비밀번호" in r.json()["detail"], "a wrong password is explained")
        r = client.post("/api/schedule/receive/preview", json={"content": sealed, "password": "현장 비번"})
        check(r.status_code == 200 and len(r.json()["items"]) == 2 and not any(i["exists"] for i in r.json()["items"]),
              "the right password opens the batch for review")

        keys = [i["key"] for i in r.json()["items"]]
        r = client.post("/api/schedule/receive/apply", json={"content": sealed, "password": "현장 비번", "selected": keys})
        check(r.status_code == 200 and r.json()["imported"] == 2, "checked schedules are added to the PC")
        board = {t["title"]: t for t in todos.board(limit=50)["items"]}
        meeting = board.get("15:00 결제 시스템 점검 회의")
        report = db.one("SELECT status FROM todos WHERE title=? AND active=1", ("보고서 제출",))   # 끝낸 일은 기본 목록에 안 보인다
        check(meeting and meeting["scheduled_for"] == "2026-11-20" and meeting["status"] == "open"
              and sorted(meeting["tags"]) == ["결제", "회의"], "time goes in front of the name, date and tags are kept")
        memo = db.one("SELECT body,source FROM memos WHERE id=?", (meeting["memo_id"],)) if meeting else None
        check(memo and memo["source"] == "task" and "자료는 김과장" in memo["body"], "the note is stored in the schedule's memo")
        check(report and report["status"] == "done", "a schedule finished on the phone arrives as done")

        r = client.post("/api/schedule/receive/preview", json={"content": sealed, "password": "현장 비번"})
        check(all(i["exists"] for i in r.json()["items"]), "sending the same schedules again shows them as already there")
        r = client.post("/api/schedule/receive/apply", json={"content": sealed, "password": "현장 비번", "selected": keys})
        count = db.one("SELECT COUNT(*) AS n FROM todos WHERE title IN (?,?)", ("보고서 제출", "15:00 결제 시스템 점검 회의"))["n"]
        check(r.json()["imported"] == 0 and r.json()["skipped_existing"] == 2 and count == 2,
              "applying again adds nothing (no duplicates)")

        r = client.post("/api/schedule/receive/apply", json={"content": text, "selected": ["not-a-key"]})
        check(r.status_code == 400, "a selection that is not in the batch is refused")
        r = client.post("/api/schedule/receive/apply", json={"content": text, "selected": []})
        check(r.status_code == 400, "applying with nothing selected is refused")

        created = todos.create({"title": "메모 붙은 일정", "scheduled_for": "2026-11-30", "request_id": "6c1c3a52-6f7c-4c3e-9f1a-2b6a0f1d7e11",
                                "tags": [], "note": "덧붙인 설명"})
        body = db.one("SELECT body FROM memos WHERE id=?", (created["memo_id"],))["body"]
        check(body.endswith("덧붙인 설명"), "todos.create keeps an optional note in the memo body")
        try:
            todos.create({"title": "x", "scheduled_for": "2026-11-30", "request_id": "6c1c3a52-6f7c-4c3e-9f1a-2b6a0f1d7e12", "note": "가" * 5001})
            check(False, "an over-long note is refused")
        except ValueError:
            check(True, "an over-long note is refused")

    print("ALL PASS" if not FAILS else f"{FAILS} FAILED")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
