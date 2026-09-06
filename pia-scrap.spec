# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for PIA Scrap (Novelpia Global downloader)

from pathlib import Path
from runpy import run_path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    load_version_info_from_text_file,
)

# Use the same version as the CLI and UI for the executable's Windows metadata.
spec_dir = Path(SPECPATH)
app_version = run_path(str(spec_dir / 'src' / '__init__.py'))['__version__']
windows_version = tuple(int(part) for part in app_version.split('.')) + (0,)
version_info = load_version_info_from_text_file(str(spec_dir / 'version_info.txt'))
version_info.ffi = FixedFileInfo(filevers=windows_version, prodvers=windows_version)
version_strings = {'FileVersion': '.'.join(map(str, windows_version)), 'ProductVersion': app_version}
for info in version_info.kids:
    if isinstance(info, StringFileInfo):
        for table in info.kids:
            for entry in table.kids:
                if entry.name in version_strings:
                    entry.val = version_strings[entry.name]

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'src',
        'src.api',
        'src.advertisements',
        'src.ad_viewer',
        'src.ad_navigation',
        'src.ad_continue',
        'src.builder',
        'src.chrome_session',
        'src.const',
        'src.epub',
        'src.helper',
        'src.images',
        'src.novel',
        'src.scraper',
        'src.ui',
        'src.webview_login',
        # GUI
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        # Core deps
        'requests',
        'bs4',
        'ebooklib',
        'ebooklib.epub',
        'dotenv',
        'lxml',
        # Webview login
        'webview',
        'clr_loader',
        'pythonnet',
        # Stdlib
        'concurrent.futures',
        'html.parser',
        'json',
        'base64',
        'uuid',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PIA-Scrap',
    version=version_info,
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
)
