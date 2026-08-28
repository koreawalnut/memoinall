"""임베딩 계층.

설계 의도: 앱은 모델 다운로드를 기다리지 않는다.
- 시작하자마자 해시 기반 임베더로 동작한다(품질은 낮지만 즉시 검색 가능).
- 백그라운드에서 sentence-transformers 모델을 로드하고, 끝나면 교체한다.
- 청크마다 어떤 모델로 만들었는지 기록해두고, 모델이 바뀌면 재임베딩한다.
"""

from __future__ import annotations

import hashlib
import logging
import threading

import numpy as np

from . import config

log = logging.getLogger(__name__)

HASH_MODEL = "hash-v1"


class HashEmbedder:
    """의존성 없는 폴백. 문자 n-gram 해싱 + L2 정규화."""

    name = HASH_MODEL
    dim = config.EMBED_DIM_FALLBACK

    def encode(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        from .textutil import ngrams

        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for tok in ngrams(text):
                h = int.from_bytes(hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest(), "little")
                idx = h % self.dim
                sign = 1.0 if (h >> 63) & 1 else -1.0
                out[i, idx] += sign
        return _l2(out)


class STEmbedder:
    """sentence-transformers 래퍼."""

    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.name = model_name
        # sentence-transformers 3.x 에서 이름이 바뀌었다. 둘 다 받아준다.
        getter = getattr(self._model, "get_embedding_dimension", None) or self._model.get_sentence_embedding_dimension
        self.dim = int(getter())

    def encode(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        if config.EMBED_USE_E5_PREFIX:
            prefix = "query: " if is_query else "passage: "
            texts = [prefix + t for t in texts]
        vecs = self._model.encode(texts, batch_size=16, show_progress_bar=False, convert_to_numpy=True)
        return _l2(np.asarray(vecs, dtype=np.float32))


def _l2(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


_lock = threading.Lock()
_current = HashEmbedder()
_state = "fallback"  # fallback | loading | downloading | ready | failed
_error = ""
_backend = "hash"  # hash | onnx | sentence-transformers
_progress: dict = {}
_on_upgrade: list = []


def current():
    return _current


def status() -> dict:
    return {
        "model": _current.name,
        "dim": _current.dim,
        "state": _state,
        "error": _error,
        "backend": _backend,
        "progress": dict(_progress),
    }


def on_upgrade(callback) -> None:
    """모델이 교체되면 호출된다(재임베딩 트리거용)."""
    _on_upgrade.append(callback)


def ensure_loaded_async() -> None:
    """백그라운드로 실제 모델을 올린다. 이미 시도했으면 아무것도 안 한다."""
    global _state
    with _lock:
        disabled = config.DISABLE_ST or config.EMBED_BACKEND == "hash"
        if disabled or _state in {"loading", "downloading", "ready", "failed"}:
            return
        _state = "loading"
    threading.Thread(target=_load, name="embed-load", daemon=True).start()


def _load() -> None:
    """ONNX → sentence-transformers → (실패 시) 해시 폴백 순으로 시도한다.

    ONNX 를 먼저 쓰는 이유: torch 없이 돌고(패키징 크기 1/10), 로드가 10배 빠르며,
    가중치가 같아 **벡터가 완전히 동일**하다. 그래서 백엔드가 바뀌어도
    이미 저장된 임베딩을 그대로 쓸 수 있다(모델명이 같으므로 재색인 불필요).
    """
    global _current, _state, _error, _backend
    wanted = config.EMBED_BACKEND
    candidates = [(_load_onnx, "onnx"), (_load_st, "sentence-transformers")]
    if wanted == "onnx":
        candidates = candidates[:1]
    elif wanted == "st":
        candidates = candidates[1:]

    for loader, name in candidates:
        try:
            log.info("임베딩 백엔드 시도: %s", name)
            model = loader()
        except Exception as exc:
            log.info("%s 사용 불가: %s: %s", name, type(exc).__name__, exc)
            with _lock:
                _error = f"{type(exc).__name__}: {exc}"
            continue

        with _lock:
            _current, _state, _backend, _error = model, "ready", name, ""
            _progress.clear()
        log.info("임베딩 준비 완료: %s / %s (dim=%d)", name, model.name, model.dim)
        for cb in list(_on_upgrade):
            try:
                cb(model.name)
            except Exception:
                log.exception("임베딩 업그레이드 콜백 실패")
        return

    with _lock:
        _state = "failed"
    log.warning("임베딩 모델 로드 실패, 해시 임베더로 계속합니다: %s", _error)


def _load_onnx():
    from . import embed_onnx

    if not embed_onnx.available():
        raise RuntimeError("onnxruntime/tokenizers 없음")

    if not embed_onnx.is_downloaded():
        global _state
        with _lock:
            _state = "downloading"
        embed_onnx.download(progress=_note_progress)
    return embed_onnx.OnnxEmbedder()


def _note_progress(name: str, done: int, total: int) -> None:
    _progress.update({"file": name, "done": done, "total": total,
                      "percent": round(done / total * 100, 1) if total else 0})


def _load_st():
    if config.DISABLE_ST:
        raise RuntimeError("MEMOINALL_DISABLE_ST 설정됨")
    return STEmbedder(config.EMBED_MODEL)


def encode(texts: list[str], *, is_query: bool = False) -> tuple[np.ndarray, str]:
    emb = _current
    if not texts:
        return np.zeros((0, emb.dim), dtype=np.float32), emb.name
    return emb.encode(texts, is_query=is_query), emb.name


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32, count=dim)
