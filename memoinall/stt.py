"""음성 입력 (STT) — 말한 내용을 메모 입력칸에 글로 넣는다.

엔진은 둘이다.
  - local  : faster-whisper. 오프라인이고 음성이 PC 밖으로 나가지 않는다. 기본값.
  - gemini : Gemini API. 설정에서 고를 수 있다. 음성이 Google 로 전송된다.

로컬 모델은 임베딩 모델처럼 exe 에 넣지 않는다. 처음 쓸 때 받아
~/.memoinall/models/whisper/<이름> 에 둔다. 재설치해도 다시 받지 않는다.

브라우저가 16kHz 모노 16bit WAV 로 바꿔 보내므로 서버는 오디오 디코딩(ffmpeg)을 하지 않는다.
변환 결과는 바로 저장하지 않고 입력칸에 넣는다 — 받아쓰기는 틀릴 수 있으니
사용자가 확인하고 저장해야 한다.
"""

from __future__ import annotations

import importlib.util
import io
import logging
import sys
import threading
import time
import types
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config, providers, settings

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
# 메모 한 번 분량. 상한이 있어야 변환 시간을 예측할 수 있고, 요청 크기도 막을 수 있다.
MAX_SECONDS = 300
MIN_SECONDS = 0.5
MAX_BYTES = (MAX_SECONDS + 10) * SAMPLE_RATE * 2 + 1024
# 이보다 작은 소리만 있으면 마이크가 꺼져 있거나 말을 안 한 것이다. 모델을 돌리지 않는다.
SILENCE_PEAK = 0.005

ENGINES = {"local": "로컬 Whisper", "gemini": "Gemini"}
DEFAULT_ENGINE = "local"


@dataclass(frozen=True)
class LocalModel:
    name: str
    label: str
    repo: str
    files: tuple[str, ...]  # model.bin 을 마지막에 둔다 — 다 받아야 '있음'으로 친다
    size_mb: int


LOCAL_MODELS: dict[str, LocalModel] = {
    "small": LocalModel(
        "small", "small — 빠름 (권장)", "Systran/faster-whisper-small",
        ("config.json", "tokenizer.json", "vocabulary.txt", "model.bin"), 486,
    ),
    "large-v3-turbo": LocalModel(
        "large-v3-turbo", "large-v3-turbo — 정확도 우선, 3~4배 느림", "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        ("config.json", "preprocessor_config.json", "tokenizer.json", "vocabulary.json", "model.bin"), 1622,
    ),
}
DEFAULT_LOCAL_MODEL = "small"

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"]
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
LIVE_MODEL = "gemini-3.5-transcribe-live"
GEMINI_PROMPT = (
    "첨부한 음성을 들리는 그대로 받아 적으세요. 한국어는 한글로, 영어 단어와 숫자는 들린 대로 적습니다. "
    "요약·설명·제목·따옴표 없이 받아쓴 문장만 출력하세요. 말소리가 없으면 아무것도 출력하지 마세요."
)


