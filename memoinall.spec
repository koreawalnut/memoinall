# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 스펙 — 단일 exe.

크기가 전부인 파일이다. 아무 생각 없이 묶으면 torch(496MB) + transformers(94MB) +
CUDA 스텁까지 딸려와 1GB 를 넘고, onefile 은 실행할 때마다 그걸 임시폴더에 풀기 때문에
시작이 수십 초 걸린다.

그래서:
  - torch / transformers / sentence-transformers 를 **명시적으로 제외**하고
    onnxruntime + tokenizers 로 임베딩을 돌린다(벡터는 완전히 동일함을 확인).
  - 임베딩 모델 가중치(약 450MB)는 exe 에 넣지 않고 첫 실행 때 받아
    사용자 폴더(~/.memoinall/models)에 둔다. 재설치해도 다시 받지 않는다.
  - anthropic/openai 는 있으면 넣고 없으면 건너뛴다(선택 기능).

빌드:  python build_exe.py
"""

import importlib.util
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

block_cipher = None


def has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "memoinall.providers.anthropic_provider",
    "memoinall.providers.openai_compat",
]
datas = [("memoinall/static", "memoinall/static")]
binaries = []

# pywebview 는 파이썬 코드만으로 돌지 않는다. 창을 띄우는 데 필요한
#   webview/lib/*.dll            (Microsoft.Web.WebView2.Core / WinForms)
#   webview/lib/runtimes/.../WebView2Loader.dll
#   webview/js/*.js              (window.pywebview.api 를 만드는 스크립트)
# 를 데이터로 들고 있어서, submodules 만 모으면 exe 가 조용히 창을 못 띄운다.
# (실제로 이걸 빠뜨려 첫 빌드가 창 없이 떴다.)
for pkg in ("webview", "clr_loader", "pythonnet"):
    if has(pkg):
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hidden

hiddenimports += ["clr", "webview.platforms.edgechromium", "webview.platforms.winforms"]

# onnxruntime 은 네이티브 DLL 을 데이터로 들고 있어 수집이 필요하다.
if has("onnxruntime"):
    datas += collect_data_files("onnxruntime")
    hiddenimports += ["onnxruntime", "onnxruntime.capi._pybind_state"]

for optional in ("anthropic", "openai", "tokenizers"):
    if has(optional):
        hiddenimports.append(optional)
        if optional != "tokenizers":  # tokenizers 는 순수 확장모듈이라 데이터가 없다
            datas += collect_data_files(optional)

# 넣으면 크기만 커지고 쓰지 않는 것들.
excludes = [
    "torch", "torchvision", "torchaudio",
    "transformers", "sentence_transformers",
    "scipy", "sklearn", "pandas", "matplotlib", "PIL",
    "chromadb", "IPython", "notebook", "jupyter",
    "tkinter", "test", "unittest", "pydoc_data",
]

a = Analysis(
    ["desktop_main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="memoinall",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 는 백신 오탐을 크게 늘린다. 크기보다 그게 더 아프다.
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # 콘솔 창 없이 뜨는 진짜 데스크톱 앱
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
