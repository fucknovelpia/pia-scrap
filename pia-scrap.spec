# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for PIA Scrap (Novelpia Global downloader)

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
    version='version_info.txt',
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
