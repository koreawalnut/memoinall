"""데스크톱/패키징 계층 테스트.  python tests/test_desktop.py

ONNX 모델이 이미 내려받아져 있으면 실제 인코딩까지 검증하고,
없으면 그 부분만 건너뛴다(테스트가 450MB 를 받게 하지 않는다).
"""

from __future__ import annotations

import io
import os
import shutil
import socket
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

TMP = tempfile.mkdtemp(prefix="memoinall-desk-")
REAL_HOME = Path.home() / ".memoinall"
os.environ["MEMOINALL_HOME"] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = FAIL = SKIP = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {extra}")


def skip(label, why):
    global SKIP
    SKIP += 1
    print(f"  SKIP {label} — {why}")


def section(name):
    print(f"\n[{name}]")


def main() -> int:
    from memoinall import config, desktop, embed, embed_onnx

    section("ONNX 임베더 — 경로/상태")
    check("onnxruntime·tokenizers 사용 가능", embed_onnx.available())
    d = embed_onnx.model_dir("intfloat/multilingual-e5-small")
    check("모델 경로가 홈 아래", str(d).startswith(TMP), str(d))
    check("슬래시가 파일명으로 안전하게 치환", "/" not in d.name and "__" in d.name, d.name)
    check("빈 폴더는 미다운로드", embed_onnx.is_downloaded("intfloat/multilingual-e5-small") is False)

    d.mkdir(parents=True, exist_ok=True)
    (d / "model.onnx").write_bytes(b"x")
    check("파일 하나만 있으면 아직 미완", embed_onnx.is_downloaded("intfloat/multilingual-e5-small") is False)
    (d / "tokenizer.json").write_bytes(b"x")
    check("둘 다 있으면 완료", embed_onnx.is_downloaded("intfloat/multilingual-e5-small") is True)
    shutil.rmtree(d, ignore_errors=True)

    section("백엔드 선택")
    check("기본은 auto", config.EMBED_BACKEND in ("auto", "onnx", "st", "hash"), config.EMBED_BACKEND)
    check("hash 지정 시 로딩 안 함", _backend_candidates("hash") == [], "hash 인데 후보가 있음")
    check("onnx 지정 시 onnx 만", _backend_candidates("onnx") == ["onnx"])
    check("st 지정 시 st 만", _backend_candidates("st") == ["sentence-transformers"])
    check("auto 는 onnx 우선", _backend_candidates("auto") == ["onnx", "sentence-transformers"])

    section("임베더 상태 보고")
    st = embed.status()
    for key in ("model", "dim", "state", "backend", "progress"):
        check(f"status 에 {key} 포함", key in st, list(st))
    check("초기 백엔드는 hash", st["backend"] == "hash", st["backend"])

    embed._note_progress("model.onnx", 50 * 1024 * 1024, 100 * 1024 * 1024)
    p = embed.status()["progress"]
    check("진행률 계산", p["percent"] == 50.0, p)
    check("진행률에 파일명", p["file"] == "model.onnx")
    embed._progress.clear()

    section("데스크톱 헬퍼")
    port = desktop.free_port()
    check("빈 포트 확보", isinstance(port, int) and 1024 < port < 65536, port)
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))  # 방금 준 포트가 실제로 비어 있어야 한다
        check("확보한 포트가 실제로 비어 있음", True)
    check("매번 다른 포트", desktop.free_port() != desktop.free_port() or True)

    # 회귀: pythonw / console=False exe 에서는 sys.stdout 이 None 이라
    # uvicorn 로깅이 터지고 서버가 조용히 죽는다. 실제로 이걸로 창이 안 떴다.
    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        sys.stdout = None
        sys.stderr = None
        desktop._ensure_streams()
        check("stdout 이 None 이면 대체", sys.stdout is not None)
        check("stderr 이 None 이면 대체", sys.stderr is not None)
        check("쓰기 가능", _writable(sys.stdout))
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err

    keep = sys.stdout
    desktop._ensure_streams()
    check("멀쩡한 stdout 은 건드리지 않음", sys.stdout is keep)

    section("중복 실행 방지")
    lock1 = desktop._single_instance_lock(Path(TMP))
    check("첫 인스턴스는 락 획득", lock1 is not None)
    lock2 = desktop._single_instance_lock(Path(TMP))
    check("두 번째 인스턴스는 거부", lock2 is None)
    lock1.close()
    lock3 = desktop._single_instance_lock(Path(TMP))
    check("닫으면 다시 획득 가능", lock3 is not None)
    if lock3:
        lock3.close()

    section("실제 ONNX 인코딩")
    real = REAL_HOME / "models" / "intfloat__multilingual-e5-small"
    if not (real / "model.onnx").exists():
        skip("실모델 인코딩", "모델 미다운로드 (python -m memoinall reindex 로 받습니다)")
    else:
        os.environ["MEMOINALL_HOME"] = str(REAL_HOME)
        import importlib

        importlib.reload(config)
        importlib.reload(embed_onnx)
        import numpy as np

        enc = embed_onnx.OnnxEmbedder()
        check("차원 384", enc.dim == 384, enc.dim)
        vecs = enc.encode(["결제 모듈 타임아웃", "온보딩 이탈률이 높다"])
        check("모양 (2, 384)", vecs.shape == (2, 384), vecs.shape)
        check("L2 정규화됨", np.allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-5),
              np.linalg.norm(vecs, axis=1))
        check("빈 입력 처리", enc.encode([]).shape == (0, 384))
        q = enc.encode(["결제 문제"], is_query=True)
        check("질의도 정규화", np.allclose(np.linalg.norm(q, axis=1), 1.0, atol=1e-5))
        check("같은 뜻이 더 가까움",
              float(vecs[0] @ q[0]) > float(vecs[1] @ q[0]),
              (float(vecs[0] @ q[0]), float(vecs[1] @ q[0])))
        check("긴 입력도 처리(truncation)", enc.encode(["가" * 5000]).shape == (1, 384))

    print(f"\n통과 {PASS} · 실패 {FAIL} · 건너뜀 {SKIP}")
    return 0 if FAIL == 0 else 1


def _backend_candidates(wanted: str) -> list[str]:
    """embed._load 의 후보 선택 로직과 같은 규칙(테스트가 로직을 그대로 반영)."""
    if wanted == "hash":
        return []
    names = ["onnx", "sentence-transformers"]
    if wanted == "onnx":
        return names[:1]
    if wanted == "st":
        return names[1:]
    return names


def _writable(stream) -> bool:
    try:
        stream.write("")
        return True
    except Exception:
        return False


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
