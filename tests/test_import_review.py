"""ENEX fidelity and reviewed import/export boundaries, using an isolated DB."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-review-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from memoinall import db, exchange, importers, store
from memoinall.api import app
from memoinall.importers.evernote import EvernoteError, EvernoteImporter


def enex(notes=None):
    if notes is None:
        notes = [
            '<note><title>인수인계</title><content><![CDATA[<?xml version="1.0"?>'
            '<!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd">'
            '<en-note><div>첫 문단 &amp; 설명</div><div><en-todo checked="true"/>완료</div>'
            '<div><en-todo/>할 일</div><a href="https://example.com/a">참고</a>'
            '<en-media type="image/png" hash="abc"/><en-crypt>SECRET CIPHERTEXT</en-crypt>'
            '<script>DO NOT IMPORT</script></en-note>]]></content>'
            '<created>20250102T030405Z</created><updated>20250103T040506Z</updated>'
            '<tag>팀 업무</tag><tag>日本語</tag><resource><data encoding="base64">YWJj</data>'
            '<mime>image/png</mime><resource-attributes><file-name>화면.png</file-name></resource-attributes></resource></note>',
            '<note><title>개인 메모</title><content><![CDATA[<en-note>공유하지 않을 내용</en-note>]]></content></note>',
        ]
    return '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE en-export SYSTEM "http://xml.evernote.com/pub/evernote-export4.dtd"><en-export>' + ''.join(notes) + '</en-export>'


class ImportReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = TestClient(app)
        cls.client = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)
        db.close()

    def setUp(self):
        for source in ("evernote", "shared", "review-test"):
            store.delete_by_source(source)

    def preview(self, **kwargs):
        payload = {"source": "evernote", "evernote_content": enex(), "review": True, **kwargs}
        response = self.client.post("/api/import/preview", json=payload)
        self.assertEqual(response.status_code, 200)
        return payload, response.json()["results"][0]

    def test_enex_text_metadata_and_visible_losses(self):
        notes = EvernoteImporter(content=enex()).read()
        self.assertEqual(len(notes), 2)
        note = notes[0]
        for part in ("인수인계", "첫 문단 & 설명", "[x] 완료", "[ ] 할 일", "https://example.com/a", "화면.png", "日本語"):
            self.assertIn(part, note.body)
        self.assertNotIn("SECRET CIPHERTEXT", note.body)
        self.assertNotIn("DO NOT IMPORT", note.body)
        self.assertEqual(note.tags, ["팀_업무"])
        self.assertEqual(len(note.warnings), 3)
        expected = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc).astimezone().replace(tzinfo=None).isoformat()
        self.assertEqual(note.created_at, expected)
        self.assertIsNone(notes[1].created_at)
        path = Path(TMP.name) / "sample.enex"
        path.write_text(enex(), encoding="utf-8-sig")
        self.assertEqual(EvernoteImporter(path).read(), notes)

    def test_invalid_xml_entities_and_limit_are_rejected(self):
        for content in ('<other/>', '<en-export>', '<!DOCTYPE en-export [<!ENTITY x SYSTEM "file:///secret">]><en-export>&x;</en-export>'):
            with self.subTest(content=content), self.assertRaises(EvernoteError):
                EvernoteImporter(content=content).read()
        with patch("memoinall.importers.evernote.MAX_BYTES", 10), self.assertRaises(EvernoteError):
            EvernoteImporter(content=enex()).read()

    def test_preview_selection_empty_and_repeat_import(self):
        payload, result = self.preview()
        self.assertEqual(result["importable"], 2)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(store.count_by_source("evernote")["memos"], 0)
        chosen = {**payload, "revision": result["revision"], "selected_keys": []}
        self.assertEqual(self.client.post("/api/import/run", json=chosen).json()["total"], 0)
        chosen["selected_keys"] = ["0"]
        saved = self.client.post("/api/import/run", json=chosen).json()
        self.assertEqual(saved["total"], 1)
        self.assertEqual(store.count_by_source("evernote")["memos"], 1)
        again = self.client.post("/api/import/preview", json=payload).json()["results"][0]
        self.assertEqual([m["eligible"] for m in again["items"]], [False, True])
        self.assertEqual(self.client.post("/api/import/run", json=chosen).json()["total"], 0)

    def test_changed_file_options_and_invalid_selection_never_write(self):
        payload, result = self.preview()
        chosen = {**payload, "revision": result["revision"], "selected_keys": ["0"]}
        for change in ({"evernote_content": enex().replace("인수인계", "바뀐 메모")}, {"tags": ["새태그"]}, {"selected_keys": ["999"]}):
            response = self.client.post("/api/import/run", json={**chosen, **change}).json()
            self.assertTrue(response["results"][0]["error"])
            self.assertEqual(response["total"], 0)
        for change in ({"selected_keys": [0]}, {"selected_keys": "0"}, {"revision": ""}):
            self.assertEqual(self.client.post("/api/import/run", json={**chosen, **change}).status_code, 400)
        self.assertEqual(store.count_by_source("evernote")["memos"], 0)

    def test_identical_entries_within_file_are_deduplicated(self):
        content = enex(['<note><title>같음</title><content><![CDATA[<en-note>내용</en-note>]]></content></note>'] * 2)
        payload, result = self.preview(evernote_content=content)
        self.assertEqual(result["importable"], 1)
        self.assertEqual(result["skipped_duplicate"], 1)
        self.assertEqual(self.client.post("/api/import/run", json=payload).json()["total"], 1)

    def test_encrypted_shared_file_selected_import(self):
        packet = {"format": exchange.FORMAT, "version": exchange.VERSION, "memos": [
            {"uid": "outside-a", "body": "보낼 자료"}, {"uid": "outside-b", "body": "개인 정보"}]}
        content = exchange.dumps(exchange.seal(packet, "password"))
        payload, result = self.preview(source="shared", shared_content=content, shared_password="password")
        saved = self.client.post("/api/import/run", json={**payload, "selected_keys": ["0"], "revision": result["revision"]}).json()
        self.assertEqual(saved["total"], 1)
        self.assertEqual([m["body"] for m in store.list_memos() if m["source"] == "shared"], ["보낼 자료"])

    def test_export_includes_all_candidates_and_only_reviewed_ids(self):
        ids = [store.add_memo(f"검토할 메모 {i}", source="review-test", enqueue_enrich=False)["id"] for i in range(35)]
        result = self.client.post("/api/export/preview", json={"ids": ids}).json()
        self.assertEqual(len(result["items"]), 35)
        chosen = result["items"][-1]
        payload = {"ids": [chosen["id"]], "revisions": {str(chosen["id"]): chosen["revision"]}}
        store.add_memo("미리보기 후 추가한 내용", source="review-test", enqueue_enrich=False)
        exported = self.client.post("/api/export/content", json=payload).json()
        self.assertEqual(json.loads(exported["content"])["memos"][0]["body"], chosen["body"])
        self.assertEqual(exported["count"], 1)
        store.update_memo(chosen["id"], "미리보기 후 수정", enqueue_enrich=False)
        self.assertEqual(self.client.post("/api/export/content", json=payload).status_code, 409)
        store.delete_memo(chosen["id"])
        self.assertEqual(self.client.post("/api/export/content", json=payload).status_code, 409)
        self.assertEqual(self.client.post("/api/export/content", json={"ids": [], "revisions": {}}).status_code, 400)

    def test_review_limits_are_explicit_not_silently_truncated(self):
        content = enex(['<note><title>메모</title><content/></note>'] * 1001)
        _, result = self.preview(evernote_content=content)
        self.assertIn("1,000", result["error"])
        with patch("memoinall.api.exchange.select", return_value=[{}] * 1001):
            response = self.client.post("/api/export/preview", json={})
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
