"""휴지통: 지운 메모는 어디에도 안 보이고, 되살리면 같은 번호·일정 결정·연결이 돌아온다."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-trash-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memoinall import db, search, store, todos, trash  # noqa: E402


@patch.object(store, "start_worker")       # 분석은 테스트가 직접 store.enrich 로 돌린다
class TrashTests(unittest.TestCase):
    def setUp(self):
        for table in ("memo_trash", "ai_versions", "ai_results", "collections", "memos", "memo_fts"):
            db.execute(f"DELETE FROM {table}")
        while store.queue_size():
            store._jobs.get_nowait()

    def add(self, body, **kw):
        memo = store.add_memo(body, enqueue_enrich=False, **kw)
        store.enrich(memo["id"])
        return memo

    def test_deleted_memo_disappears_and_comes_back_with_decisions(self, _worker):
        memo = self.add("결제 장애 회고 #결제\n[ ] 로그 3일치 뽑기 금요일까지")
        keep = self.add("다른 메모 #결제")
        store.set_flag(memo["id"], pinned=True)
        todo = db.one("SELECT * FROM todos WHERE memo_id=? AND active=1", (memo["id"],))
        todos.update(todo["id"], {"status": "done"})
        todos.update(todo["id"], {"scheduled_for": "2026-11-27"})

        moved = trash.move(memo["id"])
        self.assertEqual(moved["id"], memo["id"])
        with self.assertRaises(KeyError):
            store.get_memo(memo["id"])
        self.assertIsNone(db.one("SELECT 1 FROM memo_fts WHERE rowid=?", (memo["id"],)))
        self.assertIsNone(db.one("SELECT 1 FROM todos WHERE memo_id=?", (memo["id"],)))     # 일정에서도 빠진다
        self.assertNotIn(memo["id"], [m["id"] for m in search.search("결제 장애", limit=10)])
        self.assertEqual([m["id"] for m in store.list_memos()], [keep["id"]])
        listed = trash.items()
        self.assertEqual([(i["id"], i["days_left"]) for i in listed], [(memo["id"], 30)])
        self.assertIn("로그 3일치", listed[0]["body"])

        restored = trash.restore(memo["id"])
        self.assertEqual((restored["id"], restored["body"], restored["created_at"], restored["pinned"]),
                         (memo["id"], memo["body"], memo["created_at"], 1))
        self.assertEqual(restored["facets"]["tag"], ["결제"])                               # 태그는 바로
        self.assertEqual(trash.items(), [])
        store.enrich(memo["id"])                                                            # 나머지는 다시 분석
        again = db.one("SELECT * FROM todos WHERE memo_id=? AND active=1", (memo["id"],))
        self.assertEqual((again["id"], again["status"], again["scheduled_for"]), (todo["id"], "done", "2026-11-27"))
        self.assertEqual([m["id"] for m in search.search("결제 장애", limit=10)][:1], [memo["id"]])
        self.assertEqual(store.list_memos()[0]["id"], memo["id"])                          # 고정도 그대로

    def test_links_from_collections_ai_results_and_import_aliases_are_restored(self, _worker):
        memo = self.add("취합 대표 메모")
        now = store.now_iso()
        cid = db.execute("INSERT INTO collections(title,memo_id,created_at,updated_at) VALUES('취합',?,?,?)",
                         (memo["id"], now, now)).lastrowid
        rid = db.execute("INSERT INTO ai_results(title,created_at,updated_at) VALUES('결과',?,?)", (now, now)).lastrowid
        db.execute("INSERT INTO ai_versions(result_id,version,body,pack_json,kind,created_at,memo_id) "
                   "VALUES(?,1,'본문','{}','generate',?,?)", (rid, now, memo["id"]))
        db.execute("INSERT INTO memo_import_aliases VALUES(?,?,?)", (memo["id"], "old-uid", "new-uid"))
        trash.move(memo["id"])
        self.assertIsNone(db.one("SELECT memo_id FROM collections WHERE id=?", (cid,))["memo_id"])
        self.assertIsNone(db.one("SELECT memo_id FROM ai_versions WHERE result_id=?", (rid,))["memo_id"])
        trash.restore(memo["id"])
        self.assertEqual(db.one("SELECT memo_id FROM collections WHERE id=?", (cid,))["memo_id"], memo["id"])
        self.assertEqual(db.one("SELECT memo_id FROM ai_versions WHERE result_id=?", (rid,))["memo_id"], memo["id"])
        self.assertEqual(dict(db.one("SELECT original_uid,tagged_uid FROM memo_import_aliases WHERE memo_id=?", (memo["id"],))),
                         {"original_uid": "old-uid", "tagged_uid": "new-uid"})

    def test_reimported_original_blocks_restore_and_stays_in_trash(self, _worker):
        memo = self.add("에버노트에서 가져온 메모", source="evernote", external_id="note-1")
        trash.move(memo["id"])
        again = self.add("에버노트에서 다시 가져온 메모", source="evernote", external_id="note-1")
        with self.assertRaisesRegex(trash.Conflict, "다시 가져온"):
            trash.restore(memo["id"])
        self.assertEqual([i["id"] for i in trash.items()], [memo["id"]])
        self.assertTrue(store.get_memo(again["id"])["body"].startswith("에버노트에서 다시 가져온 메모"))

    def test_purge_empty_and_thirty_day_expiry(self, _worker):
        a, b, c = (self.add(f"메모 {n}") for n in "가나다")
        for memo in (a, b, c):
            trash.move(memo["id"])
        trash.purge(a["id"])
        with self.assertRaises(KeyError):
            trash.purge(a["id"])
        with self.assertRaises(KeyError):
            trash.restore(a["id"])
        # 지운 지 29일째: 하루 남음. 30일이 지나면 저절로 지워진다.
        db.execute("UPDATE memo_trash SET deleted_at=? WHERE memo_id=?",
                   ((datetime.now() - timedelta(days=29, hours=1)).replace(microsecond=0).isoformat(), b["id"]))
        self.assertEqual({i["id"]: i["days_left"] for i in trash.items()}, {b["id"]: 1, c["id"]: 30})
        self.assertEqual(trash.purge_expired(datetime.now() + timedelta(days=1)), 1)
        self.assertEqual([i["id"] for i in trash.items()], [c["id"]])
        self.assertEqual(trash.empty(), 1)
        self.assertEqual(trash.items(), [])
        with self.assertRaises(KeyError):
            trash.move(a["id"])

    def test_restore_survives_columns_added_or_removed_later(self, _worker):
        """앱이 바뀌어 열이 늘거나 줄어도 되살린다 — 지금 표에 있는 열만 넣는다."""
        memo = self.add("오래전에 지운 메모")
        trash.move(memo["id"])
        row = db.one("SELECT saved_json FROM memo_trash WHERE memo_id=?", (memo["id"],))
        saved = json.loads(row["saved_json"])
        saved["memo"]["removed_column"] = "옛 값"
        saved["memo"].pop("todo_version")
        db.execute("UPDATE memo_trash SET saved_json=? WHERE memo_id=?", (json.dumps(saved), memo["id"]))
        self.assertEqual(trash.restore(memo["id"])["body"], "오래전에 지운 메모")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        db.close()
