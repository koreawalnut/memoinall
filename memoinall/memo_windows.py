"""Always-on-top memo editors on the desktop app's existing WinForms thread."""

from concurrent.futures import Future
import logging
import sqlite3
import threading

from . import db, store

log = logging.getLogger(__name__)
PINNED = """SELECT w.*, m.title, m.body FROM memo_windows w
            JOIN memos m ON m.id=w.memo_id"""


class MemoWindows:
    def __init__(self, window):
        self.window = window
        self.forms = {}
        self.timer = None
        self.version = None
        self.stopping = False

    def restore(self):
        try:
            from System import Action

            def reopen():
                for row in db.query(PINNED + " ORDER BY w.memo_id"):
                    self._show(row)

            self.window.native.Invoke(Action(reopen))
        except Exception:
            log.exception("메모 창 복원 실패")

    def open_memo_window(self, memo_id):
        from System import Action
        from System.Windows.Forms import FormWindowState

        def open_one():
            if self.stopping:
                return
            if memo_id in self.forms:
                form = self.forms[memo_id].form
                if form.WindowState == FormWindowState.Minimized:
                    form.WindowState = FormWindowState.Normal
                form.Activate()
                return
            conn = db.connect()
            with conn:
                conn.execute("INSERT OR IGNORE INTO memo_windows(memo_id) SELECT id FROM memos WHERE id=?", (memo_id,))
                row = conn.execute(PINNED + " WHERE w.memo_id=?", (memo_id,)).fetchone()
                if row is None:
                    raise ValueError("메모를 찾을 수 없습니다.")
                self._show(row)

        self.window.native.Invoke(Action(open_one))

    def _show(self, row):
        from System.Drawing import Color, Font, Rectangle, Size, SystemColors
        from System.Windows.Forms import (BorderStyle, Button, CloseReason, DockStyle, Form, FormStartPosition,
                                          FormWindowState, Label, Padding, Panel, Screen, ScrollBars,
                                          SystemInformation, TextBox, Timer)

        memo_id = row["memo_id"]
        if self.stopping or memo_id in self.forms:
            return
        form = Form()
        form.TopMost = True
        form.MinimumSize = Size(220, 135)
        form.StartPosition = FormStartPosition.Manual
        area = Screen.FromControl(self.window.native).WorkingArea
        offset = len(self.forms) % 8 * 26
        bounds = Rectangle(area.Right - 385 - offset, area.Top + 24 + offset, 360, 230)
        if row["x"] is not None:
            bounds = Rectangle(row["x"], row["y"], row["width"], row["height"])
            area = Screen.FromRectangle(bounds).WorkingArea
        width = min(area.Width, max(form.MinimumSize.Width, bounds.Width))
        height = min(area.Height, max(form.MinimumSize.Height, bounds.Height))
        form.Bounds = Rectangle(max(area.Left, min(bounds.X, area.Right - width)),
                                max(area.Top, min(bounds.Y, area.Bottom - height)), width, height)
        form.BackColor = SystemColors.Window if SystemInformation.HighContrast else Color.FromArgb(255, 249, 210)
        form.Padding = Padding(12)
        body = TextBox()
        body.Multiline = body.AcceptsReturn = True
        body.MaxLength = 0
        body.ScrollBars = ScrollBars.Vertical
        body.BorderStyle = getattr(BorderStyle, "None")
        body.BackColor, body.ForeColor = form.BackColor, SystemColors.WindowText
        body.Font = Font("맑은 고딕", 11)
        body.AccessibleName = "고정 창의 메모 본문"
        body.Dock = DockStyle.Fill
        footer = Panel()
        footer.Dock, footer.Height = DockStyle.Bottom, 32
        button, status = Button(), Label()
        button.AutoSize, button.Dock = True, DockStyle.Right
        status.AutoEllipsis, status.Dock = True, DockStyle.Fill
        status.AccessibleName = "저장 상태"
        footer.Controls.Add(status)
        footer.Controls.Add(button)
        form.Controls.Add(body)
        form.Controls.Add(footer)
        editor = MemoEditor(self.window, form, body, button, status, row)

        def remember_bounds(*_):
            bounds = form.Bounds if form.WindowState == FormWindowState.Normal else form.RestoreBounds
            try:
                db.execute("UPDATE memo_windows SET x=?, y=?, width=?, height=? WHERE memo_id=?",
                           (bounds.X, bounds.Y, bounds.Width, bounds.Height, memo_id))
            except sqlite3.Error:
                log.exception("메모 창 위치 저장 실패")

        def closing(_sender, event):
            if event.Cancel or self.stopping:
                return
            if event.CloseReason == CloseReason.UserClosing:
                if not editor.allow_close():
                    event.Cancel = True
                    return
                try:
                    db.execute("DELETE FROM memo_windows WHERE memo_id=?", (memo_id,))
                except sqlite3.Error:
                    log.exception("메모 창 고정 해제 실패")
                    event.Cancel = True

        def closed(*_):
            self.forms.pop(memo_id, None)
            if not self.forms and self.timer is not None:
                self.timer.Dispose()
                self.timer = None
                self.window.native.FormClosing -= self._confirm_exit
                self.window.native.FormClosed -= self._stop

        form.ResizeEnd += remember_bounds
        form.FormClosing += closing
        form.FormClosed += closed
        form.Show()
        self.forms[memo_id] = editor
        if self.timer is None:
            self.window.native.FormClosing += self._confirm_exit
            self.window.native.FormClosed += self._stop
            self.version = None
            self.timer = Timer()
            self.timer.Interval = 2000
            self.timer.Tick += self._refresh
            self.timer.Start()

    def _refresh(self, *_):
        # One inexpensive SQLite version check for all open notes. Fetch only pinned
        # bodies when another connection committed, including imports and bulk deletes.
        try:
            version = db.one("PRAGMA data_version")[0]
            if self.version == version:
                return
            rows = {r["memo_id"]: r for r in db.query(PINNED)}
            for memo_id, editor in list(self.forms.items()):
                editor.refresh(rows.get(memo_id))
            self.version = None if any(e.pending or e.prompting for e in self.forms.values()) else version
        except sqlite3.Error:
            log.exception("고정한 메모 갱신 실패")

    def _confirm_exit(self, _sender, event):
        if not event.Cancel:
            event.Cancel = not all(editor.allow_close() for editor in list(self.forms.values()))

    def _stop(self, *_):
        self.stopping = True
        for editor in list(self.forms.values()):
            editor.form.Close()


