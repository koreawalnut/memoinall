"""데스크톱 앱 진입점 (PyInstaller 가 이 파일을 묶는다).

개발 중에는 그냥 `python desktop_main.py` 로도 실행된다.
"""

from __future__ import annotations

import multiprocessing
import os
import sys


def main() -> int:
    # 얼린 실행파일에서 자식 프로세스가 앱을 다시 여는 것을 막는다.
    multiprocessing.freeze_support()

    # 패키징된 앱은 임베딩을 onnxruntime 으로 돌린다(torch 를 넣지 않았으므로).
    if getattr(sys, "frozen", False):
        os.environ.setdefault("MEMOINALL_EMBED_BACKEND", "onnx")

    from memoinall.desktop import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
