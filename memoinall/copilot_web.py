"""Open the work Copilot site in a dedicated Windows WebView2 and fill a draft."""

import json
import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import config

CHAT_URL = "https://m365.cloud.microsoft/chat"
CHAT_ORIGINS = ("https://m365.cloud.microsoft", "https://copilot.cloud.microsoft")
log = logging.getLogger(__name__)


def is_work_chat(url):
    address = urlsplit(url)
    return (f"{address.scheme}://{address.netloc}" in CHAT_ORIGINS
            and address.path in ("", "/", "/chat", "/chat/"))


def draft_script(prompt):
    source = (Path(__file__).with_name("static") / "copilot-fill.js").read_text(encoding="utf-8")
    return f"{source.strip()}({json.dumps(prompt)}, {json.dumps(CHAT_ORIGINS)})"


def install(window):
    def open_copilot_web(prompt):
        from System import Action

        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("요청문이 비어 있습니다.")
        window.native.Invoke(Action(lambda: _open(window.native, prompt)))
        return True

    window.expose(open_copilot_web)


def _open(owner, prompt):
    from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2
    from System import Action
    from System.Drawing import Size
    from System.Windows.Forms import Button, Clipboard, DockStyle, Form, Label, Panel, Timer

    form, web, bar = Form(), WebView2(), Panel()
    form.Text, form.Size = "업무용 Copilot 웹", Size(1040, 780)
    bar.Dock, bar.Height = DockStyle.Top, 40
    web.Dock = DockStyle.Fill
    status, copy = Label(), Button()
    status.Dock, status.Text = DockStyle.Fill, "회사 계정으로 로그인하면 빈 채팅 입력란에 요청문이 입력됩니다."
    copy.Dock, copy.Text, copy.Width = DockStyle.Right, "요청문 복사", 110
    bar.Controls.Add(status)
    bar.Controls.Add(copy)
    form.Controls.Add(web)
    form.Controls.Add(bar)
    props = CoreWebView2CreationProperties()
    props.UserDataFolder = str(config.HOME / "copilot-web-profile")
    web.CreationProperties = props
    script = draft_script(prompt)
    timer, pending, deadline = Timer(), None, None
    timer.Interval = 600

    def copy_prompt(*_):
        try:
            Clipboard.SetText(prompt)
            status.Text = "요청문을 복사했습니다. 채팅 입력란에 붙여넣으세요."
            timer.Stop()
        except Exception:
            status.Text = "클립보드를 사용할 수 없습니다. memoinall의 요청문 미리 보기에서 복사하세요."

    def tick(*_):
        nonlocal pending, deadline
        try:
            if pending is not None:
                if not pending.IsCompleted:
                    return
                result = json.loads(str(pending.Result))
                pending = None
                if result in ("filled", "occupied", "manual"):
                    timer.Stop()
                    status.Text = {
                        "filled": "요청문이 입력됐습니다. 회사 계정과 내용을 확인한 뒤 전송하세요.",
                        "occupied": "작성 중인 메시지가 있습니다. 필요한 위치에 요청문을 복사해 붙여넣으세요.",
                        "manual": "입력된 내용을 확인하세요. 필요하면 요청문 복사를 사용하세요.",
                    }[result]
                    return
            if not is_work_chat(str(web.CoreWebView2.Source)):
                deadline = None
                return
            if deadline is None:
                deadline = time.monotonic() + 60
            if time.monotonic() >= deadline:
                timer.Stop()
                status.Text = "채팅 입력란을 찾지 못했습니다. 요청문 복사를 사용하세요."
                return
            pending = web.CoreWebView2.ExecuteScriptAsync(script)
        except Exception:
            timer.Stop()
            status.Text = "자동 입력을 확인할 수 없습니다. 요청문 복사를 사용하세요."

    def ready(_sender, event):
        if not event.IsSuccess:
            log.warning("Copilot WebView2 initialization failed: %s", event.InitializationException)
            status.Text = "웹 창을 시작할 수 없습니다. memoinall에서 브라우저로 열어주세요."
            return
        core = web.CoreWebView2
        # Remote pages, including the sign-in screen, have no memoinall bridge.
        core.Settings.AreHostObjectsAllowed = False
        core.Settings.IsWebMessageEnabled = False

        def show_site(*_):
            form.Text = f"업무용 Copilot 웹 · {urlsplit(str(core.Source)).hostname or ''}"
        core.SourceChanged += show_site
        core.Navigate(CHAT_URL)
        timer.Start()

    copy.Click += copy_prompt
    timer.Tick += tick
    form.FormClosed += lambda *_: timer.Dispose()
    web.CoreWebView2InitializationCompleted += ready
    form.Show(owner)
    # Start after WinForms finishes creating the window and returns to its UI message loop.
    web.BeginInvoke(Action(lambda: web.EnsureCoreWebView2Async(None) if not web.IsDisposed else None))
