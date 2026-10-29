# PyInstaller spec for the packaged engine (one folder: livery-engine + _internal/).
# Run from the repository root inside the build container: pyinstaller packaging/livery-engine.spec
from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("uvicorn") + collect_submodules("livery")

a = Analysis(
    ["engine_entry.py"],
    pathex=["../engine"],
    datas=[
        ("../engine/filters", "filters"),
        ("../engine/livery/desktop/neutral", "livery/desktop/neutral"),
    ],
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "docx"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="livery-engine", console=True, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="engine")