class SttError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 음성 입력 오류."""


# --------------------------------------------------------------------------- 설정


def engine_name() -> str:
    name = settings.get("stt.engine")
    return name if name in ENGINES else DEFAULT_ENGINE


def local_model_name() -> str:
    name = settings.get("stt.local.model")
    return name if name in LOCAL_MODELS else DEFAULT_LOCAL_MODEL


def gemini_model_name() -> str:
    return settings.get("stt.gemini.model").strip() or DEFAULT_GEMINI_MODEL


def gemini_transport() -> str:
    saved = settings.get("stt.gemini.transport")
    if saved in {"basic", "live"}:
        return saved
    return "live" if gemini_model_name().removeprefix("models/") == LIVE_MODEL else "basic"


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def local_package_available() -> bool:
    return _module_available("faster_whisper")


def _import_whisper_model():
    """faster-whisper 는 import 할 때 PyAV(ffmpeg 포함 약 68MB)를 끌어오지만,
    PyAV 는 파일을 디코딩하는 decode_audio() 에서만 쓴다. 우리는 PCM 배열을 바로 넘기므로
    exe 에서는 PyAV 를 빼고(memoinall.spec) 빈 모듈로 import 만 통과시킨다.
    개발 환경처럼 PyAV 가 있으면 진짜를 쓴다.
    """
    if "av" not in sys.modules and not _module_available("av"):
        sys.modules["av"] = types.ModuleType("av")
    from faster_whisper import WhisperModel

    return WhisperModel


def models_dir() -> Path:
    return config.HOME / "models" / "whisper"


def model_dir(name: str) -> Path:
    return models_dir() / name


def is_downloaded(name: str) -> bool:
    model = LOCAL_MODELS.get(name)
    if model is None:
        return False
    d = model_dir(name)
    return all((d / f).is_file() and (d / f).stat().st_size > 0 for f in model.files)


# --------------------------------------------------------------------------- 모델 받기

_job_lock = threading.Lock()
_job: dict = {"model": None, "running": False, "done": 0, "total": 0, "error": None}


def start_download(name: str | None = None) -> None:
    """모델을 백그라운드에서 받는다. 진행률은 status() 의 download 로 본다."""
    name = name or local_model_name()
    model = LOCAL_MODELS.get(name)
    if model is None:
        raise SttError(f"알 수 없는 음성 인식 모델: {name}")
    with _job_lock:
        if _job["running"]:
            if _job["model"] == name:
                return
            raise SttError(f"{_job['model']} 모델을 받는 중입니다. 끝난 뒤 다시 시도하세요.")
        if is_downloaded(name):
            return
        _job.update(model=name, running=True, done=0, total=model.size_mb * 1_000_000, error=None)
    threading.Thread(target=_download, args=(model,), daemon=True, name="stt-download").start()


def _download(model: LocalModel) -> None:
    from .embed_onnx import HF_BASE, _fetch

    target = model_dir(model.name)
    finished = 0
    try:
        target.mkdir(parents=True, exist_ok=True)
        for fname in model.files:
            dest = target / fname
            if dest.is_file() and dest.stat().st_size > 0:
                finished += dest.stat().st_size
                continue

            def progress(_label, done, _total, base=finished):
                with _job_lock:
                    _job["done"] = base + done

            tmp = dest.with_name(fname + ".part")
            log.info("음성 인식 모델 내려받는 중: %s/%s", model.repo, fname)
            _fetch(HF_BASE.format(repo=model.repo, path=fname), tmp, progress, fname)
            tmp.replace(dest)  # 중간에 끊겨도 반쪽 파일이 '받음'으로 보이지 않게
            finished += dest.stat().st_size
        with _job_lock:
            _job.update(running=False, done=finished, total=finished)
        log.info("음성 인식 모델 준비 완료: %s", model.name)
    except Exception as exc:
        log.exception("음성 인식 모델을 받지 못했습니다")
        with _job_lock:
            _job.update(running=False, error=f"모델을 받지 못했습니다: {exc}")


def _download_snapshot() -> dict:
    with _job_lock:
        job = dict(_job)
    job["percent"] = min(99, int(job["done"] * 100 / job["total"])) if job["running"] and job["total"] else 0
    return job


# --------------------------------------------------------------------------- 상태


def _size_label(mb: int) -> str:
    return f"{mb / 1000:.1f}GB" if mb >= 1000 else f"{mb}MB"


def _readiness(engine: str, local: str, job: dict, gemini_key: bool) -> tuple[str, str]:
    if engine == "gemini":
        if not gemini_key:
            return "needs_key", "Gemini API 키가 없습니다. 설정 → 음성 입력에서 키를 넣으세요."
        if gemini_transport() == "live":
            return "ready", f"Gemini Live {LIVE_MODEL}로 음성을 실시간 전송합니다."
        if gemini_model_name().removeprefix("models/") == LIVE_MODEL:
            return "needs_transport", "이 모델은 설정 → 음성 입력에서 실시간 WebSocket 방식을 선택하세요."
        if not _module_available("openai"):
            return "missing_package", "openai 패키지가 없어 Gemini 음성 인식을 쓸 수 없습니다.  pip install openai"
        return "ready", f"Gemini {gemini_model_name()} 로 옮깁니다. 음성이 Google 로 전송됩니다."

    if not local_package_available():
        return "missing_package", (
            "faster-whisper 패키지가 없어 로컬 음성 인식을 쓸 수 없습니다.  pip install faster-whisper "
            "— 또는 설정 → 음성 입력에서 Gemini 를 고르세요."
        )
    if job["running"] and job["model"] == local:
        return "downloading", f"음성 인식 모델 받는 중 {job['percent']}% · 메모는 계속 쓸 수 있습니다"
    if not is_downloaded(local):
        model = LOCAL_MODELS[local]
        failed = f" (지난 시도: {job['error']})" if job["error"] and job["model"] == local else ""
        return "needs_download", f"음성 인식 모델({local}, 약 {_size_label(model.size_mb)})을 처음 한 번 받아야 합니다.{failed}"
    return "ready", f"로컬 Whisper {local} — 음성이 이 PC 밖으로 나가지 않습니다."


def status() -> dict:
    engine, local = engine_name(), local_model_name()
    job = _download_snapshot()
    gemini_key = settings.provider_ready("gemini")
    state, message = _readiness(engine, local, job, gemini_key)
    return {
        "engine": engine,
        "engines": [{"value": k, "label": v} for k, v in ENGINES.items()],
        "state": state,
        "ready": state == "ready",
        "message": message,
        "local_model": local,
        "local_models": [
            {"name": m.name, "label": m.label, "size_mb": m.size_mb, "size": _size_label(m.size_mb),
             "downloaded": is_downloaded(m.name)}
            for m in LOCAL_MODELS.values()
        ],
        "local_package": local_package_available(),
        "gemini_model": gemini_model_name(),
        "gemini_transport": gemini_transport(),
        "gemini_live_model": LIVE_MODEL,
        "gemini_models": GEMINI_MODELS,
        "gemini_key": gemini_key,
        "download": job,
        "max_seconds": MAX_SECONDS,
        "models_dir": str(models_dir()),
    }


# --------------------------------------------------------------------------- 변환


def read_wav(data: bytes) -> np.ndarray:
    """16kHz 모노 16bit WAV → float32 [-1, 1]."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, SAMPLE_RATE):
                raise SttError("16kHz 모노 16bit WAV 만 받습니다.")
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as exc:
        raise SttError("녹음 파일을 읽지 못했습니다.") from exc
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def transcribe(data: bytes, *, partial: bool = False) -> dict:
    """WAV 한 구간을 글로 옮긴다.

    화면은 말을 '쉼' 단위 구간으로 끊어 보낸다. partial 은 말하는 도중 보여 줄 초안이다 —
    빨라야 하므로 로컬은 빔 탐색을 줄인다. 쉼에서 보내는 확정본은 정확하게 옮긴다.
    """
    if len(data) > MAX_BYTES:
        raise SttError(f"녹음은 한 번에 {MAX_SECONDS // 60}분까지 옮길 수 있습니다.")
    pcm = read_wav(data)
    seconds = len(pcm) / SAMPLE_RATE
    if seconds < MIN_SECONDS:
        raise SttError("녹음이 너무 짧습니다. 🎤를 누르고 말한 뒤 다시 누르세요.")
    if seconds > MAX_SECONDS + 5:
        raise SttError(f"녹음은 한 번에 {MAX_SECONDS // 60}분까지 옮길 수 있습니다.")

    engine = engine_name()
    if engine == "gemini" and gemini_transport() == "live":
        raise SttError("실시간 음성 입력은 WebSocket으로 연결하세요.")
    model = gemini_model_name() if engine == "gemini" else local_model_name()
    started = time.monotonic()
    peak = float(np.abs(pcm).max()) if pcm.size else 0.0
    # 소리가 아예 안 들어온 것(음소거·Windows 마이크 권한)과 알아듣지 못한 것을 구분해 알려 준다.
    silent = peak < SILENCE_PEAK
    if silent:
        text = ""
    elif engine == "gemini":
        text = _transcribe_gemini(data, model)
    else:
        text = _transcribe_local(pcm, model, partial=partial)
    elapsed = time.monotonic() - started
    # 초안은 1초마다 오므로 기본 로그에는 남기지 않는다.
    log.log(logging.DEBUG if partial else logging.INFO,
            "음성 변환%s: %s/%s %.1f초 분량(최대 음량 %.4f) → %.1f초, %d자",
            "(초안)" if partial else "", engine, model, seconds, peak, elapsed, len(text))
    return {
        "text": text.strip(),
        "silent": silent,
        "engine": engine,
        "model": model,
        "seconds": round(seconds, 1),
        "elapsed": round(elapsed, 1),
    }


