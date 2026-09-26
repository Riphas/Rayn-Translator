# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec для portable-сборки «Ёлочка Плюс».

Сборка:  pyinstaller yolochka.spec
Результат: dist/YolochkaPlus.exe (onefile) + locales/ рядом с exe (onedir-стиль
для тяжёлых OCR-моделей не ломает portable-режим: БД и app.log пишутся
в папку exe, см. _app_dir() в main.py/database.py).
"""

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("locales", "locales")],          # JSON-локали едут внутри EXE
    hiddenimports=[
        "deep_translator",                   # динамические импорты внутри функций
        "easyocr",
        "pytesseract",
        "PIL",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="YolochkaPlus",
    debug=False,
    strip=False,
    upx=True,
    console=False,                           # без окна консоли — живём в трее
    icon=None,                               # иконка генерируется кодом (make_tray_icon)
)
