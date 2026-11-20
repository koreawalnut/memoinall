"""Exercise the Windows hotkey and WinForms editor on a private, non-interactive desktop."""

import ctypes
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(sys.platform == "win32", "Windows desktop feature")
class QuickCaptureTests(unittest.TestCase):
    def test_hotkey_editor_and_separate_draft(self):
        from ctypes import wintypes
        from memoinall import config, db, store
        from memoinall.quick_capture import QuickCapture, WM_HOTKEY, MOD_CONTROL, MOD_ALT

        user = ctypes.WinDLL("user32", use_last_error=True)
        user.CreateDesktopW.restype = wintypes.HANDLE
        user.SetThreadDesktop.argtypes = user.CloseDesktop.argtypes = [wintypes.HANDLE]
        desktop = user.CreateDesktopW(f"memoinall-test-{os.getpid()}", None, None, 0, 0x1ff, None)
        self.assertTrue(desktop, ctypes.get_last_error())
        self.addCleanup(user.CloseDesktop, desktop)
        failures = []

        def exercise():
            try:
                self.assertTrue(user.SetThreadDesktop(desktop))
                import clr
                clr.AddReference("System.Windows.Forms")
                from System.Windows.Forms import Application, Form, Keys

                def pump_until(check):
                    end = time.monotonic() + 4
                    while not check() and time.monotonic() < end:
                        Application.DoEvents()
                        time.sleep(0.01)
                    self.assertTrue(check())

                def key(form, code, control=False):
                    original = (ctypes.c_ubyte * 256)()
                    self.assertTrue(user.GetKeyboardState(original))
                    keys = type(original).from_buffer_copy(original)
                    keys[0x11] = 0x80 if control else 0
                    self.assertTrue(user.SetKeyboardState(keys))
                    try:
                        editor = form.Controls[0]
                        self.assertTrue(user.PostMessageW(wintypes.HWND(editor.Handle.ToInt64()), 0x100, int(code), 1))
                        Application.DoEvents()
                    finally:
                        user.SetKeyboardState(original)

                parent = Form()
                parent.Show()
                window = Mock(native=parent)
                capture = QuickCapture(window)
                # Windows supplies no foreground HWND on an inactive desktop. Model that
                # boundary while exercising real forms, dispatch, registration and storage.
                foreground = [parent.Handle.ToInt64()]
                capture.user.GetForegroundWindow = lambda: foreground[0]
                capture.user.SetForegroundWindow = lambda handle: foreground.__setitem__(0, handle)
                listen = capture._listen

                def listen_here():
                    if not user.SetThreadDesktop(desktop):
                        return
                    listen()

                capture._listen = listen_here
                # Occupy only this private desktop's primary combination.
                self.assertTrue(user.RegisterHotKey(None, 22, MOD_CONTROL | MOD_ALT, ord("M")))
                try:
                    capture.start()
                    self.assertTrue(capture.ready.wait(3))
                    self.assertEqual(capture.quick_capture_status()["shortcut"], "Ctrl+Alt+Shift+M")
                    self.assertTrue(user.PostThreadMessageW(capture.thread_id, WM_HOTKEY, 1, 0))
                    pump_until(lambda: capture.form is not None)
                    first = capture.form
                    self.assertEqual(foreground[0], first.Handle.ToInt64())
                    editor = first.Controls[0]
                    editor.Text = "중간에 떠오른 견적 메모"
                    capture.open_quick_capture()
                    Application.DoEvents()
                    self.assertEqual(first, capture.form)
                    key(first, Keys.Escape)
                    self.assertIsNone(capture.form)
                    self.assertEqual(foreground[0], parent.Handle.ToInt64())
                    self.assertEqual(db.get_meta("quick_capture_draft"), "중간에 떠오른 견적 메모")
                    capture.open_quick_capture()
                    pump_until(lambda: capture.form is not None)
                    self.assertEqual(capture.form.Controls[0].Text, "중간에 떠오른 견적 메모")
                    with self.assertLogs("memoinall.quick_capture", level="ERROR"), \
                         patch.object(store, "add_memo", side_effect=ValueError("test")):
                        key(capture.form, Keys.Enter, control=True)
                        pump_until(lambda: not capture.form.Controls[0].ReadOnly)
                    self.assertEqual(capture.form.Controls[0].Text, "중간에 떠오른 견적 메모")
                    gate = threading.Event()
                    add = store.add_memo

                    def delayed_save(*args, **kwargs):
                        self.assertTrue(gate.wait(3))
                        return add(*args, **kwargs)

                    with patch.object(store, "add_memo", side_effect=delayed_save) as write:
                        key(capture.form, Keys.Enter, control=True)
                        key(capture.form, Keys.Enter, control=True)
                        key(capture.form, Keys.Escape)
                        self.assertIsNotNone(capture.form)
                        gate.set()
                        pump_until(lambda: capture.form is None)
                        write.assert_called_once()
                    self.assertEqual(foreground[0], parent.Handle.ToInt64())
                    self.assertEqual([m["body"] for m in store.list_memos()], ["중간에 떠오른 견적 메모"])
                    self.assertIsNone(db.get_meta("quick_capture_draft"))
                    self.assertEqual(db.get_meta("capture_draft"), "본창 초안")
                finally:
                    capture.stop()
                    pump_until(lambda: capture.thread_id is None)
                    user.UnregisterHotKey(None, 22)
                    parent.Close()
                    db.close()
            except BaseException as error:
                failures.append(error)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp, \
             patch.object(config, "HOME", Path(temp)), patch.object(config, "DB_PATH", Path(temp) / "memo.db"):
            db.set_meta("capture_draft", "본창 초안")
            db.close()
            thread = threading.Thread(target=exercise, daemon=True)
            thread.start()
            thread.join(15)
            self.assertFalse(thread.is_alive(), "Windows UI test timed out")
            if failures:
                raise failures[0]


if __name__ == "__main__":
    unittest.main()
