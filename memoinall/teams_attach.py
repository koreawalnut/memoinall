"""Teams 새 채팅에 글과 파일을 넣는다.

Teams 새 채팅 링크(msteams://…/chat)는 글만 미리 채울 수 있고 파일은 붙일 수 없다. 게다가 받는 사람을 고르지 않은
새 채팅 초안이 남아 있으면 링크의 글도 무시하고 예전 초안을 그대로 보여준다. 클립보드의 파일을 Ctrl+V 해도
새 Teams 는 첨부로 받지 않고, Windows 공유 창은 UAC 를 끈 PC 에서 빈 화면만 띄운다(모두 실제 PC 에서 확인).

그래서 링크로 새 채팅을 연 다음 UI Automation 으로 그 화면을 직접 다룬다.
  1. 메시지 입력란에 우리 글이 없으면(예전 초안) 지우고 우리 글을 붙여 넣는다(클립보드 → Ctrl+V).
  2. 예전 초안에 남은 첨부는 떼어 낸다 — 엉뚱한 파일이 같이 나가지 않게.
  3. '파일 첨부 → 이 디바이스에서 업로드'를 눌러 뜬 파일 대화상자에 경로를 넣고 '열기'.
  4. 첨부가 화면에 보이면 받는 사람 칸으로 포커스를 돌린다.

첨부 버튼이 접혀 있으면 작성란에서 Teams 첨부 단축키(Alt+Shift+O)를 쓴다.
키를 보내기 전에는 앞 창과 포커스가 같은 새 채팅 입력란인지 확인한다.
받는 사람 칸이 있는 '새 채팅' 화면에서만 움직인다(이미 있는 대화방에는 받는 사람 칸이 없다).
Enter 는 보내지 않는다 — 보내기는 언제나 사람이 누른다. 어느 단계든 안 되면 AttachFailed 로 멈추고,
부르는 쪽이 '파일 첨부 버튼으로 직접 올리기' 안내로 대신한다.
"""

from __future__ import annotations

import ctypes
import logging
import re
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

TEAMS_EXES = ("ms-teams.exe", "teams.exe")
COMPOSE_PREFIX = "new-message-"          # 새 Teams 메시지 입력란의 AutomationId 앞부분
PEOPLE_PICKER = "people-picker-input"    # 새 채팅의 받는 사람 칸
ATTACH_BUTTONS = ("파일 첨부", "첨부 파일", "Attach file", "Attach files", "Attach")
UPLOAD_ITEMS = ("이 디바이스에서 업로드", "이 장치에서 업로드", "컴퓨터에서 업로드", "내 컴퓨터에서 업로드",
                "이 컴퓨터에서 업로드", "이 디바이스에서 업로드하기", "Upload from this device",
                "Upload from computer", "Upload from my computer", "Upload from this computer")
REMOVE_BUTTONS = ("첨부 파일 제거", "Remove attachment")
FILE_NAME_BOX, OPEN_BUTTON = "1148", "1"  # Windows 파일 열기 대화상자의 파일 이름 칸·열기 버튼(언어와 무관)
_ASSEMBLY = ", Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35"

VK_CONTROL, VK_DELETE, VK_A, VK_V = 0x11, 0x2E, 0x41, 0x56
VK_ALT, VK_SHIFT, VK_O = 0x12, 0x10, 0x4F
KEYEVENTF_KEYUP = 0x0002
WM_CLOSE = 0x0010
BM_CLICK = 0x00F5


class AttachFailed(RuntimeError):
    """자동으로 넣지 못했다. 사용자가 직접 올리도록 안내한다."""


def _uia():
    try:
        import clr
        for name in ("UIAutomationTypes", "UIAutomationClient"):
            clr.AddReference(name + _ASSEMBLY)
        from System import IntPtr
        from System.Windows.Automation import (AutomationElement, ControlType, ExpandCollapsePattern,
                                               InvokePattern, PropertyCondition, TreeScope, ValuePattern)
    except Exception as exc:  # pythonnet·.NET Framework 가 없으면
        raise AttachFailed("UI Automation 을 쓸 수 없습니다.") from exc
    return dict(IntPtr=IntPtr, AE=AutomationElement, ControlType=ControlType, Expand=ExpandCollapsePattern,
                Invoke=InvokePattern, Property=PropertyCondition, Scope=TreeScope, Value=ValuePattern)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def message_marker(message: str) -> str:
    """입력란에 우리 글이 들어 있는지 알아볼 글. 앞 120자(줄바꿈·공백 차이는 무시).
    첫 줄만 보면 '공유 일정 1건'처럼 예전 초안과 첫 줄이 같은 경우를 못 가린다."""
    return _squash(message)[:120]


