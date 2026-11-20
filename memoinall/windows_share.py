"""Windows 공유 창(DataTransferManager)으로 파일을 넘긴다.

Teams 새 채팅 링크(msteams://…/chat)는 메시지 글만 미리 채울 수 있고 파일은 첨부할 수 없다.
그래서 파일은 Windows 공유 창으로 넘긴다. 새 Teams 는 Windows 공유 대상이어서(StorageItems, 모든 파일 형식)
공유 창에서 Microsoft Teams 를 고르면 파일이 첨부된 채로 Teams 공유 창이 열린다.

데스크톱(Win32) 창에서 공유 창을 여는 문서화된 방법은 IDataTransferManagerInterop 이다.
  GetForWindow(hwnd)       그 창의 DataTransferManager 를 얻는다 → DataRequested 에서 내용을 채운다
  ShowShareUIForWindow(hwnd) 공유 창을 연다
두 메서드는 PyWinRT 에 투영돼 있지 않아 vtable 로 직접 부르고, 돌려받은 객체는 PyWinRT 로 감싸 쓴다.
관리자 권한으로 실행 중이면 Windows 가 공유 창을 띄우지 않으므로 처음부터 쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import threading
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

IID_DATA_TRANSFER_MANAGER_INTEROP = uuid.UUID("3A3DCD6C-3EAB-43DC-BCDE-45671CE800C8")
IID_DATA_TRANSFER_MANAGER = uuid.UUID("A5CAEE9B-8708-49D1-8D36-67D25A8DA00C")
_CAPSULE_NAME = b"winrt.interface"      # PyWinRT 가 COM 포인터를 주고받는 캡슐 이름


class ShareUnavailable(RuntimeError):
    """공유 창을 쓸 수 없을 때. 부르는 쪽은 클립보드·파일 위치 열기로 대신한다."""


class _GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]


def _guid(value: uuid.UUID) -> _GUID:
    out = _GUID()
    ctypes.memmove(out.data, value.bytes_le, 16)
    return out


def _elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _winrt():
    try:
        from winrt.runtime import interop
        from winrt.windows.applicationmodel.datatransfer import DataTransferManager
        from winrt.windows.storage import StorageFile
    except ImportError as exc:
        raise ShareUnavailable("Windows 공유 구성요소(winrt)가 없습니다.") from exc
    return interop, DataTransferManager, StorageFile


def _storage_file(path: Path):
    """공유할 파일을 WinRT StorageFile 로. UI 스레드를 막지 않게 부르는 쪽(작업 스레드)에서 연다."""
    _, _, StorageFile = _winrt()
    try:
        from winrt.runtime import MTA, init_apartment
        init_apartment(MTA)
    except Exception:      # 이미 이 스레드에 COM 이 준비돼 있으면 그대로 쓴다
        pass

    async def load():
        return await StorageFile.get_file_from_path_async(str(path))

    return asyncio.run(load())


class Sharer:
    """창마다 DataTransferManager 하나에 DataRequested 를 한 번 걸고, 공유할 때마다 내용만 바꾼다."""

    def __init__(self):
        self._lock = threading.Lock()
        self._managers: dict[int, tuple] = {}
        self._pending: dict | None = None

    def share(self, run_on_ui, get_hwnd, path: Path, *, title: str, text: str = "") -> None:
        """공유 창을 연다. 열지 못하면 ShareUnavailable. 무엇을 골랐는지는 알 수 없다(Windows 가 알려주지 않는다)."""
        if _elevated():
            raise ShareUnavailable("관리자 권한으로 실행 중이라 Windows 공유 창을 열 수 없습니다.")
        try:
            file = _storage_file(path)
        except ShareUnavailable:
            raise
        except Exception as exc:
            log.warning("공유할 파일을 WinRT 로 열지 못함: %r", exc)
            raise ShareUnavailable("공유할 파일을 열지 못했습니다.") from exc
        errors: list[BaseException] = []
        with self._lock:
            self._pending = {"file": file, "title": title, "description": path.name, "text": text}

            def on_ui():
                try:
                    self._show(get_hwnd())
                except BaseException as exc:   # UI 스레드 밖으로 예외를 넘긴다
                    errors.append(exc)

            run_on_ui(on_ui)
        if errors:
            log.warning("Windows 공유 창 실패: %r", errors[0])
            raise ShareUnavailable("Windows 공유 창을 열지 못했습니다.") from errors[0]

    @staticmethod
    def _interop():
        """IDataTransferManagerInterop 과 그 두 메서드. factory 를 쥐고 있는 동안만 pointer 가 유효하다."""
        interop, DataTransferManager, _ = _winrt()
        factory = interop.get_activation_factory(DataTransferManager, IID_DATA_TRANSFER_MANAGER_INTEROP)
        pointer = interop.interface_pointer(factory)
        vtable = ctypes.cast(ctypes.c_void_p.from_address(pointer).value, ctypes.POINTER(ctypes.c_void_p))
        # IUnknown 3개 다음: GetForWindow, ShowShareUIForWindow
        get_for_window = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(vtable[3])
        show_for_window = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p)(vtable[4])
        return factory, pointer, get_for_window, show_for_window

    def _manager(self, hwnd: int, pointer: int, get_for_window):
        """그 창의 DataTransferManager. 처음 한 번만 얻어 DataRequested 를 건다(UI 스레드에서)."""
        if hwnd not in self._managers:
            interop, DataTransferManager, _ = _winrt()
            raw = ctypes.c_void_p()
            get_for_window(pointer, hwnd, ctypes.byref(_guid(IID_DATA_TRANSFER_MANAGER)), ctypes.byref(raw))
            capsule = _capsule(raw.value)
            manager = interop.wrap_interface(capsule, DataTransferManager)
            token = manager.add_data_requested(self._on_requested)
            self._managers[hwnd] = (manager, token, capsule)
        return self._managers[hwnd][0]

    def _show(self, hwnd: int) -> None:
        factory, pointer, get_for_window, show_for_window = self._interop()
        self._manager(hwnd, pointer, get_for_window)
        show_for_window(pointer, hwnd)
        del factory

    def _on_requested(self, _sender, args) -> None:
        pending = self._pending
        request = args.request
        try:
            if not pending:
                request.fail_with_display_text("공유할 파일이 없습니다. memoinall 에서 다시 공유하세요.")
                return
            data = request.data
            data.properties.title = pending["title"]
            data.properties.description = pending["description"]
            if pending["text"]:
                data.set_text(pending["text"])
            data.set_storage_items([pending["file"]])
        except Exception:
            log.exception("공유 내용 채우기 실패")
            try:
                request.fail_with_display_text("공유할 파일을 준비하지 못했습니다.")
            except Exception:
                pass


def _capsule(pointer: int):
    """GetForWindow 가 준 COM 포인터를 PyWinRT 가 받는 캡슐로. 참조 하나는 창이 살아 있는 동안 쓴다."""
    new = ctypes.pythonapi.PyCapsule_New
    new.restype = ctypes.py_object
    new.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
    return new(pointer, _CAPSULE_NAME, None)