# 모델 적재와 변환은 한 번에 하나씩 — CPU 를 나눠 쓰면 둘 다 느려진다.
_engine_lock = threading.Lock()
_loaded: tuple[str, object] | None = None


def _load_local(name: str):
    """_engine_lock 을 잡은 상태에서 부른다."""
    global _loaded
    if _loaded and _loaded[0] == name:
        return _loaded[1]
    if not local_package_available():
        raise SttError("faster-whisper 패키지가 없어 로컬 음성 인식을 쓸 수 없습니다.")
    if not is_downloaded(name):
        raise SttError(f"음성 인식 모델({name})을 먼저 받아야 합니다.")
    WhisperModel = _import_whisper_model()

    _loaded = None  # 다른 모델로 바꿀 때 이전 모델 메모리를 먼저 놓는다
    started = time.monotonic()
    model = WhisperModel(str(model_dir(name)), device="cpu", compute_type="int8")
    log.info("음성 인식 모델 적재: %s (%.1fs)", name, time.monotonic() - started)
    _loaded = (name, model)
    return model


def _transcribe_local(pcm: np.ndarray, name: str, *, partial: bool = False) -> str:
    with _engine_lock:
        model = _load_local(name)
        # vad_filter: 말 없는 구간을 잘라낸다. Whisper 는 무음에서 "시청해주셔서 감사합니다"
        # 같은 문장을 지어내는 일이 있어서 이게 없으면 메모에 엉뚱한 말이 들어간다.
        # 초안은 빔 1(그리디)로 2~3배 빠르게, 확정본은 기본 빔 5로 정확하게 옮긴다.
        segments, _info = model.transcribe(
            pcm, language="ko", vad_filter=True, condition_on_previous_text=False,
            beam_size=1 if partial else 5, without_timestamps=partial,
        )
        return " ".join(s.text.strip() for s in segments if s.text.strip())


