# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for comfy_manager.py

這份 spec 的每個選項都是為了「少被防毒誤判」而選的：

  onedir（不是 onefile）
      onefile 執行時會把自己解壓到 %TEMP% 再跑，那正是惡意程式的典型行為，
      啟發式掃描最討厭這個。onedir 沒有這個動作，誤判率低很多。
  upx=False
      壓縮過的執行檔是防毒的頭號特徵之一。不要壓。
  console=False
      GUI 程式不需要黑窗。
  version 資源
      有公司名、產品名、描述的執行檔比完全空白的可疑度低。
  excludes
      管理介面只用到標準庫 + tkinter，完全不需要 torch / numpy / PIL。
      排除掉之後大小從幾百 MB 降到十幾 MB，也少掉一堆 DLL 誤判來源。

建置：
    build_exe.bat
或
    pyinstaller --clean --noconfirm comfy_manager.spec
"""
import os

# ── 版本資源（讓執行檔有正常的檔案屬性）──
VER = (1, 0, 0, 0)
COMPANY = "TYWu"
PRODUCT = "ComfyUI Manager"
DESC = "ComfyUI environment manager"
VERSION_FILE = "version_info.txt"

with open(VERSION_FILE, "w", encoding="utf-8") as f:
    f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={VER}, prodvers={VER},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040404b0', [
        StringStruct('CompanyName', '{COMPANY}'),
        StringStruct('FileDescription', '{DESC}'),
        StringStruct('FileVersion', '{".".join(map(str, VER))}'),
        StringStruct('InternalName', 'comfy_manager'),
        StringStruct('OriginalFilename', 'ComfyManager.exe'),
        StringStruct('ProductName', '{PRODUCT}'),
        StringStruct('ProductVersion', '{".".join(map(str, VER))}')])]),
    VarFileInfo([VarStruct('Translation', [1028, 1200])])
  ]
)
""")

# ── 圖示 ──
# 注意：給了 .spec 之後就不能在命令列加 --icon / -i，PyInstaller 會直接拒絕
#       （"makespec options not valid when a .spec file is given"）。
#       圖示一律在這裡決定。
# 順序：環境變數 COMFY_ICON → 下列檔名依序找第一個存在的。
ICON = os.environ.get("COMFY_ICON") or None
if not ICON or not os.path.exists(ICON):
    for _c in ("comfyui.ico", "manager.ico", "icon.ico", "app.ico"):
        if os.path.exists(_c):
            ICON = _c
            break
    else:
        ICON = None
print(f"[spec] 圖示：{ICON or '（沒找到 .ico，使用預設圖示）'}")

a = Analysis(
    ["comfy_manager.py"],
    pathex=[],
    binaries=[],
    datas=[],
    # 這三個是用 try/except ImportError 匯入的，PyInstaller 不一定掃得到
    hiddenimports=["comfy_config_io", "comfy_nodes_backup", "comfy_sys"],
    hookspath=[],
    runtime_hooks=[],
    # 管理介面本身不需要這些重量級套件（它是用 subprocess 叫 venv 的 Python 跑）
    excludes=[
        "torch", "torchvision", "torchaudio", "numpy", "scipy", "PIL",
        "Pillow", "cv2", "matplotlib", "pandas", "transformers",
        "safetensors", "IPython", "pytest", "setuptools", "pip",
        "test", "unittest", "pydoc", "doctest",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ComfyManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # ← 不要壓縮，這是誤判主因
    console=False,          # ← GUI，不開黑窗
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version=VERSION_FILE,
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ComfyManager",    # → dist\ComfyManager\ComfyManager.exe
)
