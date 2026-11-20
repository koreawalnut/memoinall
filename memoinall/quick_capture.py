"""Windows global hotkey and a small modeless editor on the existing WinForms UI thread."""

import ctypes
from ctypes import wintypes
import logging
import threading

log = logging.getLogger(__name__)
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 1, 2, 4, 0x4000
DRAFT_KEY = "quick_capture_draft"


class QuickCapture:
    def __init__(self, window):
        self.window = window
        self.shortcut = ""
        self.form = None
        self.previous = None
        self.thread_id = None
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.SetForegroundWindow.argtypes = [wintypes.HWND]

    def install(self):
        self.window.expose(self.quick_capture_status, self.open_quick_capture)
        self.window.events.shown += self.start
        self.window.events.closed += self.stop

    def start(self):
        threading.Thread(target=self._listen, daemon=True, name="quick-memo-hotkey").start()

    def stop(self):
        self.stopped.set()
        if self.thread_id is not None:
            self.user.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)

    def _listen(self):
        message = wintypes.MSG()
        self.user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)  # create message queue
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        try:
            for extra, label in [(0, "Ctrl+Alt+M"), (MOD_SHIFT, "Ctrl+Alt+Shift+M")]:
                if self.user.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT | extra, ord("M")):
                    self.shortcut = label
                    break
            self.ready.set()
            while self.shortcut and not self.stopped.is_set():
                if self.user.GetMessageW(ctypes.byref(message), None, 0, 0) <= 0:
                    break
                if message.message == WM_HOTKEY and message.wParam == 1:
                    try:
                        self.open_quick_capture()
                    except Exception:
                        if not self.stopped.is_set():
                            log.exception("빠른 메모창 열기 실패")
        finally:
            if self.shortcut:
                self.user.UnregisterHotKey(None, 1)
            self.thread_id = None
            self.ready.set()

    def quick_capture_status(self):
        self.ready.wait(1)
        return {"shortcut": self.shortcut}

    def open_quick_capture(self):
        from System import Action

        previous = self.user.GetForegroundWindow()
        if not self.stopped.is_set():
            self.window.native.Invoke(Action(lambda: self._show(previous)))

    def _show(self, previous):
        from System import Action
        from System.Drawing import Font, Size
        from System.Windows.Forms import (AutoScaleMode, Button, DockStyle, FlowLayoutPanel,
                                          Form, FormBorderStyle, FormStartPosition, Keys,
                                          Label, Padding, ScrollBars, TextBox)
        from . import db, store

        if self.stopped.is_set():
            return
        if not self.form or previous != self.form.Handle.ToInt64():
            self.previous = previous
        if self.form is not None:
            self.user.SetForegroundWindow(self.form.Handle.ToInt64())
            self.form.Activate()
            return

        form = Form()
        form.Text = "빠른 메모 · memoinall"
        form.Font = Font("맑은 고딕", 10)
        form.AutoScaleMode = AutoScaleMode.Dpi
        form.ClientSize = Size(480, 200)
        form.Padding = Padding(12)
        form.FormBorderStyle = FormBorderStyle.FixedDialog
        form.MaximizeBox = form.MinimizeBox = form.ShowInTaskbar = False
        form.StartPosition = FormStartPosition.CenterScreen
        form.KeyPreview = True
        editor = TextBox()
        editor.AccessibleName = "빠른 메모 본문"
        editor.Multiline = editor.AcceptsReturn = True
        editor.MaxLength = 0
        editor.ScrollBars = ScrollBars.Vertical
        editor.Dock = DockStyle.Fill
        editor.Text = db.get_meta(DRAFT_KEY, "") or ""
        footer = FlowLayoutPanel()
        footer.Dock = DockStyle.Bottom
        footer.Height = 40
        button = Button()
        button.Text = "메모 저장"
        button.AutoSize = True
        hint = Label()
        hint.AutoSize = True
        hint.Padding = Padding(0, 6, 0, 0)
        hint.Text = "Ctrl+Enter 저장 · Esc 초안 보관"
        footer.Controls.Add(button)
        footer.Controls.Add(hint)
        form.Controls.Add(editor)
        form.Controls.Add(footer)
        saved = False
        saving = False

        def save(*_):
            nonlocal saving
            if saving or saved or not editor.Text.strip():
                return
            saving = True
            body = editor.Text
            editor.ReadOnly = True
            button.Enabled = False
            hint.Text = "저장 중…"
            threading.Thread(target=persist, args=(body,), name="quick-memo-save", daemon=True).start()

        def persist(body):
            try:
                store.add_memo(body, clear_draft=DRAFT_KEY)
                ok = True
            except Exception:
                log.exception("빠른 메모 저장 실패")
                ok = False
            finally:
                db.close()
            form.BeginInvoke(Action(lambda: finish_save(ok)))

        def finish_save(ok):
            nonlocal saved, saving
            saving = False
            if not ok:
                editor.ReadOnly = False
                update_button()
                hint.Text = "저장하지 못했습니다. 다시 시도하세요."
                editor.Focus()
                return
            saved = True
            form.Close()
            threading.Thread(target=self._refresh_feed, daemon=True).start()

        def on_key(_sender, event):
            if event.KeyCode == Keys.Escape:
                event.SuppressKeyPress = True
                form.Close()
            elif event.Control and event.KeyCode == Keys.Enter:
                event.SuppressKeyPress = True
                save()

        def closing(_sender, event):
            if saving:
                event.Cancel = True
                return
            if not saved:
                try:
                    db.set_meta(DRAFT_KEY, editor.Text)
                except Exception:
                    log.exception("빠른 메모 초안 보관 실패")
                    event.Cancel = True
                    hint.Text = "초안을 보관하지 못했습니다. 다시 시도하세요."
                    return
            if self.previous and self.user.GetForegroundWindow() == form.Handle.ToInt64():
                self.user.SetForegroundWindow(self.previous)

        def closed(*_):
            self.form = None
            self.window.native.FormClosing -= close_with_main

        def close_with_main(_sender, event):
            if not event.Cancel:
                form.Close()
                event.Cancel = self.form is not None

        def update_button(*_):
            button.Enabled = bool(editor.Text.strip())

        update_button()
        editor.TextChanged += update_button
        button.Click += save
        form.KeyDown += on_key
        form.FormClosing += closing
        form.FormClosed += closed
        self.window.native.FormClosing += close_with_main
        self.form = form
        form.Show()
        self.user.SetForegroundWindow(form.Handle.ToInt64())
        form.Activate()
        editor.Focus()
        editor.Select(editor.TextLength, 0)

    def _refresh_feed(self):
        if not self.stopped.is_set():
            try:
                self.window.evaluate_js("window.dispatchEvent(new Event('quick-memo-saved'))")
            except Exception:
                log.debug("메인 창 새로고침 대기", exc_info=True)