def _process_name(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(handle)


def _user32():
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetDlgItem.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowEnabled.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    return user32


def _is_teams(hwnd) -> bool:
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return _process_name(pid.value) in TEAMS_EXES


def _teams_foreground():
    """앞에 있는 창이 Teams 면 그 창 핸들, 아니면 None."""
    hwnd = _user32().GetForegroundWindow()
    return hwnd if _is_teams(hwnd) else None


def _teams_file_dialogs(owner=None) -> list:
    """Teams 가 띄운 파일 대화상자(#32770). 대화상자는 WebView2 프로세스 것이고 주인 창이 Teams 다."""
    user32 = _user32()
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(16)
            user32.GetClassNameW(hwnd, cls, 16)
            if cls.value == "#32770":
                parent = user32.GetWindow(hwnd, 4)  # GW_OWNER
                if owner is None:
                    if _is_teams(hwnd) or _is_teams(parent):
                        found.append(hwnd)
                else:
                    while parent:
                        if parent == owner:
                            found.append(hwnd)
                            break
                        parent = user32.GetWindow(parent, 4)
        return True

    user32.EnumWindows(visit, 0)
    return found


def _keys(*strokes):
    """(가상 키, 뗌 여부)를 차례대로 보낸다."""
    user32 = ctypes.windll.user32
    for vk, up in strokes:
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)
        time.sleep(0.02)


def _ctrl(vk):
    _keys((VK_CONTROL, False), (vk, False), (vk, True), (VK_CONTROL, True))


def _wait(ready, seconds: float, step: float = 0.2):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = ready()
        if value:
            return value
        time.sleep(step)
    return ready()


