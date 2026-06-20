# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Shorts Editor.

Produces dist/ShortsEditor/ShortsEditor.exe plus a folder of dependencies.
Inno Setup (installer.iss) wraps that into ShortsEditor-Setup.exe.
"""

from pathlib import Path

from PyInstaller.utils.hooks import (collect_data_files, collect_dynamic_libs,
                                     collect_submodules)

block_cipher = None
ROOT = Path('.').resolve()

datas = [
    ('gui/styles.qss', 'gui'),
    ('profiles/style_profile.json', 'profiles'),
]

binaries = []

vendor_ffmpeg = ROOT / 'vendor' / 'ffmpeg'
for tool in ('ffmpeg.exe', 'ffprobe.exe'):
    candidate = vendor_ffmpeg / tool
    if candidate.exists():
        binaries.append((str(candidate), 'vendor/ffmpeg'))

# Pull in the C++ runtime + model loader bits from faster-whisper/ctranslate2.
try:
    binaries += collect_dynamic_libs('ctranslate2')
except Exception:
    pass

# faster-whisper ships tokenizer/model assets next to its package.
try:
    datas += collect_data_files('faster_whisper')
except Exception:
    pass

hiddenimports = [
    'src.main', 'src.plan', 'src.transcribe', 'src.matcher',
    'src.vision', 'src.capcut_draft', 'src.viewer', 'src.reference',
    'src.config',
    'gui.app', 'gui.main_window', 'gui.config_store', 'gui.worker',
    'gui.pages.setup_page', 'gui.pages.progress_page',
    'gui.pages.review_page', 'gui.pages.settings_page',
]
try:
    hiddenimports += collect_submodules('faster_whisper')
except Exception:
    pass

a = Analysis(
    ['gui/app.py'],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ShortsEditor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ShortsEditor',
)
