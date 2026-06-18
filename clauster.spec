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
"""

a = Analysis(
    ["pyinstaller_entry.py"],
    pathex=["src"],
    binaries=[],
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
    ],
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
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
