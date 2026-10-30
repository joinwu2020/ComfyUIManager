# -*- coding: utf-8 -*-
"""
環境修復：Python 3.12 / CUDA 13.0 / PyTorch 2.13.0+cu130

用法（在 ComfyUI 根目錄，已啟動 venv）：
    python repair_env.py
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
os.chdir(ROOT)
PIP = [sys.executable, "-m", "pip"]

TORCH_PINS = {"torch": "2.13.0", "torchvision": "0.28.0", "torchaudio": "2.11.0"}
TORCH_INDEX = "https://download.pytorch.org/whl/cu130"
BLOCKED = ["tensorflow", "tensorflow-intel", "tensorboard", "liger-kernel"]


# 無 console 模式（pythonw / console=False 的 EXE）下，子程序沒帶這個旗標
# 就會被 Windows 另外配一個 console —— 畫面上就是一閃而過的黑窗。
# 節點掃描會對每個節點跑一次 git，四十個節點就是四十個黑窗連閃。
NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def sh(cmd, quiet=False):
    print(f"  > {' '.join(str(c) for c in cmd[2:] if c != '-q')}")
    return subprocess.run(cmd, capture_output=quiet, text=True, encoding="utf-8", creationflags=NOWIN)


def ver(pkg):
    r = subprocess.run(
        [sys.executable, "-c",
         f"from importlib.metadata import version; print(version({pkg!r}))"],
        capture_output=True, text=True, encoding="utf-8")
    return r.stdout.strip() if r.returncode == 0 else None


def step(n, total, title):
    print()
    print("=" * 54)
    print(f"  [{n}/{total}] {title}")
    print("=" * 54)


step(1, 3, "移除會破壞環境的套件")
hits = [p for p in BLOCKED if ver(p)]
if hits:
    print(f"  發現：{', '.join(hits)}")
    sh(PIP + ["uninstall"] + hits + ["-y"])
else:
    print("  沒有需要移除的套件")


step(2, 3, "還原 PyTorch cu130 鎖定版本")
need = [f"{p}=={v}" for p, v in TORCH_PINS.items()]
cur = {p: ver(p) for p in TORCH_PINS}
print("  目前：" + "  ".join(f"{p}={cur[p] or '未安裝'}" for p in TORCH_PINS))
ok = all(cur[p] and cur[p].split("+")[0] == v and "cu130" in cur[p]
         for p, v in TORCH_PINS.items())
if ok:
    print("  版本正確，略過")
else:
    sh(PIP + ["uninstall"] + list(TORCH_PINS) + ["-y"])
    sh(PIP + ["install"] + need + ["--index-url", TORCH_INDEX])


step(3, 3, "重新安裝 wheels/ 自訂 wheel")
wdir = os.path.join(ROOT, "wheels")
whls = sorted(f for f in os.listdir(wdir)) if os.path.isdir(wdir) else []
whls = [f for f in whls if f.lower().endswith(".whl")]
if not whls:
    print(f"  {wdir} 底下沒有 .whl，略過")
for f in whls:
    print(f"  {f}")
    sh(PIP + ["install", "--force-reinstall", "--no-deps",
              os.path.join(wdir, f), "-q"], quiet=True)


print()
print("=" * 54)
print("  驗證")
print("=" * 54)
checks = [
    ("torch",        "import torch; print(torch.__version__)"),
    ("numpy",        "import numpy; print(numpy.__version__)"),
    ("transformers", "import transformers; print(transformers.__version__)"),
    ("comfy_kitchen", "import comfy_kitchen as m; "
                      "print('sol_attn' if hasattr(m,'sol_attn') else 'no sol_attn')"),
    ("sageattention", "from importlib.metadata import version; "
                      "print(version('sageattention'))"),
]
for name, code in checks:
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True, encoding="utf-8", creationflags=NOWIN)
    if r.returncode == 0:
        print(f"  [OK]   {name:14} {r.stdout.strip()}")
    else:
        last = (r.stderr or "").strip().splitlines()
        print(f"  [FAIL] {name:14} {last[-1] if last else '匯入失敗'}")

for p in BLOCKED:
    v = ver(p)
    print(f"  [{'FAIL' if v else 'OK'}]   {p:14} "
          f"{'仍存在 ' + v if v else '已移除'}")

print()
input("按 Enter 結束…")
