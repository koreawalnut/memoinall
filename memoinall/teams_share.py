"""Pass a draft or an export file to Teams on Windows.

Teams 새 채팅 링크로 새 채팅을 열고, 그 화면에 글과 파일(메모 .json·일정 .ics)을 넣는다(teams_attach).
링크는 글만 미리 채울 수 있고 파일은 붙일 수 없으며, 받는 사람 없는 예전 초안이 남아 있으면 링크의 글도 무시된다.
자동으로 넣지 못하면 새 채팅만 열어 두고, 사용자가 '파일 첨부'로 직접 올리거나 파일을 끌어다 놓게 안내한다.
보내기는 언제나 사용자가 한다.
"""

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

from . import config, exchange
from .teams_attach import AttachFailed, fill_new_chat

log = logging.getLogger(__name__)
EXPORT_STEM = r"memoinall-[0-9]{8}-[0-9]{4}-[0-9]+건"
SCHEDULE_STEM = r"일정공유-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{4}-[0-9]{2}-[0-9]{2}"
EXPORT_NAME = re.compile(EXPORT_STEM + r"\.json|" + SCHEDULE_STEM + r"\.ics")
STAGED_NAME = re.compile(EXPORT_STEM + r"(?: \([0-9]+\))?\.json|" + SCHEDULE_STEM + r"(?: \([0-9]+\))?\.ics")
# 함께 보낼 글이 없을 때 쓰는 글. 새 채팅을 알아보는 표시도 된다.
DEFAULT_TEXT = {".json": "memoinall 메모 파일을 보냅니다.", ".ics": "memoinall 일정 파일을 보냅니다."}
NEW_CHAT = "msteams://teams.microsoft.com/l/chat/0/0?users="
# Long messages travel intact via the clipboard, without relying on URI length limits.
MAX_CHAT_URI = 8000


def _chat_uri(message):
    uri = NEW_CHAT + "&message=" + quote(message, safe="")
    return uri if len(message) <= MAX_CHAT_URI and len(uri) <= MAX_CHAT_URI else ""


def _fill(fill, *args):
    """새 채팅에 넣고, 중단된 경우 사용자에게 표시할 이유를 전달한다."""
    if fill is None:
        return None
    try:
        return fill(*args)
    except AttachFailed as exc:
        log.info("Teams 새 채팅에 자동으로 넣지 못함: %s", exc)
        return {"message": False, "attached": False, "reason": str(exc)}
    except Exception:  # UI Automation 은 화면이 바뀌면 .NET 예외를 던진다
        log.warning("Teams 새 채팅 자동 입력 중 오류", exc_info=True)
        return {"message": False, "attached": False, "reason": "Teams 화면의 첨부 상태를 읽지 못했습니다."}


