# -*- coding: utf-8 -*-
"""
程序控制與 GPU 查詢 —— comfy_manager.py 用，也可以單獨當 CLI

    python comfy_sys.py gpu        看一次 GPU 狀態
    python comfy_sys.py find       找出正在跑的 ComfyUI
    python comfy_sys.py kill       終止所有 ComfyUI
"""
import os
import re
import socket
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

NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=NOWIN)
    except (OSError, subprocess.SubprocessError):
        class R:
            returncode, stdout, stderr = 1, "", ""
        return R()


# ══════════════════════════ 程序 ══════════════════════════

def kill_tree(pid):
    """連子程序一起終止。ComfyUI 會開 worker，只殺父程序會留下孤兒佔著 VRAM。"""
    if not pid:
        return False
    if os.name == "nt":
        r = _run(["taskkill", "/F", "/T", "/PID", str(pid)], timeout=25)
        return r.returncode == 0
    ok = False
    for sig in ("-TERM", "-KILL"):
        r = _run(["pkill", sig, "-P", str(pid)])
        ok = ok or r.returncode == 0
        try:
            os.kill(int(pid), 15 if sig == "-TERM" else 9)
            ok = True
        except (OSError, ValueError):
            pass
        if sig == "-TERM":
            import time
            time.sleep(1.5)
            if not pid_alive(pid):
                return True
    return ok


def pid_alive(pid):
    if os.name == "nt":
        r = _run(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
        return str(pid) in r.stdout
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def find_comfy(root=None):
    """找出正在跑 main.py 的 Python 程序 → [(pid, 指令列)]

    只認指令列裡同時有 python 和 main.py 的，並盡量比對是不是這個資料夾。
    """
    root = os.path.normcase(os.path.abspath(root or ROOT))
    found = []
    if os.name == "nt":
        ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
              "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        r = _run(["powershell", "-NoProfile", "-Command", ps], timeout=30)
        lines = r.stdout.splitlines()
    else:
        r = _run(["ps", "-eo", "pid=,args="])
        lines = [l.strip().replace(" ", "\t", 1) for l in r.stdout.splitlines()]
    for line in lines:
        if "\t" not in line:
            continue
        pid, cmd = line.split("\t", 1)
        pid, cmd = pid.strip(), cmd.strip()
        if not pid.isdigit() or "main.py" not in cmd:
            continue
        if "comfy_manager" in cmd or "comfy_sys" in cmd:
            continue
        # 指令列裡若出現其他 ComfyUI 的路徑就不是這一份
        low = os.path.normcase(cmd)
        if root not in low and os.path.normcase(os.path.basename(root)) not in low:
            # 相對路徑啟動時指令列只有 "main.py"，無法分辨，仍然列出
            if not re.search(r"(^|[\s\"'])main\.py", cmd):
                continue
        found.append((int(pid), cmd))
    return found


def port_open(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(0.6)
    try:
        return s.connect_ex((host, int(port))) == 0
    finally:
        s.close()


def port_from_args(args, default=8188):
    args = list(args or [])
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            try:
                return int(args[i + 1])
            except ValueError:
                pass
        if a.startswith("--port="):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                pass
    return default


# ══════════════════════════ GPU ══════════════════════════

GPU_FIELDS = ["index", "name", "utilization.gpu", "memory.used", "memory.total",
              "temperature.gpu", "power.draw", "power.limit", "fan.speed",
              "clocks.sm", "utilization.memory"]


def has_nvidia_smi():
    return _run(["nvidia-smi", "--version"], timeout=10).returncode == 0


def gpus():
    """→ [dict]。拿不到就回空 list。"""
    r = _run(["nvidia-smi", f"--query-gpu={','.join(GPU_FIELDS)}",
              "--format=csv,noheader,nounits"], timeout=20)
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(GPU_FIELDS):
            continue
        d = dict(zip(GPU_FIELDS, parts))

        def num(k, cast=float):
            try:
                return cast(d[k])
            except (ValueError, KeyError):
                return None
        out.append(dict(
            index=d["index"], name=d["name"],
            util=num("utilization.gpu", int), mem_util=num("utilization.memory", int),
            used=num("memory.used", int), total=num("memory.total", int),
            temp=num("temperature.gpu", int), power=num("power.draw"),
            plimit=num("power.limit"), fan=num("fan.speed", int),
            clock=num("clocks.sm", int)))
    return out


def gpu_procs():
    """占用 VRAM 的程序 → [(pid, 名稱, MB)]"""
    r = _run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
              "--format=csv,noheader,nounits"], timeout=20)
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            out.append((int(parts[0]), parts[1], int(parts[2])))
        except ValueError:
            continue
    return out


def _cli():
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    if cmd == "gpu":
        if not has_nvidia_smi():
            print("找不到 nvidia-smi")
            return 1
        for g in gpus():
            pct = (g["used"] / g["total"] * 100) if g["total"] else 0
            print(f"GPU {g['index']}  {g['name']}")
            print(f"  使用率 {g['util']}%　VRAM {g['used']}/{g['total']} MB"
                  f"（{pct:.0f}%）　{g['temp']}°C　{g['power']}/{g['plimit']} W")
        for pid, name, mb in gpu_procs():
            print(f"  PID {pid:>7}  {mb:>6} MB  {name}")
    elif cmd == "find":
        f = find_comfy()
        print("沒有找到執行中的 ComfyUI" if not f else "")
        for pid, c in f:
            print(f"  PID {pid}  {c[:140]}")
        print(f"  連接埠 8188：{'開著' if port_open(8188) else '關閉'}")
    elif cmd == "kill":
        f = find_comfy()
        if not f:
            print("沒有要終止的程序")
        for pid, c in f:
            print(f"終止 PID {pid} … {'成功' if kill_tree(pid) else '失敗'}")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
