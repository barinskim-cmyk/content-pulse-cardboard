# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec для Cardboard (macOS .app / Windows .exe).
# Сборка: python3 -m PyInstaller Cardboard.spec --noconfirm
# Иконка: когда появится design/brand-cardboard/icon.icns — вписать в BUNDLE (icon=...).

import os
import sys

# Иконка: в maket-cp лежит в design/, в публичном репо — рядом со spec
ICON = 'icon.icns' if os.path.exists('icon.icns') else '../design/brand-cardboard/icon.icns'

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('index.html', '.')],   # frontend внутрь бандла (см. find_frontend)
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Cardboard',
    debug=False,
    strip=False,
    upx=False,
    console=False,          # windowed: без терминала
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='Cardboard',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='Cardboard.app',
        icon=ICON,   # знак cb-grid на тайле #131313
        bundle_identifier='pulse.content.cardboard',
        info_plist={
            'CFBundleName': 'Cardboard',
            'CFBundleDisplayName': 'Cardboard',
            'CFBundleShortVersionString': '1.1.0',
            'NSHighResolutionCapable': True,
            # TCC: без usage-описаний macOS МОЛЧА запрещает чтение
            # защищённых папок (Загрузки/Документы/Рабочий стол) при
            # открытии проекта без диалога — «файл не найден» на всё.
            'NSDownloadsFolderUsageDescription':
                'Cardboard читает фото съёмки по путям из файла проекта.',
            'NSDocumentsFolderUsageDescription':
                'Cardboard читает фото съёмки по путям из файла проекта.',
            'NSDesktopFolderUsageDescription':
                'Cardboard читает фото съёмки по путям из файла проекта.',
            'NSRemovableVolumesUsageDescription':
                'Cardboard читает фото съёмки с внешних дисков по путям из файла проекта.',
        },
    )
