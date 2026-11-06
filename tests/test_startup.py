"""시작 중에도 메모를 사용할 수 있는지 검증. python tests/test_startup.py"""

from __future__ import annotations

import gc
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["MEMOINALL_DISABLE_ST"] = "1"

from fastapi.testclient import TestClient
from memoinall import api, config, db, desktop, embed, store


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="memoinall-startup-")
        self.addCleanup(self.cleanup_temporary)
        home = Path(self.temporary.name)
        for target, name, value in (
            (config, "HOME", home), (config, "DB_PATH", home / "memo.db"),
            (embed, "_current", embed.HashEmbedder()), (embed, "_state", "loading"),
            (store, "_jobs", queue.Queue()),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        worker = patch.object(store, "start_worker")
        worker.start()
        self.addCleanup(worker.stop)
        db.close()
        self.addCleanup(db.close)
        store.invalidate_cache()

    def cleanup_temporary(self):
        # 종료된 TestClient 워커의 sqlite 커넥션은 GC 때 파일 핸들을 해제한다.
        gc.collect()
        self.temporary.cleanup()

    def test_loading_and_failure_preserve_existing_semantic_vectors(self):
        saved = store.add_memo("기존 결제 메모", enqueue_enrich=False)["id"]
        store.enrich(saved)
        db.execute("UPDATE chunks SET model=? WHERE memo_id=?", (config.EMBED_MODEL, saved))
        original = [tuple(row) for row in db.query("SELECT * FROM chunks WHERE memo_id=?", (saved,))]
        pending = store.add_memo("미처리 메모", enqueue_enrich=False)["id"]
        for state in ("loading", "downloading", "failed"):
            with self.subTest(state=state), patch.object(embed, "_state", state):
                self.assertEqual(store.reindex_pending(), 1)
                self.assertEqual(store._jobs.get_nowait(), pending)
                self.assertTrue(store._jobs.empty())
                self.assertEqual(original, [tuple(row) for row in db.query(
                    "SELECT * FROM chunks WHERE memo_id=?", (saved,))])
        store.enrich(pending)
        with patch.object(embed, "_current", SimpleNamespace(name=config.EMBED_MODEL)):
            self.assertEqual(store.reindex_pending(), 1)
            self.assertEqual(store._jobs.get_nowait(), pending)
            self.assertTrue(store._jobs.empty())

    def test_upgrade_during_encoding_requeues_the_inflight_memo(self):
        mid = store.add_memo("작성 중에도 모델이 준비된다", enqueue_enrich=False)["id"]
        fallback = embed.HashEmbedder()

        def encode(pieces):
            embed._current = SimpleNamespace(name=config.EMBED_MODEL)
            return fallback.encode(pieces), fallback.name

        with patch.object(embed, "encode", side_effect=encode), patch.object(store, "enqueue") as enqueue:
            self.assertTrue(store.enrich(mid))
            enqueue.assert_called_once_with(mid)

    def test_slow_search_preparation_does_not_block_capture_or_keyword_search(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        prepare = api._prepare_search

        def tracked_prepare():
            try:
                prepare()
            finally:
                finished.set()

        def slow_loader():
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test did not release loader")

        with patch.object(embed, "ensure_loaded_async", side_effect=slow_loader), \
             patch.object(api, "_prepare_search", side_effect=tracked_prepare):
            with TestClient(api.app) as client:
                try:
                    self.assertEqual(client.get("/api/health").json(), {"status": "ok"})
                    self.assertTrue(entered.wait(5))
                    self.assertFalse(release.is_set())
                    self.assertEqual(client.get("/").status_code, 200)
                    client.put("/api/draft", json={"body": "복원할 초안"})
                    self.assertEqual(client.get("/api/draft").json()["body"], "복원할 초안")
                    memo = client.post("/api/memos", json={"body": "기다리지 않고 결제 메모 저장"}).json()
                    self.assertEqual(client.get("/api/memos").json()["items"][0]["id"], memo["id"])
                    hits = client.get("/api/search", params={"q": "결제"}).json()["items"]
                    self.assertEqual(hits[0]["id"], memo["id"])
                    self.assertEqual(hits[0]["why"], "키워드")
                finally:
                    release.set()
                    self.assertTrue(finished.wait(5))
        self.assertNotIn(api._model_upgraded, embed._on_upgrade)

    def test_gui_starts_before_waiting_for_server(self):
        view = MagicMock()
        window = view.create_window.return_value
        order = []

        def start(callback, args):
            order.append("gui")
            callback(*args)

        def wait(*args, **kwargs):
            self.assertEqual(order, ["gui"])
            order.append("health")
            return True

        view.start.side_effect = start
        old_hook, old_thread_hook = sys.excepthook, threading.excepthook
        try:
            with patch.dict(sys.modules, {"webview": view}), \
                 patch.object(desktop, "_single_instance_lock", return_value=MagicMock()), \
                 patch.object(desktop, "_log_webview_assets"), \
                 patch.object(desktop, "_install_downloader"), \
                 patch.object(desktop, "_wait_until_up", side_effect=wait), \
                 patch.object(desktop.threading, "Thread"), \
                 patch.object(desktop.logging, "basicConfig"):
                self.assertEqual(desktop.run(), 0)
        finally:
            sys.excepthook, threading.excepthook = old_hook, old_thread_hook
        self.assertEqual(order, ["gui", "health"])
        self.assertIn("메모장을 여는 중", view.create_window.call_args.kwargs["html"])
        window.load_url.assert_called_once()

    def test_closed_window_and_failed_server_do_not_wait_for_timeout(self):
        closed = threading.Event()
        closed.set()
        window = MagicMock()
        desktop._open_when_ready(window, 1, closed, threading.Event())
        window.load_url.assert_not_called()
        window.load_html.assert_not_called()
        failed = threading.Event()
        failed.set()
        desktop._open_when_ready(window, 1, threading.Event(), failed)
        self.assertIn("메모장을 열지 못했습니다", window.load_html.call_args.args[0])

    def test_real_desktop_server_responds_and_saves(self):
        port = desktop.free_port()
        environment = os.environ.copy()
        environment["MEMOINALL_HOME"] = self.temporary.name
        environment["MEMOINALL_DISABLE_ST"] = "1"
        code = f"from memoinall.desktop import _serve; _serve({port})"
        started = time.monotonic()
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", code], cwd=ROOT, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            if not desktop._wait_until_up(port, timeout=15):
                process.terminate()
                output, errors = process.communicate(timeout=10)
                self.fail((output + errors).decode("utf-8", errors="replace"))
            print(f"\n실제 서버 HTTP 준비: {time.monotonic() - started:.3f}s", flush=True)
            opener = build_opener(ProxyHandler({}))
            request = Request(f"http://127.0.0.1:{port}/api/memos",
                              data=json.dumps({"body": "시작 직후 저장"}).encode(),
                              headers={"Content-Type": "application/json"})
            with opener.open(request, timeout=5) as response:
                self.assertEqual(json.load(response)["body"], "시작 직후 저장")
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
