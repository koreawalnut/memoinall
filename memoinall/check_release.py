"""Offline smoke check inside the frozen desktop runtime."""
import importlib
import traceback
from pathlib import Path


def check(report: str) -> int:
    try:
        for module in ("webview.platforms.winforms", "onnxruntime", "tokenizers", "fastapi", "uvicorn",
                       "websockets.asyncio.client", "cryptography.hazmat.primitives.ciphers.aead",
                       "icalendar", "recurring_ical_events", "tzdata", "anthropic", "openai",
                       "pypdf", "openpyxl", "xlrd", "olefile", "defusedxml"):
            importlib.import_module(module)
        from .stt import _import_whisper_model
        _import_whisper_model()
        from faster_whisper.vad import get_vad_model
        get_vad_model()
    except Exception:
        Path(report).write_text(traceback.format_exc(), encoding="utf-8")
        return 1
    Path(report).write_text("OK", encoding="utf-8")
    return 0
