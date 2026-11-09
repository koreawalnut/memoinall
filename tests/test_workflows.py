"""Reusable task settings, portable files and date-sensitive source boundaries."""
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

TMP = tempfile.TemporaryDirectory(prefix="memoinall-workflows-", ignore_cleanup_errors=True)
os.environ["MEMOINALL_HOME"] = TMP.name
os.environ["MEMOINALL_DISABLE_ST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from memoinall import db, desktop, llm, store, workflows
from memoinall.api import app


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        db.init()
        db.execute("DELETE FROM workflows")
        db.execute("DELETE FROM memos")
        db.execute("DELETE FROM memo_fts")
        store.invalidate_cache()
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.task = {"name": "팀 주간보고", "instruction": "회의 내용을 보고서로 정리해줘", "format": "report",
                     "tags": ["업무"], "period": "this_week", "budget": 6000}

    def create(self, **changes):
        result = self.client.post("/api/generate/workflows", json={**self.task, **changes})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_save_persistence_update_conflicts_and_delete_leave_memos_alone(self):
        memo = store.add_memo("원래 메모 #업무", enqueue_enrich=False)
        task = self.create()
        db.close()
        self.assertEqual(workflows.get(task["id"])["instruction"], self.task["instruction"])
        self.assertEqual(self.client.post("/api/generate/workflows", json=self.task).status_code, 400)
        url = f'/api/generate/workflows/{task["id"]}'
        update = {"version": task["version"], "settings": {**self.task, "name": "업데이트한 업무"}}
        saved = self.client.put(url, json=update).json()
        self.assertEqual(saved["version"], 2)
        self.assertEqual(self.client.put(url, json=update).status_code, 409)
        self.assertEqual(self.client.delete(url, params={"version": 1}).status_code, 409)
        self.assertEqual(self.client.delete(url, params={"version": 2}).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(store.get_memo(memo["id"])["body"], memo["body"])

    def test_share_round_trip_contains_only_settings_and_preview_does_not_write(self):
        original = self.create()
        store.add_memo("DO NOT EXPORT THIS MEMO", enqueue_enrich=False)
        file = self.client.post("/api/generate/workflows/export", json=self.task).json()
        document = json.loads(file["content"])
        self.assertEqual(set(document), {"format", "version", "workflow"})
        self.assertEqual(set(document["workflow"]), set(workflows.FIELDS))
        self.assertNotIn("DO NOT EXPORT", file["content"])
        self.assertNotIn("memo_ids", file["content"])
        self.assertIsNone(document["workflow"]["since"])
        preview = self.client.post("/api/generate/workflows/preview", json={"content": file["content"]}).json()
        self.assertEqual(preview["workflow"]["name"], "팀 주간보고 (2)")
        self.assertEqual(preview["missing_tags"], ["업무"])
        self.assertEqual(len(workflows.all_workflows()), 1)
        preview["workflow"]["tags"] = ["다른팀"]
        imported = self.client.post("/api/generate/workflows", json=preview["workflow"]).json()
        self.assertEqual(imported["tags"], ["다른팀"])
        self.assertEqual(workflows.get(original["id"])["tags"], ["업무"])

    def test_invalid_files_and_settings_cannot_add_hidden_options(self):
        for changes in ({"api_key": "secret"}, {"memo_ids": [1]}, {"budget": True}, {"tags": "업무"},
                        {"format": "unsupported"}, {"period": []}, {"instruction": ""},
                        {"tags": ["<script>"]}, {"period": "custom", "since": "2026-02-31"},
                        {"period": "custom", "since": "2026-03-02", "until": "2026-03-01"}):
            with self.subTest(changes=changes):
                response = self.client.post("/api/generate/workflows", json={**self.task, **changes})
                self.assertEqual(response.status_code, 400, response.text)
        for content in ("{", "[]", "x" * 100001, json.dumps({"format": "memoinall/memos", "version": 1, "memos": []}),
                        json.dumps({"format": workflows.FILE_FORMAT, "version": True, "workflow": self.task}),
                        json.dumps({"format": workflows.FILE_FORMAT, "version": 1, "workflow": {**self.task, "api_key": "secret"}})):
            self.assertEqual(self.client.post("/api/generate/workflows/preview", json={"content": content}).status_code, 400)
        self.assertEqual(workflows.all_workflows(), [])

    def test_relative_dates_cover_week_year_month_and_leap_boundaries(self):
        cases = [(date(2026, 11, 9), "this_week", ("2026-11-09", "2026-11-09")),
                 (date(2026, 11, 15), "this_week", ("2026-11-09", "2026-11-15")),
                 (date(2027, 1, 1), "last_week", ("2026-12-21", "2026-12-27")),
                 (date(2028, 3, 1), "last_30_days", ("2028-02-01", "2028-03-01")),
                 (date(2028, 2, 29), "this_month", ("2028-02-01", "2028-02-29"))]
        for day, period, expected in cases:
            with self.subTest(day=day, period=period), patch.object(workflows, "today", return_value=day):
                self.assertEqual(workflows.period_bounds(period), expected)
        self.assertEqual(workflows.period_bounds("custom", "2025-03-01", None), ("2025-03-01", None))
        self.assertEqual(workflows.period_bounds("all", "2025-03-01", "2025-03-02"), (None, None))

    def test_same_saved_task_resolves_new_dates_for_search_and_generation(self):
        old = store.add_memo("회의: 이전 주 #업무", created_at="2026-11-03T10:00:00", enqueue_enrich=False)
        first = store.add_memo("회의: 이번 주 #업무", created_at="2026-11-09T10:00:00", enqueue_enrich=False)
        later = store.add_memo("회의: 다음 주 #업무", created_at="2026-11-16T10:00:00", enqueue_enrich=False)
        other = store.add_memo("회의: 개인 자료 #개인", created_at="2026-11-16T10:00:00", enqueue_enrich=False)
        task = self.create()
        for day, expected in ((date(2026, 11, 9), first), (date(2026, 11, 16), later)):
            with patch.object(workflows, "today", return_value=day), patch.object(llm, "available", return_value=False):
                for q in ("", "회의"):
                    found = self.client.get("/api/generate/memos", params={"tags": "업무", "period": "this_week", "q": q}).json()
                    self.assertEqual([m["id"] for m in found["items"]], [expected["id"]])
                # Dates from an earlier use cannot override the saved relative period.
                result = self.client.post("/api/generate", json={**task, "queries": ["회의"], "since": "1900-01-01"}).json()
                self.assertEqual([m["id"] for m in result["sources"]], [expected["id"]])
                for omitted in (old, other):
                    self.assertNotIn(omitted["body"], result["prompt"])
        self.assertIsNone(workflows.get(task["id"])["since"])

    def test_native_export_uses_json_filter_and_preserves_markdown_download(self):
        fake = SimpleNamespace(SAVE_DIALOG=1)
        for suffix, label in (("json", "업무 설정 (*.json)"), ("md", "마크다운 (*.md)")):
            target = Path(TMP.name) / f"download.{suffix}"
            window = MagicMock()
            window.create_file_dialog.return_value = (str(target),)
            with patch.dict(sys.modules, {"webview": fake}):
                desktop._install_downloader(window)
                save = window.expose.call_args.args[0]
                self.assertTrue(save("한글\n내용", target.name))
            self.assertEqual(window.create_file_dialog.call_args.kwargs["file_types"][0], label)
            self.assertEqual(target.read_text(encoding="utf-8"), "한글\n내용")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        db.close()
