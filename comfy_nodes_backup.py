# -*- coding: utf-8 -*-
"""
節點備份 / 還原 / 啟停

備份存的是「清單」不是「檔案」：每個節點的 git 網址、commit、分支、啟停狀態。
一個 JSON 幾十 KB，而複製 custom_nodes 動輒好幾 GB。
還原時用 git 把節點拉回當時的 commit，缺的重新 clone。

可以單獨當 CLI 用：
    python comfy_nodes_backup.py list                     列出備份
    python comfy_nodes_backup.py save [說明]              建立備份
    python comfy_nodes_backup.py show  <檔名>             看備份內容
    python comfy_nodes_backup.py diff  <檔名>             比對備份與現狀
    python comfy_nodes_backup.py restore <檔名> [--go]    還原（預設只演練）
    add <git 網址>            安裝節點（clone + 裝它的 requirements）
                              選項：--ref <分支或commit> --no-deps --force
"""
import datetime
import io
import json
import os
import re
import shutil
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

NODES_DIR = os.path.join(ROOT, "custom_nodes")
# 停用區：舊版用 custom_nodes_disabled/，ComfyUI-Manager 用 custom_nodes/.disabled
DISABLED_DIR = os.path.join(ROOT, "custom_nodes_disabled")
ALT_DISABLED = os.path.join(NODES_DIR, ".disabled")
# 備份跟腳本放一起，重裝本體不會連備份一起沒了
BACKUP_DIR = os.path.join(TOOL_DIR, "node_backups")


# 無 console 模式（pythonw / console=False 的 EXE）下，子程序沒帶這個旗標
# 就會被 Windows 另外配一個 console —— 畫面上就是一閃而過的黑窗。
# 節點掃描會對每個節點跑一次 git，四十個節點就是四十個黑窗連閃。
NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          creationflags=NOWIN)


def _git(path, *args):
    return run(["git"] + list(args), cwd=path)


def _node_info(name, path, disabled):
    info = dict(name=name, disabled=disabled,
                git=os.path.isdir(os.path.join(path, ".git")),
                valid=os.path.exists(os.path.join(path, "__init__.py")))
    if info["git"]:
        info["url"] = _git(path, "remote", "get-url", "origin").stdout.strip()
        info["commit"] = _git(path, "rev-parse", "HEAD").stdout.strip()
        info["branch"] = _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        info["dirty"] = bool(_git(path, "status", "--porcelain",
                                  "-uno").stdout.strip())
    return info


def scan():
    """現在的節點狀態 → [dict]，啟用與停用都含在內"""
    out = []
    for base, disabled in ((NODES_DIR, False), (DISABLED_DIR, True)):
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base), key=str.lower):
            p = os.path.join(base, name)
            if not os.path.isdir(p) or name.startswith((".", "__")):
                continue
            out.append(_node_info(name, p, disabled))
    return out


