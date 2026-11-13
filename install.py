# -*- coding: utf-8 -*-
"""
ComfyUI 全新安裝 / 重建工具

在 ComfyUI 根目錄用「系統的」Python 3.12 執行（不用先啟動 venv）：
    python install.py               建立（或沿用）venv，補齊所有東西
    python install.py --recreate    舊 venv 改名備份成 venv.bak_時間，完全重建
    python install.py --lock        另外依 env_state/pip_freeze.txt 還原到上次更新後的版本
    python install.py --skip-nodes  不 clone 缺少的自訂節點
    python install.py --overwrite-core   本體 reset --hard 到上游最新
    python install.py --recreate --venv-only
                                    只重建 venv，不裝套件也不碰本體

只要有這幾支腳本 + models/ user/ custom_nodes/ env_state/，其他清空也能重建：
本體原始碼不在的時候會自己 git clone 回來。

流程：
  0. 本體：缺原始碼就 git clone（用系統的 git，在建 venv 之前）
  1. 確認 Python 3.12（找不到會試 py -3.12），建立 venv，改用 venv 重新執行自己
  2. pip → PyTorch 鎖定版 → 核心鎖版（PIN_CORE）
  3. ComfyUI requirements → 額外套件（EXTRA_PACKAGES）
  4. 依 env_state/nodes.json clone 缺少的 git 節點（回到上次紀錄的 commit）
  5. 各節點 requirements（每個節點各自記帳）
  6. （--lock）pip_freeze.txt
  7. wheels/ 自訂 wheel（最後裝，才不會被覆蓋）
  8. 驗證 torch / CUDA / GPU / 自訂 wheel，輸出摘要

設定全部在 comfy_env.py；輸出同時寫入 update_logs/install_時間.log
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import comfy_env as E  # noqa: E402

# 無 console 模式下子程序會自己開黑窗，一律帶這個旗標
NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

PY_URL = "https://www.python.org/downloads/release/python-31210/"


def parse_args():
    ap = argparse.ArgumentParser(description="ComfyUI 安裝 / 重建")
    ap.add_argument("--recreate", action="store_true", help="備份舊 venv 後完全重建")
    ap.add_argument("--lock", action="store_true", help="依 pip_freeze.txt 還原版本")
    ap.add_argument("--skip-nodes", action="store_true", help="不 clone 缺少的節點")
    ap.add_argument("--venv-only", action="store_true",
                    help="只建立 venv，不裝任何套件（給「重建 venv」用）")
    ap.add_argument("--overwrite-core", action="store_true",
                    help="本體 reset --hard 到上游最新（丟掉所有本地改動）")
    ap.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    return ap.parse_args()


# ══════════ 本體：缺就 clone，要覆蓋就 reset ══════════
def ensure_core(args):
    """
    在 venv 之前先做完，用系統的 git。三種狀態：

      沒原始碼、沒 .git  → clone 進來（models/ user/ custom_nodes/ 不在版控裡，不動）
      有 .git            → 同步 origin 網址；--overwrite-core 才 reset --hard
      有原始碼沒 .git     → 無法更新，提示手動處理（不會亂動既有檔案）
    """
    E.header("ComfyUI 本體")
    if not E.git_available():
        sys.exit("[ERROR] 找不到 git，請先安裝：https://git-scm.com/download/win")

    if not os.path.isdir(E.ROOT):
        print(f"[INFO] 建立本體目錄：{E.ROOT}")
        os.makedirs(E.ROOT, exist_ok=True)

    if E.has_git_dir():
        changed, msg = E.sync_core_remote()
        print(f"[{'INFO' if changed else 'OK'}] {msg}")
        if not E.core_present():
            print("[WARN] 有 .git 但本體檔案不全，用上游重新取出")
            ok, msg = E.overwrite_core()
            print(f"[{'INFO' if ok else 'ERROR'}] {msg}")
            if not ok:
                sys.exit(1)
        elif args.overwrite_core:
            dirty = E.dirty_files()
            if dirty:
                print(f"[INFO] 丟掉 {len(dirty)} 個本地改動（--overwrite-core）")
            ok, msg = E.overwrite_core()
            print(f"[{'INFO' if ok else 'ERROR'}] {msg}")
            if not ok:
                sys.exit(1)
        else:
            print("[OK] 本體已存在（要覆蓋成上游最新：加 --overwrite-core）")
    elif E.core_present():
        print("[WARN] 有本體檔案但沒有 .git，這份無法用 git 更新。")
        print("       想轉成 git 管理：把本體檔案刪掉（保留 models/ user/")
        print("       custom_nodes/ env_state/ 和這些腳本），再跑一次 install.py")
    else:
        print("[INFO] 找不到本體原始碼，從上游取得")
        ok, msg = E.clone_core_into_root()
        print(f"[{'INFO' if ok else 'ERROR'}] {msg}")
        if not ok:
            sys.exit(1)

    core = E.core_info()
    print(f"[INFO] v{core['version']}  {core['short']}  {core['date']}  "
          f"{core['branch']}  {core['subject']}")


def venv_python():
    if os.name == "nt":
        return os.path.join(E.VENV_DIR, "Scripts", "python.exe")
    return os.path.join(E.VENV_DIR, "bin", "python")


def in_venv():
    return os.path.normcase(os.path.abspath(sys.prefix)) == \
        os.path.normcase(os.path.abspath(E.VENV_DIR))


def find_py312():
    want = "%d.%d" % E.PY_MAJOR_MINOR
    if sys.version_info[:2] == E.PY_MAJOR_MINOR:
        return sys.executable
    if shutil.which("py"):
        r = E.run(["py", f"-{want}", "-c", "import sys; print(sys.executable)"])
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def py_version(exe):
    r = E.run([exe, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"])
    return r.stdout.strip() if r.returncode == 0 else None


# ══════════ 外層：用系統 Python 準備 venv ══════════
def outer(args):
    want = "%d.%d" % E.PY_MAJOR_MINOR
    E.header("準備虛擬環境")
    if args.recreate and in_venv():
        sys.exit("[ERROR] --recreate 不能在 venv 內執行，請先 deactivate 再跑")

    py = find_py312()
    if not py:
        print(f"[ERROR] 找不到 Python {want}（目前 {sys.version.split()[0]}）")
        print(f"        下載：{PY_URL}")
        sys.exit(1)
    print(f"[INFO] 使用 {py}")

    if args.recreate and os.path.exists(E.VENV_DIR):
        bak = f"{E.VENV_DIR}.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}"
        os.rename(E.VENV_DIR, bak)
        print(f"[INFO] 舊 venv 已改名為 {os.path.basename(bak)}（確認沒問題後可自行刪除）")

    if not os.path.exists(venv_python()):
        print("[INFO] 建立 venv…")
        r = subprocess.run([py, "-m", "venv", E.VENV_DIR], creationflags=NOWIN)
        if r.returncode != 0 or not os.path.exists(venv_python()):
            sys.exit("[ERROR] 建立 venv 失敗")
    else:
        v = py_version(venv_python())
        if v != want:
            print(f"[ERROR] 現有 venv 是 Python {v}，需要 {want}；請加 --recreate 重建")
            sys.exit(1)
        print("[INFO] 沿用現有 venv")

    if args.venv_only:
        print()
        print("=" * 54)
        print("  venv 已建立")
        print("=" * 54)
        print(f"  位置：{E.VENV_DIR}")
        print(f"  解譯器：{venv_python()}")
        print()
        print("  這是空的環境，還沒有任何套件。")
        print("  接著按「補齊環境 install.py」把套件裝進去。")
        sys.exit(0)

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    argv = [a for a in sys.argv[1:] if a != "--recreate"] + ["--inner"]
    sys.exit(subprocess.run([venv_python(), os.path.abspath(__file__)] + argv,
                            cwd=ROOT, env=env,
                            creationflags=NOWIN).returncode)


# ══════════ 節點還原 ══════════
def restore_nodes():
    res = dict(cloned=[], present=[], manual=[], failed=[], self_made=[])
    if not os.path.exists(E.NODES_MANIFEST):
        print("[SKIP] 沒有 env_state/nodes.json（跑過一次 update.py 就會產生）")
        return res
    with open(E.NODES_MANIFEST, encoding="utf-8") as f:
        data = json.load(f)
    print(f"[INFO] 節點清單紀錄時間：{data.get('time')}")
    os.makedirs(E.NODES_DIR, exist_ok=True)
    for n in data.get("nodes", []):
        name, kind = n["name"], n["kind"]
        if kind == "disabled":
            continue
        path = os.path.join(E.NODES_DIR, name)
        if os.path.exists(path):
            res["present"].append(name)
            continue
        if kind == "excluded" and not n.get("url"):
            res["self_made"].append(name)
            continue
        if not n.get("url"):
            res["manual"].append(n)
            continue
        r = E.run(["git", "clone", n["url"], path])
        if r.returncode != 0:
            res["failed"].append((name, E.last_err(r)))
            print(f"[WARN] clone 失敗：{name}")
            continue
        note = ""
        commit = E.NODE_PINS.get(name) or n.get("commit")
        if commit:
            if name in E.NODE_PINS:
                r = E.run(["git", "checkout", "--detach", commit], cwd=path)
            else:   # 留在分支上，之後 update.py 才能繼續往前更新
                r = E.run(["git", "reset", "--hard", commit], cwd=path)
            note = f" @ {commit[:8]}" if r.returncode == 0 else "（回到紀錄 commit 失敗，停在最新）"
        res["cloned"].append(name + note)
        print(f"[INFO] 已 clone：{name}{note}")
    return res


def filtered_lock():
    """去掉 torch、黑名單、自訂 wheel、本機路徑的 freeze"""
    skip = {E.norm(p) for p in E.TORCH_PINS} | {E.norm(p) for p in E.BLOCKLIST} \
        | {E.norm(n) for _, n, _ in E.local_wheels()}
    out, dropped = [], []
    with open(E.PIP_FREEZE, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith(("#", "-e")):
                continue
            name = E.norm(s.split("==")[0].split(" @ ")[0].strip())
            if name in skip or "@ file:" in s:
                dropped.append(name)
                continue
            out.append(s)
    path = os.path.join(E.STATE_DIR, "pip_freeze.filtered.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return path, len(out), dropped


# ══════════ 內層：在 venv 裡安裝 ══════════
def inner(args):
    os.chdir(E.ROOT)
    log_path = E.start_log("install")
    rep = E.Report()
    snap0 = E.snapshot()
    pv = sys.version_info
    print(f"[INFO] venv Python {pv.major}.{pv.minor}.{pv.micro}：{sys.executable}")

    E.header("1/8 pip")
    r = E.run(E.PIP + ["install", "--upgrade", "pip", "-q"])
    print("[INFO] pip 已更新" if r.returncode == 0 else f"[WARN] {E.last_err(r)}")

    E.header("2/8 PyTorch（鎖定版）")
    if E.fix_torch(rep, "PyTorch", culprit="install.py", snap=snap0):
        print(f"[INFO] torch {E.snapshot().get('torch')}")
    else:
        print("[ERROR] PyTorch 安裝失敗，中止（後面的套件都依賴它）")
        sys.exit(1)

    E.header("3/8 ComfyUI 本體")
    core_note = "未鎖版"
    if E.PIN_CORE:
        ok, msg = E.checkout_core_pin()
        core_note = f"鎖定於 {E.PIN_CORE}：{msg}"
        print(f"[{'INFO' if ok else 'ERROR'}] {core_note}")
    core = E.core_info()
    print(f"[INFO] v{core['version']}  {core['short']}  {core['date']}  {core['subject']}")
    for fn in ("requirements.txt", "manager_requirements.txt"):
        p = os.path.join(E.ROOT, fn)
        if os.path.exists(p):
            print(f"[INFO] {fn}")
            E.install_tracked(rep, ["-r", p], "本體 requirements", fn)

    E.apply_core_patch(quiet=True)

    E.header("4/8 額外套件")
    E.install_tracked(rep, list(E.EXTRA_PACKAGES), "額外套件", "EXTRA_PACKAGES")

    E.header("5/8 自訂節點")
    nodes = dict(cloned=[], present=[], manual=[], failed=[], self_made=[])
    if args.skip_nodes:
        print("[SKIP] --skip-nodes")
    else:
        nodes = restore_nodes()
    scan = E.scan_nodes()
    hit = 0
    for m in E.enabled_nodes(scan):
        if m["has_req"]:
            hit += 1
            E.install_node_reqs(rep, m)
    print(f"[INFO] 已處理 {hit} 個節點的 requirements.txt")

    E.header("6/8 鎖定檔")
    if not args.lock:
        print("[SKIP] 未指定 --lock")
    elif not os.path.exists(E.PIP_FREEZE):
        print("[WARN] 沒有 env_state/pip_freeze.txt，跳過")
    else:
        path, n, dropped = filtered_lock()
        print(f"[INFO] 依 pip_freeze.txt 還原 {n} 個套件（略過 {len(dropped)} 個：torch/黑名單/自訂 wheel）")
        E.install_tracked(rep, ["-r", path], "鎖定檔", "pip_freeze.txt")

    E.header("7/8 自訂 wheel")
    if E.local_wheels():
        # skip_newer：不要把自己編譯的新版（comfy_kitchen 0.2.37）
        # 倒退成 wheels/ 裡的舊檔（0.2.35）
        E.install_wheels(skip_newer=True)
    else:
        print("[SKIP] wheels/ 沒有 .whl")

    E.header("8/8 驗證")
    E.guard(rep, "最終檢查")
    snap = E.snapshot()
    r = E.run([sys.executable, "-c", E.TORCH_CHECK])
    tparts = r.stdout.strip().split("|") if r.returncode == 0 else []
    checks = E.import_checks()
    try:
        E.write_state(scan=E.scan_nodes(), snap=snap)
        state_msg = (E.STATE_DIR if E.SPLIT else os.path.relpath(E.STATE_DIR, E.ROOT))
    except Exception as ex:
        state_msg = f"寫入失敗：{ex}"

    # ── 摘要 ──
    E.header("安裝摘要")
    E.print_env(snap)
    E.section("GPU")
    if len(tparts) == 5:
        ver, cuda, avail, gpu, sm = tparts
        print(f"  {'✓' if avail == 'True' else '✗'} torch {ver}　CUDA {cuda}"
              f"　可用：{avail}　{gpu}　{sm}")
    else:
        print(f"  ✗ torch 匯入失敗：{E.last_err(r)}")

    E.section("ComfyUI 本體")
    print(f"  模式：{core_note}")
    print(f"  版本：v{core['version']}　{core['short']}　{core['date']}　{core['subject']}")
    fe = snap.get("comfyui-frontend-package")
    if fe:
        print(f"  前端：comfyui-frontend-package {fe}")

    E.print_events(rep)

    E.section("套件")
    a, rm, ch = E.diff(snap0, snap)
    print(f"  本次：新增 {len(a)}、改版 {len(ch)}、移除 {len(rm)}（完整清單見紀錄檔）")
    for l in E.fmt_changes(a, rm, ch):
        E.log_only("      " + l)

    E.section("自訂節點")
    print(f"  已存在 {len(nodes['present'])}　新 clone {len(nodes['cloned'])}"
          f"　失敗 {len(nodes['failed'])}")
    for n in nodes["cloned"]:
        print(f"  + {n}")
    for n, why in nodes["failed"]:
        print(f"  ✗ {n}：{why}")
    if nodes["manual"]:
        print(f"  需在 Manager 手動安裝（無 git）{len(nodes['manual'])} 個：")
        for n in nodes["manual"]:
            v = f" v{n['version']}" if n.get("version") else ""
            print(f"      - {n['name']}{v}")
    if nodes["self_made"]:
        print("  自製節點（需自行從備份複製）：")
        for n in nodes["self_made"]:
            print(f"      - {n}")
    if nodes["manual"]:
        print("  裝完這些節點後再跑一次 python install.py，補齊它們的依賴")
    bad_nodes = [m for m in E.enabled_nodes(scan) if not m.get("valid", True)]
    if bad_nodes:
        print(f"  ✗ {len(bad_nodes)} 個資料夾沒有 __init__.py，ComfyUI 載不起來：")
        for m in bad_nodes:
            print(f"      - {m['name']}　→ 確認是殘骸後刪除")

    E.section("自訂 wheel")
    E.print_wheels(E.wheel_status(snap))
    for name, ok, msg in checks:
        print(f"  {'✓' if ok else '✗'} 匯入 {name:14} {msg}")
        if name == "comfy_kitchen" and not ok:
            print("      → 執行 python build_comfy_kitchen.py 編譯新版（Sol-Attn 需要）")

    E.print_dependency_check(scan, snap)

    print()
    print(f"  環境快照：{state_msg}")
    print(f"  紀錄檔：{(log_path if E.SPLIT else os.path.relpath(log_path, E.ROOT))}")
    print("=" * 54)
    print("  啟動：run.bat")


if __name__ == "__main__":
    a = parse_args()
    # --venv-only 只管 venv，不碰本體（純粹重建，不混其他功能）
    if not a.inner and not a.venv_only:
        ensure_core(a)
    if a.inner or (in_venv() and not a.recreate):
        inner(a)
    else:
        outer(a)
