"""Pass a draft or an export file to Teams on Windows."""

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

from . import config, exchange

log = logging.getLogger(__name__)
EXPORT_STEM = r"memoinall-[0-9]{8}-[0-9]{4}-[0-9]+건"
EXPORT_NAME = re.compile(EXPORT_STEM + r"\.json")
STAGED_NAME = re.compile(EXPORT_STEM + r"(?: \([0-9]+\))?\.json")
NEW_CHAT = "msteams://teams.microsoft.com/l/chat/0/0?users="
# Long messages travel intact via the clipboard, without relying on URI length limits.
MAX_CHAT_URI = 8000


def _open_text_chat(content, copy_text):
    if not isinstance(content, str) or not content.strip():
        raise ValueError("공유할 메모 본문이 비어 있습니다.")
    uri = ""
    if len(content) <= MAX_CHAT_URI:
        uri = NEW_CHAT + "&message=" + quote(content, safe="")
    prefilled = bool(uri) and len(uri) <= MAX_CHAT_URI
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
    if not prefilled:
        return "paste"
    return "draft-copied" if copied else "draft"


def _share_dir():
    return config.HOME / "teams-share"


def _expire_attachments(folder):
    cutoff = time.time() - 24 * 3600
    try:
        for path in folder.glob("memoinall-*.json"):
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
        raise ValueError("메모 내보내기 파일 이름이 올바르지 않습니다.")
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
            path = folder / f"{Path(filename).stem} ({copy_number}).json"
    try:
        with stream:
            stream.write(data)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return path.absolute()


def _file_drop_list(path):
    from System.Collections.Specialized import StringCollection

    files = StringCollection()
    files.Add(str(path))
    return files


def _open_file_chat(content, filename, copy_file):
    path = _stage_attachment(content, filename)
    try:
        copy_file(path)
        copied = True
    except Exception:
        copied = False
    try:
        os.startfile(NEW_CHAT)
        opened = True
    except OSError:
        opened = False
    return {"path": str(path), "name": path.name, "copied": copied, "opened": opened}


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

    def share_to_teams(content, filename=None):
        if not busy.acquire(blocking=False):
            raise ValueError("진행 중인 공유를 마친 뒤 다시 시도하세요.")
        try:
            from System import Action
            from System.Windows.Forms import Clipboard

            if filename is None:
                def copy_text(text):
                    window.native.Invoke(Action(lambda: Clipboard.SetText(text)))

                return _open_text_chat(content, copy_text)

            def copy_file(path):
                window.native.Invoke(Action(lambda: Clipboard.SetFileDropList(_file_drop_list(path))))

            return _open_file_chat(content, filename, copy_file)
        except ImportError:
            raise RuntimeError("Windows 공유 구성요소가 필요합니다. 최신 memoinall 데스크톱 버전을 사용하세요.") from None
        except OSError:
            log.exception("Teams 첨부파일 준비 실패")
            raise RuntimeError("첨부파일을 준비할 수 없습니다. 저장 공간과 폴더 권한을 확인하세요.") from None
        finally:
            busy.release()

    window.expose(share_to_teams, reveal_teams_file)
