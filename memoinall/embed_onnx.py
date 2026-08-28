"""ONNX 임베더 — 패키징(단일 exe)용 경량 백엔드.

sentence-transformers 경로는 torch(496MB) + transformers(94MB) 를 끌고 온다.
PyInstaller 로 한 파일 exe 를 만들면 1GB 를 훌쩍 넘고, onefile 은 실행할 때마다
임시폴더에 압축을 풀기 때문에 시작이 수십 초 걸린다.

onnxruntime(42MB) + tokenizers(7MB) 로 같은 모델을 돌리면 라이브러리가 10분의 1이 된다.
가중치가 같으므로 결과 벡터도 같다 — 풀링(mean pooling)과 정규화만 맞춰주면 된다.

모델 파일은 exe 에 넣지 않고 첫 실행 때 받아 사용자 폴더에 둔다.
exe 는 작게 유지되고, 재설치해도 모델을 다시 받지 않는다.
"""

from __future__ import annotations

import logging
import threading
import urllib.request
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger(__name__)

HF_BASE = "https://huggingface.co/{repo}/resolve/main/{path}"
# (저장할 이름, 저장소 내 경로)
FILES = [("model.onnx", "onnx/model.onnx"), ("tokenizer.json", "tokenizer.json")]

MAX_LEN = 512


def model_dir(model_name: str | None = None) -> Path:
    name = (model_name or config.EMBED_MODEL).replace("/", "__")
    return config.HOME / "models" / name


def is_downloaded(model_name: str | None = None) -> bool:
    d = model_dir(model_name)
    return all((d / fname).exists() for fname, _ in FILES)


def download(model_name: str | None = None, progress=None) -> Path:
    """모델을 사용자 폴더로 내려받는다. 이미 있으면 건너뛴다."""
    repo = model_name or config.EMBED_MODEL
    target = model_dir(repo)
    target.mkdir(parents=True, exist_ok=True)

    for fname, path in FILES:
        dest = target / fname
        if dest.exists() and dest.stat().st_size > 0:
            continue
        url = HF_BASE.format(repo=repo, path=path)
        tmp = dest.with_suffix(dest.suffix + ".part")
        log.info("모델 내려받는 중: %s", url)
        _fetch(url, tmp, progress, fname)
        tmp.replace(dest)  # 중간에 끊겨도 반쪽 파일이 남지 않게
    return target


def _fetch(url: str, dest: Path, progress, label: str) -> None:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as out:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(label, done, total)


class OnnxEmbedder:
    """e5 계열 문장 임베더. mean pooling + L2 정규화."""

    def __init__(self, model_name: str | None = None):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.name = model_name or config.EMBED_MODEL
        d = model_dir(self.name)
        if not is_downloaded(self.name):
            raise FileNotFoundError(f"모델이 없습니다: {d}")

        self._tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self._tok.enable_truncation(max_length=MAX_LEN)
        self._tok.enable_padding()

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # exe 안에서 스레드를 과하게 잡으면 오히려 느려진다.
        opts.intra_op_num_threads = min(4, (os_cpu() or 4))
        self._sess = ort.InferenceSession(str(d / "model.onnx"), opts, providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._sess.get_inputs()}
        self.dim = int(self._sess.get_outputs()[0].shape[-1])

    def encode(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if config.EMBED_USE_E5_PREFIX:
            prefix = "query: " if is_query else "passage: "
            texts = [prefix + t for t in texts]

        out = []
        for start in range(0, len(texts), 16):
            out.append(self._encode_batch(texts[start : start + 16]))
        return np.vstack(out)

    def _encode_batch(self, batch: list[str]) -> np.ndarray:
        encoded = self._tok.encode_batch(batch)
        ids = np.array([e.ids for e in encoded], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)

        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        feed = {k: v for k, v in feed.items() if k in self._inputs}

        hidden = self._sess.run(None, feed)[0]  # (batch, seq, dim)

        # mean pooling — 패딩 토큰은 빼고 평균낸다. 이걸 빼먹으면
        # sentence-transformers 결과와 미묘하게 달라져 검색 품질이 조용히 나빠진다.
        m = mask[..., None].astype(np.float32)
        summed = (hidden * m).sum(axis=1)
        counts = np.clip(m.sum(axis=1), 1e-9, None)
        vecs = summed / counts

        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (vecs / norms).astype(np.float32)


def os_cpu() -> int:
    import os

    return os.cpu_count() or 4


def available() -> bool:
    """onnxruntime 과 tokenizers 가 있는지."""
    try:
        import onnxruntime  # noqa: F401
        from tokenizers import Tokenizer  # noqa: F401
    except ImportError:
        return False
    return True


_download_lock = threading.Lock()


def ensure_and_load(model_name: str | None = None, progress=None) -> OnnxEmbedder:
    with _download_lock:
        if not is_downloaded(model_name):
            download(model_name, progress)
    return OnnxEmbedder(model_name)
