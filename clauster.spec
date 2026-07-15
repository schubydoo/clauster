# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for a standalone one-file `clauster` binary.

Build in CI / a full environment (NOT the dev sandbox, which lacks runtime libs):

    uv pip install -e '.[package]'
    pyinstaller clauster.spec
    # -> dist/clauster

The analyzed script is ``pyinstaller_entry.py`` (an absolute-import shim), NOT
``clauster/__main__.py``: PyInstaller runs the entry as top-level ``__main__`` with
no package context, so ``__main__``'s relative imports would fail at runtime.
Bundles the Jinja templates and static assets as data so the binary is self-contained,
and pins uvicorn's dynamically-imported submodules as hidden imports.

Also bundles ``pip`` (via ``collect_all``) so the frozen binary can drive
``clauster deps install <extra>`` (#904 slice 2b): the managed side-install runs pip
in-process through ``pip._internal.cli.main`` — PyInstaller collects pip's submodules +
vendored data but not the dynamically imported ``pip.__main__``, hence the private entry.
Costs ~2 MB; verified end-to-end by the 2026-07-14 spike.
"""

from PyInstaller.utils.hooks import collect_all

_pip_datas, _pip_binaries, _pip_hiddenimports = collect_all("pip")

a = Analysis(
    ["pyinstaller_entry.py"],
    pathex=["src"],
    binaries=_pip_binaries,
    datas=[
        ("src/clauster/templates", "clauster/templates"),
        ("src/clauster/static", "clauster/static"),
        # Alembic loads the migration env + revision scripts from the filesystem at
        # startup (bootstrap.upgrade_to_head), so the whole migrations tree and the
        # alembic.ini must ship as data — they are not importable modules Analysis
        # would otherwise pick up. Mirror the in-package layout so
        # ``Path(__file__).parent / "migrations"`` resolves under _MEIPASS.
        ("src/clauster/db/migrations", "clauster/db/migrations"),
        ("src/clauster/db/alembic.ini", "clauster/db"),
        *_pip_datas,
    ],
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        *_pip_hiddenimports,
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="clauster",
    debug=False,
    strip=False,
    upx=True,
    console=True,
    onefile=True,
)
