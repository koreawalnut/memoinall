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


if __name__ == "__main__":
    unittest.main()
