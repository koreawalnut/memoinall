"""Exercise actual always-on-top forms on a private Windows desktop and a temporary DB."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(sys.platform == "win32", "Windows desktop feature")
class MemoWindowTests(unittest.TestCase):
    def test_window_lifecycle(self):
        from memoinall import config, db, store
        from memoinall.memo_windows import MemoWindows

        user = ctypes.WinDLL("user32", use_last_error=True)
        user.CreateDesktopW.restype = wintypes.HANDLE
        user.SetThreadDesktop.argtypes = user.CloseDesktop.argtypes = [wintypes.HANDLE]
        desktop = user.CreateDesktopW(f"memo-windows-{os.getpid()}", None, None, 0, 0x1ff, None)
        self.assertTrue(desktop, ctypes.get_last_error())
        self.addCleanup(user.CloseDesktop, desktop)
        errors = []

        def exercise():
            try:
                self.assertTrue(user.SetThreadDesktop(desktop))
                import clr
                clr.AddReference("System.Windows.Forms")
                from System.Diagnostics import Process
                from System.Windows.Forms import Application, Form, FormWindowState, Keys

                def wait_saved(editor):
                    deadline = time.monotonic() + 5
                    while editor.pending and time.monotonic() < deadline:
                        Application.DoEvents()
                        time.sleep(.01)
                    self.assertIsNone(editor.pending)

                parent = Form()
                parent.Show()
                windows = MemoWindows(SimpleNamespace(native=parent, evaluate_js=lambda _js: None))
                windows.restore()
                self.assertIsNone(windows.timer)
                notes = [store.add_memo(f"원문 {i}\n다음 줄", enqueue_enrich=False)["id"] for i in range(6)]
                process = Process.GetCurrentProcess()
                before = process.PrivateMemorySize64
                for memo_id in notes:
                    windows.open_memo_window(memo_id)
                Application.DoEvents()
                process.Refresh()
                print(f"Six native editors: private bytes delta {(process.PrivateMemorySize64-before)/1048576:.1f} MiB")
                self.assertEqual(len(windows.forms), len(notes))
                editor = windows.forms[notes[0]]
                first, body = editor.form, editor.body
                self.assertTrue(first.TopMost)
                self.assertFalse(body.ReadOnly)
                self.assertEqual(body.MaxLength, 0)
                self.assertEqual(body.Lines.Length, 2)
                self.assertIsNone(first.Owner)
                parent.WindowState = FormWindowState.Minimized
                Application.DoEvents()
                self.assertTrue(first.Visible)
                first.WindowState = FormWindowState.Minimized
                windows.open_memo_window(notes[0])
                self.assertEqual(first.WindowState, FormWindowState.Normal)
                self.assertEqual(windows.forms[notes[0]].form, first)
                first.Left -= 18
                first.Width += 10
                # WinForms persists geometry at the end of a move/resize gesture.
                user.SendMessageW(wintypes.HWND(first.Handle.ToInt64()), 0x0232, 0, 0)
                saved_bounds = first.Bounds
                self.assertEqual(db.one("SELECT x FROM memo_windows WHERE memo_id=?", (notes[0],))[0], first.Left)
                windows._refresh()

                def modify():
                    try:
                        store.update_memo(notes[0], "수정된 원문", enqueue_enrich=False)
                        store.delete_memo(notes[1])
                    except Exception as error:
                        errors.append(error)
                    finally:
                        db.close()

                writer = threading.Thread(target=modify)
                writer.start()
                writer.join(4)
                deadline = time.monotonic() + 5
                while notes[1] in windows.forms and time.monotonic() < deadline:
                    Application.DoEvents()
                    time.sleep(.02)
                self.assertEqual(body.Text, "수정된 원문")
                self.assertNotIn(notes[1], windows.forms)
                body.Text = "작은 창에서 편집\r\n#새태그"
                self.assertEqual(store.get_memo(notes[0])["body"], "수정된 원문")
                body.Focus()
                previous = (ctypes.c_ubyte * 256)()
                self.assertTrue(user.GetKeyboardState(previous))
                keys = type(previous).from_buffer_copy(previous)
                keys[0x11] = 0x80
                self.assertTrue(user.SetKeyboardState(keys))
                try:
                    user.PostMessageW(wintypes.HWND(body.Handle.ToInt64()), 0x100, int(Keys.S), 1)
                    Application.DoEvents()
                finally:
                    user.SetKeyboardState(previous)
                wait_saved(editor)
                saved = store.get_memo(notes[0])
                self.assertEqual(saved["body"], "작은 창에서 편집\n#새태그")
                self.assertIn("새태그", saved["facets"]["tag"])
                body.Text = "로컬 편집본"
                with patch.object(store, "update_memo", side_effect=OSError("write failed")), \
                     self.assertLogs("memoinall.memo_windows", level="ERROR"):
                    editor.button.PerformClick()
                    wait_saved(editor)
                self.assertEqual(body.Text, "로컬 편집본")
                self.assertTrue(editor.button.Enabled)
                self.assertFalse(body.ReadOnly)
                self.assertEqual(store.get_memo(notes[0])["body"], saved["body"])
                store.update_memo(notes[0], "메인 창 수정", enqueue_enrich=False)
                editor.button.PerformClick()  # Race before the refresh timer detects the edit.
                wait_saved(editor)
                self.assertTrue(editor.diverged)
                self.assertEqual(body.Text, "로컬 편집본")
                self.assertEqual(store.get_memo(notes[0])["body"], "메인 창 수정")
                editor.button.PerformClick()
                wait_saved(editor)
                self.assertEqual(body.Text, "메인 창 수정")
                self.assertTrue(db.one("SELECT 1 FROM memos WHERE body=?", ("로컬 편집본",)))
                discarded = windows.forms[notes[2]]
                discarded.body.Text = "취소 후 버릴 편집"
                with patch("memoinall.memo_windows._ask", return_value="Cancel"):
                    discarded.form.Close()
                    parent.Close()
                self.assertTrue(discarded.form.Visible)
                with patch("memoinall.memo_windows._ask", return_value="No"):
                    discarded.form.Close()
                self.assertFalse(db.one("SELECT 1 FROM memo_windows WHERE memo_id=?", (notes[2],)))
                self.assertTrue(store.get_memo(notes[2]))
                deleted = windows.forms[notes[3]]
                deleted.body.Text = "삭제된 원본의 편집본"
                store.delete_memo(notes[3])
                windows.version = None
                windows._refresh()
                self.assertTrue(deleted.diverged)
                self.assertEqual(deleted.body.Text, "삭제된 원본의 편집본")
                with patch("memoinall.memo_windows._ask", return_value="Yes"):
                    deleted.form.Close()
                self.assertTrue(db.one("SELECT 1 FROM memos WHERE body=?", ("삭제된 원본의 편집본",)))
                body.Text = "앱을 닫으면서 저장"
                with patch("memoinall.memo_windows._ask", return_value="Yes"):
                    parent.Close()
                self.assertEqual(store.get_memo(notes[0])["body"], "앱을 닫으면서 저장")
                self.assertIsNone(windows.timer)
                self.assertEqual(windows.forms, {})
                parent = Form()
                parent.Show()
                restored = MemoWindows(SimpleNamespace(native=parent))
                restored.restore()
                self.assertEqual(set(restored.forms), set(notes) - {notes[1], notes[2], notes[3]})
                self.assertEqual(restored.forms[notes[0]].form.Bounds, saved_bounds)
                for item in list(restored.forms.values()):
                    item.form.Close()
                self.assertIsNone(restored.timer)
                parent.Close()
            except BaseException as error:
                errors.append(error)
            finally:
                db.close()

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp, \
             patch.object(config, "HOME", Path(temp)), patch.object(config, "DB_PATH", Path(temp) / "memo.db"):
            thread = threading.Thread(target=exercise, daemon=True)
            thread.start()
            thread.join(25)
            self.assertFalse(thread.is_alive(), "native memo window test timed out")
            if errors:
                raise errors[0]


    def test_at_most_ten_and_trash_keeps_the_window(self):
        """11번째는 안내와 함께 거절한다. 이미 띄운 메모는 다시 열 수 있다. 휴지통을 거쳐도 창 설정이 남는다."""
        from memoinall import config, db, memo_windows, store, trash

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp, \
             patch.object(config, "HOME", Path(temp)), patch.object(config, "DB_PATH", Path(temp) / "memo.db"):
            try:
                notes = [store.add_memo(f"스티커 {i}", enqueue_enrich=False)["id"] for i in range(12)]
                self.assertEqual(memo_windows.status(), {"ids": [], "max": 10})
                for memo_id in notes[:10]:
                    self.assertTrue(memo_windows.pin(memo_id))
                self.assertFalse(memo_windows.pin(notes[0]))                     # 이미 띄운 것은 그대로 연다
                with self.assertRaises(memo_windows.LimitReached) as limit:
                    memo_windows.pin(notes[10])
                self.assertIn("10개까지", str(limit.exception))
                with self.assertRaisesRegex(ValueError, "찾을 수 없습니다"):
                    memo_windows.pin(999999)
                self.assertEqual(memo_windows.status()["ids"], notes[:10])

                db.execute("UPDATE memo_windows SET x=11, y=22, width=330, height=220 WHERE memo_id=?", (notes[0],))
                trash.move(notes[0])
                self.assertNotIn(notes[0], memo_windows.status()["ids"])
                self.assertTrue(memo_windows.pin(notes[10]))                     # 자리가 나면 다른 메모를 띄울 수 있다
                trash.restore(notes[0])                                           # 10개가 차 있어 창은 빼고 되살린다
                self.assertNotIn(notes[0], memo_windows.status()["ids"])
                db.execute("DELETE FROM memo_windows WHERE memo_id=?", (notes[10],))
                trash.move(notes[1])
                trash.restore(notes[1])
                self.assertIn(notes[1], memo_windows.status()["ids"])           # 자리가 있으면 창 설정도 돌아온다
            finally:
                db.close()

    def test_startup_restore_waits_opens_one_by_one_and_keeps_focus(self):
        """앱을 켤 때: 화면이 부르면 바로 돌아오고, 잠시 뒤 10개까지 하나씩 연다. 쓰던 창의 포커스는 그대로."""
        from memoinall import config, db, memo_windows, store

        user = ctypes.WinDLL("user32", use_last_error=True)
        user.CreateDesktopW.restype = wintypes.HANDLE
        user.SetThreadDesktop.argtypes = user.CloseDesktop.argtypes = [wintypes.HANDLE]
        desktop = user.CreateDesktopW(f"memo-restore-{os.getpid()}", None, None, 0, 0x1ff, None)
        self.assertTrue(desktop, ctypes.get_last_error())
        self.addCleanup(user.CloseDesktop, desktop)
        errors, seen = [], {}

        def exercise():
            try:
                self.assertTrue(user.SetThreadDesktop(desktop))
                import clr
                clr.AddReference("System.Windows.Forms")
                from System.Windows.Forms import Application, Form

                notes = [store.add_memo(f"다시 띄울 메모 {i}", enqueue_enrich=False)["id"] for i in range(12)]
                # 예전 버전에서 10개 넘게 정해 둔 경우
                for memo_id in notes:
                    db.execute("INSERT INTO memo_windows(memo_id) VALUES(?)", (memo_id,))
                parent = Form()
                parent.Show()
                parent.Activate()
                Application.DoEvents()
                seen["active_before"] = Form.ActiveForm is not None
                windows = memo_windows.MemoWindows(SimpleNamespace(native=parent, evaluate_js=lambda _js: None))
                started = time.monotonic()
                self.assertTrue(windows.restore_memo_windows())
                self.assertFalse(windows.restore_memo_windows())                # 화면을 새로 고쳐도 한 번만
                self.assertEqual(windows.forms, {})                              # 부른 쪽을 붙잡지 않는다
                store.delete_memo(notes[2])                                       # 기다리는 사이 지워진 메모는 건너뛴다
                counts = []
                while len(windows.forms) < 10 and time.monotonic() - started < 8:
                    Application.DoEvents()
                    if not counts or counts[-1][1] != len(windows.forms):
                        counts.append((time.monotonic() - started, len(windows.forms)))
                    time.sleep(.01)
                for _ in range(40):                                               # 10개 뒤로는 더 열지 않는다
                    Application.DoEvents()
                    time.sleep(.01)
                seen["ids"] = sorted(windows.forms)
                seen["first"] = next(t for t, n in counts if n >= 1)
                seen["all"] = next(t for t, n in counts if n >= 10)
                seen["steps"] = [n for _, n in counts]
                active = Form.ActiveForm
                seen["focus_kept"] = active is not None and active.Handle == parent.Handle
                for editor in list(windows.forms.values()):
                    editor.form.Close()
                parent.Close()
            except BaseException as error:
                errors.append(error)
            finally:
                db.close()

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp, \
             patch.object(config, "HOME", Path(temp)), patch.object(config, "DB_PATH", Path(temp) / "memo.db"):
            thread = threading.Thread(target=exercise, daemon=True)
            thread.start()
            thread.join(25)
            self.assertFalse(thread.is_alive(), "restore test timed out")
            if errors:
                raise errors[0]
        print(f"restore: first window {seen['first']:.2f}s, ten windows {seen['all']:.2f}s, steps {seen['steps']}")
        self.assertEqual(len(seen["ids"]), 10)
        self.assertEqual(seen["ids"], sorted(seen["ids"]))
        self.assertGreaterEqual(seen["first"], memo_windows.RESTORE_DELAY_MS / 1000 - 0.1)   # 앱이 자리 잡을 틈을 둔다
        self.assertEqual(seen["steps"][1:3], [1, 2])                                         # 한꺼번에가 아니라 하나씩
        if seen["active_before"]:   # 입력을 받지 않는 시험용 데스크톱에서는 활성 창이 아예 없다
            self.assertTrue(seen["focus_kept"])

    def test_reopened_window_gives_focus_back(self):
        """띄운 뒤 원래 쓰던 창을 다시 활성화한다. 다른 앱을 쓰는 중(활성 창 없음)이면 건드리지 않는다."""
        from unittest.mock import Mock
        from memoinall import memo_windows

        windows = memo_windows.MemoWindows(SimpleNamespace(native=None))
        main = SimpleNamespace(IsDisposed=False, Activate=Mock())
        order = []
        windows._show = lambda row: order.append(("show", row))
        main.Activate.side_effect = lambda: order.append(("activate", "main"))
        with patch.object(memo_windows, "_active_form", return_value=main):
            windows._show_quietly("note")
        self.assertEqual(order, [("show", "note"), ("activate", "main")])
        with patch.object(memo_windows, "_active_form", return_value=None):
            windows._show_quietly("other")
        self.assertEqual(order[-1], ("show", "other"))
        closed = SimpleNamespace(IsDisposed=True, Activate=Mock())
        with patch.object(memo_windows, "_active_form", return_value=closed):
            windows._show_quietly("third")
        closed.Activate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