def prepare() -> None:
    """녹음을 시작할 때 부른다. 말하는 동안 모델을 올려 두면 첫 변환이 몇 초 빨라진다."""
    if engine_name() != "local":
        return
    name = local_model_name()
    if not (local_package_available() and is_downloaded(name)):
        return
    if _loaded and _loaded[0] == name:
        return

    def warm():
        try:
            with _engine_lock:
                _load_local(name)
        except Exception:
            log.exception("음성 인식 모델 적재 실패")

    threading.Thread(target=warm, daemon=True, name="stt-warm").start()


def _transcribe_gemini(wav: bytes, model: str) -> str:
    from .providers.openai_compat import OpenAICompatProvider

    cfg = settings.provider_config("gemini")
    if not cfg.get("api_key"):
        raise SttError("Gemini API 키가 없습니다. 설정 → 음성 입력에서 키를 넣으세요.")
    cfg["model"] = model
    provider = OpenAICompatProvider(providers.spec("gemini"), cfg)
    try:
        return provider.transcribe(wav, GEMINI_PROMPT)
    except providers.ProviderError:
        raise
    except Exception as exc:  # SDK 의 인증·요청 오류 — 이유를 그대로 보여 준다
        raise providers.ProviderError(f"{type(exc).__name__}: {exc}") from exc