def save(note=""):
    """建立備份，回傳檔案完整路徑"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    nodes = scan()
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BACKUP_DIR, f"nodes_{stamp}.json")
    data = dict(time=datetime.datetime.now().isoformat(timespec="seconds"),
                note=note, count=len(nodes), nodes=nodes)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def backups():
    """[(檔名, 完整路徑, 時間, 說明, 節點數)]，新的在前"""
    if not os.path.isdir(BACKUP_DIR):
        return []
    res = []
    for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not f.endswith(".json"):
            continue
        p = os.path.join(BACKUP_DIR, f)
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            res.append((f, p, d.get("time", "?"), d.get("note", ""),
                        d.get("count", len(d.get("nodes", [])))))
        except Exception:
            res.append((f, p, "?", "（讀取失敗）", 0))
    return res


def load(path):
    if not os.path.isabs(path):
        path = os.path.join(BACKUP_DIR, path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def plan(path):
    """比對備份與現狀 → [(動作, 節點名, 說明)]　動作：clone/checkout/enable/disable/extra/ok"""
    want = {n["name"]: n for n in load(path).get("nodes", [])}
    have = {n["name"]: n for n in scan()}
    acts = []
    for name, w in want.items():
        h = have.get(name)
        if h is None:
            if w.get("url"):
                acts.append(("clone", name,
                             f"重新 clone {w['url']} @ {(w.get('commit') or '')[:8]}"))
            else:
                acts.append(("manual", name, "無 git 來源，需在 Manager 手動安裝"))
            continue
        if w.get("commit") and h.get("commit") and w["commit"] != h["commit"]:
            acts.append(("checkout", name,
                         f"{h['commit'][:8]} → {w['commit'][:8]}"))
        if bool(w.get("disabled")) != bool(h.get("disabled")):
            acts.append(("enable" if not w["disabled"] else "disable", name,
                         "啟用" if not w["disabled"] else "停用"))
    for name in have:
        if name not in want:
            acts.append(("extra", name, "備份當時沒有這個節點（不會自動刪除）"))
    if not acts:
        acts.append(("ok", "", "與備份完全一致，沒有要做的事"))
    return acts


def _move(name, to_disabled):
    src = os.path.join(DISABLED_DIR if not to_disabled else NODES_DIR, name)
    dst_dir = DISABLED_DIR if to_disabled else NODES_DIR
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, name)
    if os.path.exists(dst):
        return False, "目標位置已存在同名資料夾"
    if not os.path.isdir(src):
        return False, "找不到來源資料夾"
    shutil.move(src, dst)
    return True, "已移動"


def set_enabled(name, enabled):
    """啟用 / 停用一個節點（在 custom_nodes 和 custom_nodes_disabled 之間搬移）"""
    return _move(name, to_disabled=not enabled)


def restore(path, log=print):
    """照備份還原。回傳 (成功數, 失敗數)"""
    want = {n["name"]: n for n in load(path).get("nodes", [])}
    ok = fail = 0
    for action, name, why in plan(path):
        if action in ("ok", "extra", "manual"):
            log(f"[SKIP] {name or '-'}：{why}")
            continue
        w = want[name]
        base = DISABLED_DIR if w.get("disabled") else NODES_DIR
        target = os.path.join(base, name)
        if action == "clone":
            os.makedirs(base, exist_ok=True)
            r = run(["git", "clone", w["url"], target])
            if r.returncode != 0:
                log(f"[FAIL] {name}：clone 失敗")
                fail += 1
                continue
            action = "checkout"     # clone 完接著回到指定 commit
        if action == "checkout":
            cur = os.path.join(NODES_DIR, name)
            p = cur if os.path.isdir(cur) else os.path.join(DISABLED_DIR, name)
            if not os.path.isdir(p):
                log(f"[FAIL] {name}：找不到資料夾")
                fail += 1
                continue
            _git(p, "fetch", "origin", "--tags")
            br = w.get("branch") or "master"
            if br and br != "HEAD":
                _git(p, "checkout", br)      # 留在分支上，之後還能正常更新
            r = _git(p, "reset", "--hard", w["commit"])
            if r.returncode == 0:
                log(f"[OK]   {name} → {w['commit'][:8]}")
                ok += 1
            else:
                log(f"[FAIL] {name}：無法回到 {w['commit'][:8]}")
                fail += 1
        elif action in ("enable", "disable"):
            good, msg = set_enabled(name, action == "enable")
            log(f"[{'OK' if good else 'FAIL'}]   {name}：{why} — {msg}")
            ok += good
            fail += (not good)
    return ok, fail


GIT_URL_RE = re.compile(
    r"^(?:https?://|git@)[\w.\-]+(?::\d+)?[/:][\w.\-]+/[\w.\-]+?(?:\.git)?/?$")


def parse_url(text):
    """
    把使用者貼進來的東西整理成 (clone 網址, 資料夾名, 指定的 ref, 錯誤訊息)。

    吃得下：
        https://github.com/Luisacaotica/ComfyUI-MiniMaxH3Mod
        同上 + .git / 結尾斜線 / ?tab=readme
        .../tree/main            → ref = main
        .../commit/abc1234       → ref = abc1234
        git@github.com:使用者/倉庫.git
        使用者/倉庫               → 當成 GitHub
    """
    t = (text or "").strip().strip('"').strip("'")
    if not t:
        return None, None, None, "沒有輸入網址"
    ref = None
    m = re.match(r"^(https?://[^\s]+?)/(?:tree|commit)/([^/\s]+)/?$", t)
    if m:
        t, ref = m.group(1), m.group(2)
    t = re.sub(r"[?#].*$", "", t).rstrip("/")
    if re.match(r"^[\w.\-]+/[\w.\-]+$", t) and "://" not in t:
        t = "https://github.com/" + t
    if not GIT_URL_RE.match(t):
        return None, None, None, f"看不懂這個網址：{t}"
    name = t.rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    if not name:
        return None, None, None, "從網址取不出資料夾名稱"
    return t, name, ref, ""


def add_node(text, ref=None, no_deps=False, force=False, log=print):
    """
    clone 一個自訂節點到 custom_nodes，並安裝它的 requirements.txt。

    刻意不做的事：
      - 不碰 ComfyUI-Manager 的資料庫，就是單純 git clone
      - 不自動蓋掉同名節點，發現重複直接停下來讓你決定
      - requirements 走 comfy_env 的約束檔與黑名單，所以它不會偷偷
        把 torch 換成 CPU 版、把 numpy 降級，或裝進 tensorflow
    回傳結束碼（0 = 成功）
    """
    url, name, url_ref, err = parse_url(text)
    if err:
        log(f"[ERROR] {err}")
        return 2
    ref = ref or url_ref

    log(f"[INFO] 來源：{url}")
    log(f"[INFO] 目標：custom_nodes\\{name}" + (f"　ref {ref}" if ref else ""))

    dst = os.path.join(NODES_DIR, name)
    if os.path.exists(dst):
        log(f"[ERROR] 已經存在：{dst}")
        log("        要重裝的話先把這個資料夾移走或改名。")
        return 1
    for off in (os.path.join(DISABLED_DIR, name),
                os.path.join(NODES_DIR, ".disabled", name)):
        if os.path.exists(off):
            log(f"[ERROR] 這個節點在停用區：{off}")
            log("        想用的話在節點分頁把它啟用，不要重複 clone。")
            return 1
    if not shutil.which("git"):
        log("[ERROR] 找不到 git")
        return 1

    os.makedirs(NODES_DIR, exist_ok=True)
    log(f"[INFO] git clone --recursive {url}")
    r = run(["git", "clone", "--recursive", url, dst])
    log((r.stdout or "") + (r.stderr or ""))
    if r.returncode != 0 or not os.path.isdir(dst):
        log("[ERROR] clone 失敗（網址打錯？私有倉庫？沒有網路？）")
        shutil.rmtree(dst, ignore_errors=True)
        return 1

    if ref:
        r = run(["git", "checkout", ref], cwd=dst)
        log(f"[{'INFO' if r.returncode == 0 else 'WARN'}] "
            + (f"已切到 {ref}" if r.returncode == 0 else "切不過去，停在預設分支"))

    entries = [f for f in ("__init__.py", "nodes.py")
               if os.path.exists(os.path.join(dst, f))]
    if entries:
        log(f"[INFO] 進入點：{'、'.join(entries)}")
    else:
        log("[WARN] 裡面沒有 __init__.py 也沒有 nodes.py，ComfyUI 可能載不起來")
        log("       （有些倉庫要把子資料夾搬出來才是節點本體）")

    req = os.path.join(dst, "requirements.txt")
    if no_deps:
        log("[SKIP] 不裝依賴")
    elif not os.path.exists(req):
        log("[INFO] 沒有 requirements.txt，不用裝依賴")
    else:
        sys.path.insert(0, ROOT)
        try:
            import comfy_env as E
        except Exception as ex:
            log(f"[WARN] 讀不到 comfy_env.py（{ex}），改用不設防的 pip install")
            E = None
        with io.open(req, encoding="utf-8", errors="replace") as f:
            lines = [x.strip() for x in f
                     if x.strip() and not x.strip().startswith("#")]
        log(f"[INFO] requirements.txt（{len(lines)} 行）：")
        for l in lines[:25]:
            log(f"         {l}")
        if len(lines) > 25:
            log(f"         …還有 {len(lines) - 25} 行")

        if E is not None:
            bl = {E.norm(b) for b in E.BLOCKLIST}
            bad = [l for l in lines
                   if E.norm(re.split(r"[<>=!~\[; ]", l, 1)[0]) in bl]
            if bad and not force:
                log("")
                log("[ERROR] 這個節點要求的套件在黑名單裡：")
                for l in bad:
                    k = E.norm(re.split(r"[<>=!~\[; ]", l, 1)[0])
                    why = next((v for b, v in E.BLOCKLIST.items()
                                if E.norm(b) == k), "")
                    log(f"         {l}　←　{why}")
                log("        裝下去會弄壞現在的環境。節點已經 clone 好但依賴沒裝；")
                log("        確定要裝就勾「忽略黑名單」，或把它從 custom_nodes 刪掉。")
                return 3
            rep = E.Report()
            E.install_tracked(rep, ["-r", req], "新節點依賴", name)
            E.print_events(rep)
            E.guard(rep, f"安裝 {name} 之後的檢查", culprit=name)
        else:
            r = run([sys.executable, "-m", "pip", "install", "-r", req])
            log((r.stdout or "") + (r.stderr or ""))

    log("")
    log("=" * 54)
    log(f"  {name} 已安裝")
    log("=" * 54)
    log("  重新啟動 ComfyUI 才會載入新節點。")
    log("  跑一次 update.py 會把它記進 env_state/nodes.json（重建時才會還原）。")
    return 0


def _cli():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 0
    cmd = a[0]
    if cmd == "add" and len(a) > 1:
        ref = None
        if "--ref" in a:
            i = a.index("--ref")
            ref = a[i + 1] if i + 1 < len(a) else None
        return add_node(a[1], ref=ref, no_deps="--no-deps" in a,
                        force="--force" in a)
    if cmd == "save":
        print("已建立備份：" + save(" ".join(a[1:])))
    elif cmd == "list":
        rows = backups()
        if not rows:
            print("還沒有任何備份")
        for f, p, t, note, c in rows:
            print(f"  {f}　{t}　{c} 個節點　{note}")
    elif cmd == "show" and len(a) > 1:
        d = load(a[1])
        print(f"時間 {d.get('time')}　說明 {d.get('note')}　{d.get('count')} 個節點")
        for n in d["nodes"]:
            st = "停用" if n.get("disabled") else "啟用"
            print(f"  {st}  {n['name']:40} {(n.get('commit') or '-')[:8]}  "
                  f"{n.get('url') or '（無 git）'}")
    elif cmd == "diff" and len(a) > 1:
        for action, name, why in plan(a[1]):
            print(f"  [{action:8}] {name:40} {why}")
    elif cmd == "restore" and len(a) > 1:
        if "--go" not in a:
            print("演練（加 --go 實際執行）：")
            for action, name, why in plan(a[1]):
                print(f"  [{action:8}] {name:40} {why}")
        else:
            ok, fail = restore(a[1])
            print(f"完成 {ok}　失敗 {fail}")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