class _NewChat:
    """링크로 연 Teams 새 채팅 화면 하나."""

    def __init__(self, uia, marker):
        self.u = uia
        self.marker = marker
        self.window = self.compose = self.compose_id = None
        self.hwnd = None

    # ------------------------------------------------------------------ 찾기
    def _all(self, root, prop, value):
        u = self.u
        return list(root.FindAll(u["Scope"].Descendants, u["Property"](prop, value)))

    def _first(self, prop, value):
        u = self.u
        return self.window.FindFirst(u["Scope"].Descendants, u["Property"](prop, value))

    def locate(self) -> bool:
        """앞 창이 Teams 새 채팅이면 메시지 입력란을 잡는다. 우리 글이 든 입력란을 먼저 고른다."""
        u = self.u
        hwnd = _teams_foreground()
        if not hwnd or (self.hwnd and hwnd != self.hwnd):
            return False
        try:
            self.window = u["AE"].FromHandle(u["IntPtr"](hwnd))
            if self._first(u["AE"].AutomationIdProperty, PEOPLE_PICKER) is None:
                return False
            edits = [e for e in self._all(self.window, u["AE"].ControlTypeProperty, u["ControlType"].Edit)
                     if str(e.Current.AutomationId).startswith(COMPOSE_PREFIX)]
        except Exception:
            return False
        ours = [e for e in edits if self.marker in _squash(self.value(e))]
        chosen = ours[:1] or (edits if len(edits) == 1 else [])
        if not chosen:
            return False
        self.compose, self.compose_id = chosen[0], str(chosen[0].Current.AutomationId)
        self.hwnd = hwnd
        return True

    def value(self, element=None) -> str:
        try:
            ok, pattern = (element or self.compose).TryGetCurrentPattern(self.u["Value"].Pattern, None)
            return str(pattern.Current.Value) if ok else ""
        except Exception:
            return ""

    def has_message(self) -> bool:
        return self.marker in _squash(self.value())

    def shows(self, name: str) -> bool:
        try:
            return self._first(self.u["AE"].NameProperty, name) is not None
        except Exception:
            return False

    def _buttons(self, names):
        u = self.u
        return [b for b in self._all(self.window, u["AE"].ControlTypeProperty, u["ControlType"].Button)
                if str(b.Current.Name) in names]

    # ------------------------------------------------------------------ 글
    def _compose_focused(self):
        try:
            focused = str(self.u["AE"].FocusedElement.Current.AutomationId)
        except Exception:
            return False
        return bool(self.hwnd and _teams_foreground() == self.hwnd and focused == self.compose_id)

    def check(self):
        """키를 보내기 직전 확인: 앞 창이 Teams 이고 포커스가 바로 그 입력란이다."""
        if not self._compose_focused():
            raise AttachFailed("Teams 메시지 입력란에 포커스가 없습니다.")

    def replace_message(self, message, put_text):
        """예전 초안 글을 우리 글로. 클립보드에 먼저 담는다 — 담지 못하면 초안을 지우기 전에 멈춘다."""
        try:
            put_text(message)
        except Exception as exc:
            raise AttachFailed("클립보드에 글을 담지 못했습니다.") from exc
        try:
            self.compose.SetFocus()
        except Exception as exc:
            raise AttachFailed("메시지 입력란으로 이동하지 못했습니다.") from exc
        time.sleep(0.25)
        self.check(); _ctrl(VK_A); _keys((VK_DELETE, False), (VK_DELETE, True))
        time.sleep(0.1)
        self.check(); _ctrl(VK_V)
        _wait(self.has_message, 3)

    # ------------------------------------------------------------------ 파일
    def remove_old_attachments(self):
        """예전 초안에 남은 첨부를 모두 뗀다(이번 파일은 그다음에 새로 붙인다)."""
        for _ in range(10):
            buttons = self._buttons(REMOVE_BUTTONS)
            if not buttons:
                return
            ok, pattern = buttons[0].TryGetCurrentPattern(self.u["Invoke"].Pattern, None)
            if not ok:
                raise AttachFailed("예전 첨부를 떼지 못했습니다.")
            pattern.Invoke()
            time.sleep(0.4)
        raise AttachFailed("예전 첨부를 떼지 못했습니다.")

    def _command(self, names, roles):
        u = self.u
        labels = {name.casefold() for name in names}
        for role in roles:
            for element in self._all(self.window, u["AE"].ControlTypeProperty, getattr(u["ControlType"], role)):
                try:
                    current = element.Current
                    # Teams can append a localized shortcut to the accessible name.
                    label = re.sub(r"\s*[(（].*?[)）]$", "", _squash(str(current.Name))).strip().casefold()
                    if label in labels and current.IsEnabled and not current.IsOffscreen:
                        return element
                except Exception:
                    continue  # The toolbar was redrawn during enumeration.

    def _check_chat(self, *, menu_open=False):
        if not self.hwnd or _teams_foreground() != self.hwnd:
            raise AttachFailed("Teams 새 채팅이 앞에 있지 않아 첨부를 중단했습니다.")
        # The open Teams menu hides the underlying recipient field from UI Automation.
        if not menu_open and self._first(self.u["AE"].AutomationIdProperty, PEOPLE_PICKER) is None:
            raise AttachFailed("Teams 새 채팅의 받는 사람 입력란을 찾지 못했습니다.")

    def _attachment_shortcut(self):
        self._check_chat()
        self.compose.SetFocus()
        _wait(self._compose_focused, 2, 0.1)
        self.check()
        _keys((VK_ALT, False), (VK_SHIFT, False), (VK_O, False),
              (VK_O, True), (VK_SHIFT, True), (VK_ALT, True))

    def open_upload_dialog(self):
        u = self.u
        self._check_chat()
        button = _wait(lambda: self._command(ATTACH_BUTTONS, ("Button",)), 2)
        if button is not None:
            self._check_chat()
            ok, expand = button.TryGetCurrentPattern(u["Expand"].Pattern, None)
            if ok:
                expand.Expand()
            else:
                ok, invoke = button.TryGetCurrentPattern(u["Invoke"].Pattern, None)
                if ok:
                    invoke.Invoke()

        def upload_item():
            return self._command(UPLOAD_ITEMS, ("MenuItem", "Button"))

        item = _wait(upload_item, 2) if button is not None else None
        if item is None:
            self._attachment_shortcut()
            item = _wait(upload_item, 5)
        if item is None:
            raise AttachFailed("Teams에서 ‘이 디바이스에서 업로드’ 메뉴가 보이지 않아 파일을 선택하지 못했습니다.")
        self._check_chat(menu_open=True)
        ok, invoke = item.TryGetCurrentPattern(u["Invoke"].Pattern, None)
        if not ok:
            raise AttachFailed("'이 디바이스에서 업로드'를 누르지 못했습니다.")
        existing = set(_teams_file_dialogs(self.hwnd))
        invoke.Invoke()
        dialogs = _wait(lambda: set(_teams_file_dialogs(self.hwnd)) - existing, 10)
        if len(dialogs) != 1:
            raise AttachFailed("파일 선택 창이 뜨지 않았습니다.")
        return dialogs.pop()

    def choose_file(self, dialog, path: str):
        u = self.u
        user32 = _user32()
        if dialog not in _teams_file_dialogs(self.hwnd):
            raise AttachFailed("Teams 파일 선택 창이 닫혀 첨부를 중단했습니다.")
        try:
            window = u["AE"].FromHandle(u["IntPtr"](dialog))
            box = window.FindFirst(u["Scope"].Descendants, u["Property"](u["AE"].AutomationIdProperty, FILE_NAME_BOX))
            edit = box.FindFirst(u["Scope"].Descendants,
                                 u["Property"](u["AE"].ControlTypeProperty, u["ControlType"].Edit)) if box else None
            ok, value = (edit or box).TryGetCurrentPattern(u["Value"].Pattern, None)
            if not ok:
                raise AttachFailed("파일 이름 입력란을 사용할 수 없습니다.")
            value.SetValue(path)
            # The common dialog's Open split button can lack UIA InvokePattern.
            # IDOK is the native button of this specific Teams-owned dialog.
            button = user32.GetDlgItem(dialog, int(OPEN_BUTTON))
            cls = ctypes.create_unicode_buffer(32)
            if button:
                user32.GetClassNameW(button, cls, len(cls))
            if (str(value.Current.Value) != path or cls.value != "Button" or
                    not _wait(lambda: user32.IsWindowEnabled(button), 2) or
                    dialog not in _teams_file_dialogs(self.hwnd) or not user32.PostMessageW(button, BM_CLICK, 0, 0)):
                raise AttachFailed("파일 선택 창의 ‘열기’ 버튼을 누르지 못했습니다.")
        except AttachFailed:
            raise
        except Exception as exc:
            user32.PostMessageW(dialog, WM_CLOSE, 0, 0)
            raise AttachFailed("파일 선택 창에 파일을 넣지 못했습니다.") from exc
        if not _wait(lambda: dialog not in _teams_file_dialogs(self.hwnd), 8):
            user32.PostMessageW(dialog, WM_CLOSE, 0, 0)
            raise AttachFailed("파일 선택 창이 닫히지 않았습니다.")

    def focus_recipients(self):
        try:
            picker = self._first(self.u["AE"].AutomationIdProperty, PEOPLE_PICKER)
            if picker is not None:
                picker.SetFocus()
        except Exception:
            log.debug("받는 사람 칸으로 포커스를 옮기지 못함")