def _ask(form, message):
    from System.Windows.Forms import MessageBox, MessageBoxButtons, MessageBoxIcon
    return str(MessageBox.Show(form, message, form.Text, MessageBoxButtons.YesNoCancel, MessageBoxIcon.Question))


class MemoEditor:
    def __init__(self, window, form, body, button, status, row):
        from System.Windows.Forms import Keys

        self.window, self.form, self.body = window, form, body
        self.button, self.status = button, status
        self.memo_id = row["memo_id"]
        self.pending = None
        self.prompting = self.diverged = False
        self._load(row)

        def changed(*_):
            self.button.Enabled = not self.body.ReadOnly
            if not self.form.Text.startswith("＊ "):
                self.form.Text = "＊ " + self.form.Text
            if not self.diverged:
                self.status.Text = "수정 중"

        def key(_sender, event):
            if event.Control and event.KeyCode == Keys.S:
                event.SuppressKeyPress = True
                if self.diverged and _ask(form, "원본이 바뀌었습니다. 이 창의 내용을 새 메모로 저장할까요?") != "Yes":
                    return
                self.save()

        form.KeyPreview = True
        form.KeyDown += key
        body.TextChanged += changed
        button.Click += lambda *_: self.save()

    def _load(self, row):
        self.base = row["body"]   # Keep the exact stored line endings for the conditional write.
        text = self.base.replace("\r\n", "\n").replace("\n", "\r\n")
        if self.body.Text != text:
            self.body.Text = text
        self.form.Text = row["title"] + " · 메모 창"
        self.button.Text = "저장 (Ctrl+S)"
        self.button.Enabled = self.diverged = False
        self.status.Text = ""

    def _text(self):
        return self.body.Text.replace("\r\n", "\n")

    def _dirty(self):
        return self._text() != self.base.replace("\r\n", "\n")

    def _conflict(self, message):
        self.diverged = True
        self.status.Text = message
        self.button.Text = "새 메모로 저장"
        self.button.Enabled = True

    def refresh(self, row):
        if self.pending or self.prompting:
            return
        if row is not None and row["body"] == self.base:
            if self.diverged:
                self.diverged = False
                self.button.Text = "저장 (Ctrl+S)"
                self.button.Enabled = self._dirty()
                self.status.Text = "수정 중" if self.button.Enabled else ""
            return
        if self._dirty():
            if row is None or row["body"] != self.base:
                self._conflict("원본이 변경됨" if row is not None else "원본이 삭제됨")
        elif row is None:
            self.form.Close()
        else:
            self._load(row)

    def save(self):
        from System import Action

        if self.pending or not self.button.Enabled:
            return
        text, expected, copy = self._text(), self.base, self.diverged
        if not text.strip():
            self.status.Text = "본문을 입력하세요."
            return
        future = self.pending = Future()
        self.body.ReadOnly, self.button.Enabled = True, False
        self.status.Text = "저장 중…"

        def write():
            try:
                if copy:
                    original = db.one("SELECT body, title FROM memos WHERE id=?", (self.memo_id,))
                    saved = store.add_memo(text)
                else:
                    saved = store.update_memo(self.memo_id, text, expected_body=expected)
                    original = saved
                future.set_result((original, copy))
            except Exception as error:
                future.set_exception(error)
            finally:
                db.close()
            try:
                if not self.form.IsDisposed:
                    self.form.BeginInvoke(Action(lambda: self._finish(future)))
                if future.exception() is None:
                    self.window.evaluate_js(
                        f"window.dispatchEvent(new CustomEvent('quick-memo-saved', {{detail:{int(saved['id'])}}}))")
            except Exception:
                log.debug("메모 저장 화면 알림 종료", exc_info=True)

        threading.Thread(target=write, name="memo-window-save", daemon=True).start()

    def _finish(self, future):
        from System import Action

        if self.pending is not future:
            return True
        try:
            original, copy = future.result()
        except (store.MemoEditConflict, KeyError) as error:
            self._conflict("원본이 삭제됨" if isinstance(error, KeyError) else "원본이 변경됨")
            return False
        except Exception:
            log.exception("메모 창 저장 실패")
            self.status.Text = "저장 실패 · 다시 시도하세요."
            self.button.Enabled = True
            return False
        finally:
            self.pending = None
            self.body.ReadOnly = False
        if original is None:  # The deleted original has no window to restore.
            self.base = self._text()
            self.form.BeginInvoke(Action(self.form.Close))
        else:
            self._load(original)
        self.status.Text = "내 메모에 사본을 저장했습니다." if copy else "저장됨"
        return True

    def allow_close(self):
        if self.pending and not self._finish(self.pending):
            return False
        if not self._dirty():
            return True
        message = ("원본이 바뀌었습니다. 이 창의 내용을 새 메모로 저장하고 닫을까요?"
                   if self.diverged else "수정한 내용을 저장하고 닫을까요?")
        self.prompting = True
        try:
            choice = _ask(self.form, message)
        finally:
            self.prompting = False
        if choice == "Yes":
            self.save()
            return self.pending is not None and self._finish(self.pending)
        return choice == "No"
