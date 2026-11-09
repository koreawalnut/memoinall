"""Import metadata repairs must preserve edits, dates and sharing deduplication."""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-metadata-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from memoinall import db, documents, exchange, importers, store
from memoinall.api import app
from memoinall.importers import Note
from memoinall.importers.redmine import RedmineImporter
from memoinall.importers.shared import SharedFileImporter
from memoinall.importers.sticky import StickyNotesImporter
from memoinall.source_tags import SOURCE_TAGS


class Source:
    path = "fixture"
    label = "fixture"

    def __init__(self, name, notes):
        self.name, self.notes = name, notes

    def available(self):
        return True

    def read(self):
        return self.notes


class ImportMetadataTests(unittest.TestCase):
    def setUp(self):
        db.init()
        db.execute("DELETE FROM memos")
        db.execute("DELETE FROM memo_fts")
        db.execute("DELETE FROM meta WHERE key='source_tags_v1'")

    def run_import(self, source, **kwargs):
        return importers.run_import(source, background_enrich=False, **kwargs)

    def test_preview_matches_saved_tags_and_filters_without_enrichment(self):
        for source, tag in SOURCE_TAGS.items():
            with self.subTest(source=source):
                note = Note(source, "원래 내용 #업무", created_at="2025-01-02T14:30:00", tags=["업무", "팀"])
                imp = Source(source, [note])
                before = store.stats()["memos"]
                preview = self.run_import(imp, dry_run=True, review=True)
                self.assertEqual(store.stats()["memos"], before)
                self.assertIn("#" + tag, preview.items[0]["body"])
                result = self.run_import(imp, dry_run=False)
                saved = store.get_memo(result.memo_ids[0])
                self.assertEqual(saved["body"], preview.items[0]["body"])
                self.assertEqual(saved["body"].count("#업무"), 1)
                self.assertEqual(set(saved["facets"]["tag"]), {"업무", "팀", tag})
                self.assertEqual([m["id"] for m in store.list_memos(tag=tag)], [saved["id"]])
                self.assertEqual(saved["updated_at"], note.created_at)
                self.assertIsNone(saved["enriched_at"])

    def test_existing_source_tag_is_case_insensitive_and_requires_a_token(self):
        a = store.add_memo("원본 #redmine", source="redmine", enqueue_enrich=False)
        self.assertEqual(a["body"], "원본 #redmine")
        b = store.add_memo("https://example.com/#Redmine", source="redmine", enqueue_enrich=False)
        self.assertTrue(b["body"].endswith("\n#Redmine"))

    def legacy_memo(self, source="sticky", body="직접 보완한 내용 #업무"):
        memo = store.add_memo(body, created_at="2025-01-02T05:00:00",
                              updated_at="2025-01-03T06:00:00", enqueue_enrich=False)
        db.execute("UPDATE memos SET source=?,external_id='old' WHERE id=?", (source, memo["id"]))
        return store.get_memo(memo["id"])

    def test_backfill_is_once_preserves_dates_flags_and_sharing(self):
        old = self.legacy_memo()
        payload = exchange.dumps(exchange.build([old]))
        store.set_flag(old["id"], pinned=True, archived=True)
        self.assertEqual(store.backfill_source_tags(), 1)
        new = store.get_memo(old["id"])
        self.assertEqual(new["body"], old["body"] + "\n#스티커메모")
        for field in ("title", "created_at", "updated_at"):
            self.assertEqual(new[field], old[field])
        self.assertEqual((new["pinned"], new["archived"]), (1, 1))
        self.assertIn("스티커메모", new["facets"]["tag"])
        self.assertEqual(self.run_import(SharedFileImporter(content=payload), dry_run=False).skipped_duplicate, 1)
        store.update_memo(old["id"], "직접 고치고 태그 삭제", enqueue_enrich=False)
        self.assertEqual(store.backfill_source_tags(), 0)
        self.assertEqual(store.get_memo(old["id"])["body"], "직접 고치고 태그 삭제")
        # Once a person changes the content, the old file is a distinct version again.
        self.assertEqual(self.run_import(SharedFileImporter(content=payload), dry_run=False).imported, 1)

    def test_new_import_alias_matches_old_file_but_not_manual_changes(self):
        raw = "오래된 공유본 #업무"
        memo = store.add_memo(raw, source="evernote", created_at="2025-01-02T10:00:00", enqueue_enrich=False)
        old_uid = exchange.content_uid(memo["created_at"], raw)
        self.assertIn(old_uid, store.content_uids())
        store.update_memo(memo["id"], memo["body"] + "\n추가한 결정", enqueue_enrich=False)
        self.assertNotIn(old_uid, store.content_uids())
        store.delete_memo(memo["id"])
        self.assertEqual(db.query("SELECT * FROM memo_import_aliases"), [])

    def time_source(self):
        return Source("redmine", [Note("old", "서버에 있는 다른 본문",
            created_at="2025-01-02T14:00:00", updated_at="2025-01-03T15:00:00",
            legacy_created_at="2025-01-02T05:00:00", legacy_updated_at="2025-01-03T06:00:00")])

    def test_reimport_repairs_only_times_and_is_idempotent(self):
        old = self.legacy_memo(source="redmine")
        payload = exchange.dumps(exchange.build([old]))
        store.backfill_source_tags()
        source = self.time_source()
        preview = self.run_import(source, dry_run=True, review=True)
        self.assertEqual(preview.corrected_times, 1)
        self.assertEqual(preview.items[0]["status"], "시간 바로잡기")
        self.assertEqual(store.get_memo(old["id"])["created_at"], old["created_at"])
        saved_body = store.get_memo(old["id"])["body"]
        actual = self.run_import(source, dry_run=False)
        self.assertEqual((actual.imported, actual.updated, actual.corrected_times), (0, 0, 1))
        new = store.get_memo(old["id"])
        self.assertEqual((new["created_at"], new["updated_at"]), ("2025-01-02T14:00:00", "2025-01-03T15:00:00"))
        self.assertEqual(new["body"], saved_body)
        self.assertEqual(self.run_import(source, dry_run=False).corrected_times, 0)
        self.assertEqual(self.run_import(SharedFileImporter(content=payload), dry_run=False).skipped_duplicate, 1)

    def test_reimport_preserves_manual_edit_time_and_unverified_dates(self):
        old = self.legacy_memo(source="redmine")
        edited = store.update_memo(old["id"], "사용자가 수정한 본문", enqueue_enrich=False)
        self.assertEqual(self.run_import(self.time_source(), dry_run=False).corrected_times, 1)
        new = store.get_memo(old["id"])
        self.assertEqual(new["updated_at"], edited["updated_at"])
        self.assertEqual(new["body"], edited["body"])
        source = Source("redmine", [Note("old", "원본", created_at="2025-01-01T00:00:00")])
        self.assertEqual(self.run_import(source, dry_run=False).corrected_times, 0)
        self.assertEqual(store.get_memo(old["id"])["created_at"], new["created_at"])

    def test_api_explains_time_corrections_in_preview_and_result(self):
        self.legacy_memo(source="redmine")
        with patch.object(importers, "get_importer", return_value=self.time_source()), patch.object(store, "enqueue"):
            client = TestClient(app)
            for path in ("preview", "run"):
                response = client.post("/api/import/" + path, json={"source": "redmine"})
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual(result["total_corrected_times"], 1)
                self.assertEqual(result["total_updated"], 0)
            client.close()

    def test_aware_times_convert_to_local_and_naive_times_stay_local(self):
        for value in ("2025-01-02T23:30:00Z", "2025-01-02T01:00:00+14:00", "2025-01-02T23:00:00-08:00"):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            self.assertEqual(importers.parse_any_time(value), parsed.astimezone().replace(tzinfo=None).isoformat())
            self.assertEqual(importers.legacy_aware_time(value), parsed.replace(tzinfo=None).isoformat())
        self.assertEqual(importers.parse_any_time("2025-01-02T23:30:00"), "2025-01-02T23:30:00")
        for value in ("2025-01-02T23:30:00", "invalid", 638000000000000000, None):
            self.assertIsNone(importers.legacy_aware_time(value))

    def test_importers_retain_original_timezone_evidence(self):
        value = "2025-01-02T23:30:00Z"
        redmine = RedmineImporter()._issue_note({"id": 1, "subject": "issue", "created_on": value})
        self.assertEqual(redmine.created_at, importers.parse_any_time(value))
        self.assertEqual(redmine.legacy_created_at, "2025-01-02T23:30:00")
        path = Path(TMP.name) / "plum.sqlite"
        conn = sqlite3.connect(path)
        with conn:
            conn.execute("CREATE TABLE Note(Id TEXT, Text TEXT, CreatedAt TEXT, UpdatedAt TEXT)")
            conn.execute("INSERT INTO Note VALUES('one','memo',?,?)", (value, value))
        conn.close()
        sticky = StickyNotesImporter(path).read()[0]
        self.assertEqual(sticky.created_at, redmine.created_at)
        self.assertEqual(sticky.legacy_updated_at, redmine.legacy_created_at)

    def test_document_tags_are_immediately_searchable(self):
        with patch.object(store, "enqueue"):
            result = documents.save("guide.pdf", [{"key": "p1", "label": "1쪽", "text": "읽어 둔 문서"}])
        memo_id = result["memos"][0]["id"]
        self.assertIn(memo_id, [m["id"] for m in store.list_memos(tag="문서")])
        self.assertIn(memo_id, [m["id"] for m in store.list_memos(tag="PDF")])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        db.close()
