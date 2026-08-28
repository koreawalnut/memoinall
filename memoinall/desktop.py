"""윈도우 데스크톱 앱 진입점.

브라우저 대신 네이티브 창(Windows 는 Edge WebView2)에 같은 UI 를 띄운다.
서버는 프로세스 안에서 임의의 빈 포트로 돌고 밖으로 열리지 않는다.

여기서 신경 쓴 것:
  - **포트 고정 금지.** 8787 이 이미 쓰이면 앱이 안 뜨는 대신 빈 포트를 잡는다.
  - **중복 실행 방지.** 같은 DB 를 두 프로세스가 잡으면 곤란하므로,
    이미 떠 있으면 그 창을 띄우고 조용히 종료한다.
  - 첫 실행 때 임베딩 모델(약 450MB)을 받는데, 그 동안에도 앱은 쓸 수 있다.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

TITLE = "memoinall"
LOCK_PORT_FILE = "desktop.port"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _single_instance_lock(home: Path):
    """이미 떠 있으면 None 을 돌려준다. 소켓을 잡아두는 방식이라
    프로세스가 죽으면 락도 저절로 풀린다(파일 락은 남는다)."""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 51999))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


def _wait_until_up(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def run() -> int:
    # PyInstaller 로 묶으면 stdout 이 없을 수 있어 로깅을 파일로 돌린다.
    from . import config

    _ensure_streams()
    config.ensure_home()
    logging.basicConfig(
        level=logging.INFO,
        filename=str(config.HOME / "desktop.log"),
        filemode="a",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
    )
    log.info("=== memoinall 데스크톱 시작 ===")

    lock = _single_instance_lock(config.HOME)
    if lock is None:
        log.info("이미 실행 중입니다.")
        _notify("memoinall 이 이미 실행 중입니다.")
        return 0

    port = free_port()
    log.info("내부 서버 포트: %s", port)

    server = threading.Thread(target=_serve, args=(port,), daemon=True)
    server.start()

    if not _wait_until_up(port):
        log.error("서버가 뜨지 않았습니다.")
        _notify("서버를 시작하지 못했습니다.\n로그: " + str(config.HOME / "desktop.log"))
        return 1

    # 창이 안 뜨는 원인은 대개 GUI 백엔드 로딩 실패인데, 그게 어느 단계에서
    # 터지는지 로그가 없으면 알 수가 없다. 단계마다 남긴다.
    def _hook(exc_type, exc, tb):
        log.error("잡히지 않은 예외", exc_info=(exc_type, exc, tb))

    sys.excepthook = _hook
    threading.excepthook = lambda a: log.error(
        "스레드 예외", exc_info=(a.exc_type, a.exc_value, a.exc_traceback)
    )

    try:
        log.info("webview import…")
        import webview

        _log_webview_assets(webview)
        log.info("create_window…")
        window = webview.create_window(
            TITLE,
            f"http://127.0.0.1:{port}/",
            width=1180,
            height=820,
            min_size=(900, 600),
            text_select=True,
        )
        log.info("create_window OK")
        _install_downloader(window)
        log.info("webview.start() 진입 — 창이 닫힐 때까지 블로킹")
        webview.start()
        log.info("webview.start() 반환")
    except Exception:
        log.exception("창을 띄우지 못했습니다")
        _notify("창을 띄우지 못했습니다.\n로그: " + str(config.HOME / "desktop.log"))
        return 1
    log.info("=== 종료 ===")
    return 0


def _ensure_streams() -> None:
    """콘솔이 없는 실행(pythonw / console=False exe)에서는 sys.stdout 이 None 이다.

    uvicorn 은 기본 로깅 설정이 stdout 에 핸들러를 붙이므로, 이 상태로 두면
    서버가 조용히 죽고 창도 안 뜬다. 실제로 exe 를 만들기 전에 이걸로 한 번 막혔다.
    """
    import io

    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, io.StringIO())


def _log_webview_assets(webview) -> None:
    """pywebview 가 창을 띄우려면 파이썬 코드 말고도 네이티브 DLL 과 JS 가 필요하다.

    패키징이 이걸 빠뜨리면 예외도 없이 창만 안 뜬다 — 실제로 그렇게 한 번 막혔다.
    그래서 어디를 보고 있고 뭐가 있는지 남긴다.
    """
    root = Path(webview.__file__).parent
    log.info("webview 위치: %s", root)
    if getattr(sys, "frozen", False):
        log.info("frozen=True, _MEIPASS=%s", getattr(sys, "_MEIPASS", "?"))
    for rel in ("lib/Microsoft.Web.WebView2.Core.dll",
                "lib/Microsoft.Web.WebView2.WinForms.dll",
                "lib/runtimes/win-x64/native/WebView2Loader.dll",
                "js/api.js"):
        p = root / rel
        log.info("  %-58s %s", rel, "있음" if p.exists() else "없음")


def _serve(port: int) -> None:
    try:
        import uvicorn

        from .api import app

        _ensure_streams()
        # log_config=None: uvicorn 이 콘솔 핸들러를 새로 붙이지 않게 한다.
        # 우리 쪽 파일 로깅만 쓰면 충분하다.
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", log_config=None)
    except Exception:
        log.exception("내부 서버가 죽었습니다")


def _install_downloader(window) -> None:
    """웹 UI 의 파일 저장(a[download])을 네이티브 저장 대화상자로 연결한다.

    WebView2 는 blob: 다운로드를 그냥 무시해서, 브라우저에서 되던
    '파일로 저장'이 데스크톱에서는 아무 반응이 없다.
    """
    import webview

    def save(content: str, suggested: str):
        try:
            result = window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=suggested, file_types=("마크다운 (*.md)", "모든 파일 (*.*)")
            )
            if not result:
                return False
            path = result if isinstance(result, str) else result[0]
            Path(path).write_text(content, encoding="utf-8")
            return True
        except Exception:
            log.exception("파일 저장 실패")
            return False

    window.expose(save)


def _notify(message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x40)
    except Exception:
        print(message)


if __name__ == "__main__":
    sys.exit(run())
