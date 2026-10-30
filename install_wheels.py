# -*- coding: utf-8 -*-
"""
安裝 wheels/ 底下所有 .whl（--force-reinstall --no-deps）

用法（在 ComfyUI 根目錄，已啟動 venv）：
    python install_wheels.py
"""
import os
import subprocess
import sys


# 路徑一律以 comfy_env.py 為準（那裡可以用 COMFY_ROOT 把本體指到別處）。
# 讀不到就退回「腳本自己的目錄」，維持舊行為。
TOOL_DIR = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, "frozen", False) else __file__))
sys.path.insert(0, TOOL_DIR)
try:
    import comfy_env as _E
    ROOT = _E.ROOT
except Exception:
    _E = None
    ROOT = TOOL_DIR
WDIR = _E.WHEELS_DIR if _E else os.path.join(ROOT, "wheels")
PIP = [sys.executable, "-m", "pip"]
# 無 console 模式下子程序會自己開黑窗，一律帶這個旗標
NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


if not os.path.isdir(WDIR):
    sys.exit(f"找不到資料夾：{WDIR}")

whls = sorted(f for f in os.listdir(WDIR) if f.lower().endswith(".whl"))
if not whls:
    sys.exit(f"{WDIR} 底下沒有 .whl")

print("=" * 54)
print(f"  安裝 {len(whls)} 個 wheel")
print("=" * 54)
for f in whls:
    print(f"\n[INFO] {f}")
    subprocess.run(PIP + ["install", "--force-reinstall", "--no-deps",
                          os.path.join(WDIR, f)], creationflags=NOWIN)

print()
print("=" * 54)
print("  驗證")
print("=" * 54)
checks = [
    ("comfy_kitchen", "import comfy_kitchen as m; "
                      "print('sol_attn' if hasattr(m,'sol_attn') else 'no sol_attn')"),
    ("flash_attn",    "import flash_attn; print(flash_attn.__version__)"),
    ("sageattention", "from importlib.metadata import version; "
                      "print(version('sageattention'))"),
]
for name, code in checks:
    r = subprocess.run([sys.executable, "-c", code],
                       creationflags=NOWIN,
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode == 0:
        print(f"  [OK]   {name:14} {r.stdout.strip()}")
    else:
        last = (r.stderr or "").strip().splitlines()
        print(f"  [FAIL] {name:14} {last[-1] if last else '匯入失敗'}")

print()
input("按 Enter 結束…")
