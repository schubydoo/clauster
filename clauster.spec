# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for a standalone one-file `clauster` binary.

Build in CI / a full environment (NOT the dev sandbox, which lacks runtime libs):

    uv pip install -e '.[package]'
    pyinstaller clauster.spec
    # -> dist/clauster

Bundles the Jinja templates and static assets as data so the binary is self-contained,
and pins uvicorn's dynamically-imported submodules as hidden imports.
"""

a = Analysis(
    ["src/clauster/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=[
        ("src/clauster/templates", "clauster/templates"),
        ("src/clauster/static", "clauster/static"),
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
