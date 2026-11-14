# -*- mode: python ; coding: utf-8 -*-
"""
ChromaScore 打包配置
    pyinstaller chromascore.spec --noconfirm

librosa 1.x 用 lazy_loader 惰性导入子模块，PyInstaller 的静态分析抓不到，
所以这里显式 collect_submodules；matplotlib / sklearn / pandas 只在
librosa.display 等边缘功能里用，本项目不需要，排除掉能省一大截体积。
"""

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hidden = []
for pkg in ("librosa", "numba", "llvmlite", "mutagen", "soundfile", "audioread",
            "lazy_loader", "pooch", "soxr", "webview", "clr_loader"):
    try:
        hidden += collect_submodules(pkg)
    except Exception:
        pass

hidden += [
    "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
    "scipy.signal", "scipy.fft", "scipy.ndimage", "scipy.special",
    "numpy.fft", "numpy.linalg",
    # pywebview 的 Windows 后端（WebView2 via pythonnet）
    "clr", "webview.platforms.winforms", "webview.platforms.edgechromium",
]

datas = []
for pkg in ("librosa", "soundfile", "mutagen"):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass

a = Analysis(
    ["chromascore.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "matplotlib", "sklearn", "pandas", "PyQt5", "PySide2", "PySide6",
        "IPython", "notebook", "pytest", "tornado", "zmq", "sphinx",
        "jupyter", "nbformat", "notebook", "docutils",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ChromaScore",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon="icon.ico",
)
