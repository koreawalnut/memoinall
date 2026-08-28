"""환경 설정. 모두 환경변수로 덮어쓸 수 있다."""

from __future__ import annotations

import os
from pathlib import Path


def _home() -> Path:
    raw = os.environ.get("MEMOINALL_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".memoinall"


HOME = _home()
DB_PATH = HOME / "memo.db"

# 임베딩 모델. 한국어 성능 대비 크기가 작은 multilingual-e5-small 을 기본으로 쓴다.
# 최초 1회 다운로드(약 450MB)가 필요하고, 그 전까지는 해시 임베더로 폴백한다.
EMBED_MODEL = os.environ.get("MEMOINALL_EMBED_MODEL", "intfloat/multilingual-e5-small")
EMBED_DIM_FALLBACK = 384

# e5 계열은 query:/passage: 접두어를 요구한다. 다른 모델을 쓰면 자동으로 끈다.
EMBED_USE_E5_PREFIX = "e5" in EMBED_MODEL.lower()

# 임베딩 모델을 아예 로드하지 않고 해시 임베더만 쓰고 싶을 때(테스트 등)
DISABLE_ST = os.environ.get("MEMOINALL_DISABLE_ST", "").lower() in {"1", "true", "yes"}

# 임베딩 백엔드: auto | onnx | st | hash
#   onnx = onnxruntime (torch 불필요, 가볍고 빠름 — 패키징 exe 의 기본)
#   st   = sentence-transformers (torch 필요)
# 두 백엔드는 같은 가중치라 **벡터가 완전히 동일**하다. 그래서 서로 바꿔 써도
# 이미 저장된 임베딩을 재색인할 필요가 없다.
EMBED_BACKEND = os.environ.get("MEMOINALL_EMBED_BACKEND", "auto").lower()

# 청킹 파라미터 (문자 기준)
CHUNK_TARGET = int(os.environ.get("MEMOINALL_CHUNK_TARGET", "420"))
CHUNK_MAX = int(os.environ.get("MEMOINALL_CHUNK_MAX", "700"))

# 검색 융합
RRF_K = 60

# LLM 설정은 settings.py 로 옮겼다 — 웹 UI 에서 바꾸고 DB 에 남아야 하기 때문에
# 프로세스 시작 시점에 고정되는 이 파일에 둘 수 없다.

HOST = os.environ.get("MEMOINALL_HOST", "127.0.0.1")
PORT = int(os.environ.get("MEMOINALL_PORT", "8787"))


def ensure_home() -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    return HOME