def fill_new_chat(message: str, put_text, *, file_path: str | None = None, timeout: float = 15.0) -> dict:
    """링크로 막 연 Teams 새 채팅에 글(과 파일)을 넣는다. 보내지는 않는다.

    put_text(message) 는 클립보드에 글을 담는다(UI 스레드에서). 예전 초안이 남아 링크의 글이 안 들어갔을 때만 쓴다.
    돌려주는 값: {"message": 입력란에 우리 글이 있는지, "attached": 첨부가 화면에 보이는지(파일 없으면 None)}
    새 채팅을 찾지 못했거나 안전하게 계속할 수 없으면 AttachFailed.
    """
    marker = message_marker(message)
    if not marker:
        raise AttachFailed("넣을 글이 없습니다.")
    chat = _NewChat(_uia(), marker)
    if not _wait(chat.locate, timeout, 0.4):
        raise AttachFailed("Teams 새 채팅의 메시지 입력란을 찾지 못했습니다.")
    # 링크의 글이 막 채워지는 중일 수 있어 잠깐 기다린다. 끝내 없으면 예전 초안이 남은 것이다.
    fresh = _wait(lambda: chat.locate() and chat.has_message(), 1.5)
    if not fresh:
        chat.replace_message(message, put_text)
    attached = None
    if file_path is not None:
        name = file_path.replace("/", "\\").rsplit("\\", 1)[-1]
        chat.remove_old_attachments()
        chat.choose_file(chat.open_upload_dialog(), file_path)
        attached = _wait(lambda: chat.shows(name), 8, 0.4)
    result = {"message": chat.has_message(), "attached": attached}
    chat.focus_recipients()
    return result
