from pathlib import Path
import PySide6
PROJECT_DIR = Path(SPECPATH)
QT_DIR = Path(PySide6.__file__).parent
RUNTIMES = list(QT_DIR.glob('VCRUNTIME*.dll')) + list(QT_DIR.glob('MSVCP*.dll'))

# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    [str(PROJECT_DIR / 'main.py')],
    pathex=[],
    binaries=[(str(path), '.') for path in RUNTIMES],
    datas=[(str(PROJECT_DIR / 'assets'), 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# 使用Windows自带UCRT，避免构建环境PATH中的旧API shim污染打包。
a.binaries = [item for item in a.binaries if not Path(item[0]).name.lower().startswith('api-ms-win-') and Path(item[0]).name.lower() not in ('ucrtbase.dll','icuuc.dll','icudt78.dll','icuin78.dll')]
from PyInstaller.building.datastruct import normalize_toc
import PySide6
runtime_dir = Path(PySide6.__file__).parent
a.binaries = normalize_toc([(path.name, str(path), 'BINARY') for path in list(runtime_dir.glob('VCRUNTIME*.dll')) + list(runtime_dir.glob('MSVCP*.dll'))] + a.binaries)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ClipboardHistory',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(PROJECT_DIR / 'assets/icon.ico')],
)