def _open_text_chat(content, copy_text, fill=None):
    """본문을 새 채팅에. fill 이 있으면 화면에서 직접 넣고(예전 초안이 남아 링크의 글이 무시돼도) 'filled'."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("공유할 메모 본문이 비어 있습니다.")
    uri = _chat_uri(content)
    prefilled = bool(uri)
    # Teams can retain an unfinished draft when opening a second recipient-less chat.
    try:
        copy_text(content)
        copied = True
    except Exception:
        copied = False
        if not prefilled:
            raise RuntimeError("긴 본문을 클립보드에 복사하지 못했습니다. 아래 ‘본문 복사’를 눌러주세요.") from None
    try:
        os.startfile(uri if prefilled else NEW_CHAT)
    except OSError as exc:
        # A ShellExecute error may include the full draft URI. Keep it out of logs.
        log.warning("Teams 새 채팅 실행 실패 (Windows 오류 %s)", getattr(exc, "winerror", None))
        hint = "본문은 클립보드에 복사했습니다." if copied else "아래 ‘본문 복사’를 사용할 수 있습니다."
        raise RuntimeError("Windows용 Teams를 열 수 없습니다. Teams 설치 상태를 확인하세요. " + hint) from None
    filled = _fill(fill, content)
    if filled and filled["message"]:
        return "filled"
    if not prefilled:
        return "paste"
    return "draft-copied" if copied else "draft"


def _share_dir():
    return config.HOME / "teams-share"


def _expire_attachments(folder):
    cutoff = time.time() - 24 * 3600
    try:
        for path in folder.iterdir():
            if STAGED_NAME.fullmatch(path.name):
                try:
                    if path.lstat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    continue
    except OSError:
        log.debug("Could not inspect the Teams attachment cache")


def _stage_attachment(content, filename):
    if not isinstance(filename, str) or not EXPORT_NAME.fullmatch(filename):
        raise ValueError("공유할 파일 이름이 올바르지 않습니다.")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("공유할 내용이 비어 있습니다.")
    data = content.encode("utf-8")
    if len(data) > exchange.MAX_BYTES:
        raise ValueError("첨부파일은 50MB까지 공유할 수 있습니다. 메모를 나눠 선택하세요.")
    folder = _share_dir()
    folder.mkdir(parents=True, exist_ok=True)
    _expire_attachments(folder)
    path = folder / filename
    copy_number = 1
    while True:
        try:
            stream = path.open("xb")
            break
        except FileExistsError:
            copy_number += 1
            path = folder / f"{Path(filename).stem} ({copy_number}){Path(filename).suffix}"
    try:
        with stream:
            stream.write(data)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return path.absolute()


def _share_file(content, filename, text, fill):
    """파일을 준비하고 Teams 새 채팅을 열어 글과 파일을 넣는다.

    method "attached": 새 채팅에 파일이 붙은 것까지 확인했다 — 받는 사람만 고르면 된다.
    method "manual":   새 채팅만 열었다(또는 Teams 를 못 열었다, opened=False) — 사용자가 직접 올린다.
    """
    if text is not None and (not isinstance(text, str) or len(text) > 20000):
        raise ValueError("함께 보낼 글이 올바르지 않습니다.")
    path = _stage_attachment(content, filename)
    message = text or DEFAULT_TEXT[path.suffix]
    result = {"path": str(path), "name": path.name, "method": "manual", "opened": True}
    try:
        os.startfile(_chat_uri(message) or NEW_CHAT)
    except OSError as exc:
        log.warning("Teams 새 채팅 실행 실패 (Windows 오류 %s)", getattr(exc, "winerror", None))
        return {**result, "opened": False}
    filled = _fill(fill, message, path)
    if filled and filled["attached"]:
        result["method"] = "attached"
    elif filled and filled.get("reason"):
        result["reason"] = filled["reason"]
    elif filled:
        # The file picker already accepted the path. Don't suggest a second upload.
        result["method"] = "unconfirmed"
    return result


def reveal_teams_file(name):
    if not isinstance(name, str) or not STAGED_NAME.fullmatch(name):
        raise ValueError("공유할 파일 이름이 올바르지 않습니다.")
    path = (_share_dir() / name).absolute()
    if not path.is_file():
        raise ValueError("준비한 파일이 없습니다. 메모를 선택하고 다시 공유하세요.")
    subprocess.Popen(["explorer.exe", "/select,", str(path)])


def install(window):
    busy = threading.Lock()
    _expire_attachments(_share_dir())

    def share_to_teams(content, filename=None, text=None):
        if not busy.acquire(blocking=False):
            raise ValueError("진행 중인 공유를 마친 뒤 다시 시도하세요.")
        try:
            from System import Action
            from System.Windows.Forms import Clipboard

            def copy_text(value):
                window.native.Invoke(Action(lambda: Clipboard.SetText(value)))

            def fill(message, path=None):
                return fill_new_chat(message, copy_text, file_path=str(path) if path else None)

            if filename is None:
                return _open_text_chat(content, copy_text, fill)
            return _share_file(content, filename, text, fill)
        except ImportError:
            raise RuntimeError("Windows 데스크톱 구성요소가 필요합니다. 최신 memoinall 데스크톱 버전을 사용하세요.") from None
        except OSError:
            log.exception("Teams 첨부파일 준비 실패")
            raise RuntimeError("첨부파일을 준비할 수 없습니다. 저장 공간과 폴더 권한을 확인하세요.") from None
        finally:
            busy.release()

    window.expose(share_to_teams, reveal_teams_file)
