# -*- coding: utf-8 -*-
"""
ComfyUI 更新工具

設定（鎖定版本、黑名單、排除 / 固定節點）全部在 comfy_env.py。

流程：
  0. 環境檢查（Python / torch / 黑名單）
  1. ComfyUI 本體：fast-forward 到上游，或切到 PIN_CORE
  2. 同步本體 requirements
  3. 逐一補齊節點 requirements（每個節點各自記帳，抓出是誰裝了什麼）
  4. 更新有 git 的自訂節點（NODE_PINS 內的切到指定版本）
  5. 檢查 wheels/ 自訂 wheel 是否被覆蓋
  6. 寫 env_state/（節點清單 + pip freeze），install.py 重建用
  7. 摘要：復原 / 移除了什麼、排除 / 停用 / 固定了什麼、核心版本

所有輸出同時寫入 update_logs/update_YYYYmmdd_HHMMSS.log
"""
import os
import subprocess
import sys

# 明確把自己的目錄加進 sys.path。平常 Python 會自動加，但隔離模式
# （python -I）、凍結成 EXE、或被別的啟動器叫起來時不一定有。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import comfy_env as E  # noqa: E402

# --no-launch：跑完直接結束，不問「是否啟動 ComfyUI」。
#              GUI（comfy_manager.py）和無人值守排程都用這個。
NO_LAUNCH = "--no-launch" in sys.argv or os.environ.get("COMFY_NO_LAUNCH") == "1"

# ── 本體更新方式 ──
# "ff"     ：只接受 fast-forward。核心沒有自訂修改時用這個，最安全。
# "rebase" ：核心有自己的 patch commit 時用。每次更新會把你的 commit
#            重新疊到上游最新版之上；衝突時自動 abort 回復原狀。
# 這台機器的核心有 MiniMax H3 的自訂 patch，所以預設 rebase。
CORE_UPDATE_MODE = "rebase"

# True：本體有「未提交」的改動時自動 git stash（更新後 git stash pop 取回）。
#       False 則只點名檔案並跳過更新，不碰你的改動。
CORE_AUTO_STASH = False

os.chdir(E.ROOT)
log_path = E.start_log("update")
rep = E.Report()
pv = sys.version_info


# ══════════ 0. 環境檢查 ══════════
E.header("環境鎖定檢查")
print(f"[INFO] Python {pv.major}.{pv.minor}.{pv.micro}")
if (pv.major, pv.minor) != E.PY_MAJOR_MINOR:
    print(f"[ERROR] 必須是 Python {E.PY_MAJOR_MINOR[0]}.{E.PY_MAJOR_MINOR[1]}，中止")
    sys.exit(1)
snap_start = E.snapshot()
ok, why = E.torch_status(snap_start)
print(f"[INFO] PyTorch {snap_start.get('torch')}" if ok else f"[WARN] {why}")
print(f"[INFO] 約束檔：{'啟用' if E.USE_CONSTRAINTS else '停用'}")
E.guard(rep, "啟動前", snap=snap_start)


# ══════════ 1. ComfyUI 本體 ══════════
E.header("ComfyUI 本體")
core0 = E.core_info()
print(f"[INFO] 目前：v{core0['version']}  {core0['short']}  "
      f"{core0['date']}  {core0['subject']}")
core_err = ""
if E.PIN_CORE:
    core_mode = f"鎖定於 {E.PIN_CORE}"
    ok, msg = E.checkout_core_pin()
    print(f"[{'INFO' if ok else 'ERROR'}] {core_mode}：{msg}")
    if not ok:
        core_err = msg
else:
    # 先把 fetch 範圍縮成只有本體分支。預設會抓上游幾百個開發分支，
    # 其中任何一個的本地參照對不上就會讓整個 fetch 失敗。
    narrowed, nmsg = E.narrow_fetch_refspec()
    if narrowed and nmsg:
        print(f"[INFO] {nmsg}")

    # --force / --prune-tags：上游重打 tag 時（ComfyUI 發版常見），
    # 不加會得到 "would clobber existing tag" 而讓 fetch 回傳非 0，
    # 看起來像斷線，其實連線正常。覆蓋核心抓過 --tags 之後特別容易遇到。
    FETCH = ["git", "fetch", "origin", "--force", "--tags", "--prune-tags"]
    r = E.run(FETCH)
    if r.returncode != 0 and "cannot lock ref" in (r.stderr or "") + (r.stdout or ""):
        # 殘留的壞參照。清掉之後重試一次，不要讓它擋住整次更新。
        print("[INFO] 偵測到殘留的遠端參照，清理後重試…")
        E.run(["git", "remote", "prune", "origin"])
        r = E.run(FETCH)
    if r.returncode != 0:
        # 不要寫死成「無法連線」—— 回傳碼非 0 的原因很多，
        # 把 git 真正說的話帶出來，否則排查時會被誤導。
        core_err = f"git fetch 失敗：{E.last_err(r)}"
        core_mode = "未鎖版"
        print(f"[WARN] {core_err}")
        print("[WARN] 跳過本體更新")
    else:
        br = E.default_branch()
        core_mode = f"追蹤 origin/{br}（未鎖版）"
        if core0["branch"] == "HEAD":
            print(f"[INFO] 目前為 detached HEAD（先前鎖過版），切回 {br}")
            r = E.run(["git", "checkout", br])
            if r.returncode != 0:
                core_err = f"無法切回 {br}：{E.last_err(r)}"
        if not core_err:
            up = E.upstream_ref()
            behind = E.count(f"HEAD..{up}")
            ahead = E.count(f"{up}..HEAD")
            if ahead:
                core_mode += f"　本地領先 {ahead} 個提交（自訂 patch）"
                print(f"[INFO] 本地領先 {ahead} 個提交（自訂 patch），落後 {behind} 個")
            if behind == 0:
                print("[INFO] 已是最新版本")
            else:
                print(f"[INFO] 發現 {behind} 個新提交，更新中…")

                # 空佔位檔（put_*_here、_output_images_will_be_put_here）
                # 被刪掉會擋住更新，而清 output 就會刪到它。先自動還原。
                ph = E.restore_placeholders()
                if ph:
                    print(f"[INFO] 已還原 {len(ph)} 個被刪掉的空佔位檔"
                          "（內容無意義，但會擋住更新）：")
                    for fn in ph:
                        print(f"        {fn}")

                # 工具自己的安全修補是「未提交的修改」，會擋住更新。
                # 確認那個檔案只有修補、沒有別的改動，就先撤下；
                # 本段結束後的 apply_core_patch() 會補回去。
                if E.unpatch_for_update():
                    print("[INFO] 暫時撤下 StringFormat 安全修補以便更新（更新後自動補回）")

                # 先點名擋住更新的檔案，不要等 merge 失敗才從錯誤訊息猜
                dirty = E.dirty_files()
                if dirty:
                    print(f"[WARN] 本體有 {len(dirty)} 個檔案被本地修改，會擋住更新：")
                    for st, fn in dirty[:20]:
                        print(f"        {st}  {fn}　({E.dirty_label(st)})")
                    if len(dirty) > 20:
                        print(f"        …共 {len(dirty)} 個，完整清單見紀錄檔")
                        for st, fn in dirty[20:]:
                            E.log_only(f"        {st}  {fn}　({E.dirty_label(st)})")
                    if CORE_AUTO_STASH:
                        r = E.run(["git", "stash", "push", "-m", "update.py 自動暫存"])
                        if r.returncode == 0:
                            print("[INFO] 已 git stash 暫存，更新後用 git stash pop 取回")
                            dirty = []
                        else:
                            core_err = f"git stash 失敗：{E.last_err(r)}"
                    else:
                        core_err = ("本體有本地改動，未更新（"
                                    + "、".join(fn for _, fn in dirty[:5])
                                    + ("…" if len(dirty) > 5 else "")
                                    + "）。要長期保留請 git commit 成本地提交並把 "
                                      "CORE_UPDATE_MODE 設成 \"rebase\"；"
                                      "只是臨時改動就設 CORE_AUTO_STASH = True")

                if not core_err:
                    if CORE_UPDATE_MODE == "rebase":
                        r = E.run(["git", "rebase", up])
                        if r.returncode != 0:
                            # 衝突時一定要 abort，否則倉庫會卡在 rebase 中途
                            E.run(["git", "rebase", "--abort"])
                            core_err = (f"rebase 衝突，已回復原狀：{E.last_err(r)}"
                                        f" → 請手動執行 git rebase {up} 解衝突")
                    else:
                        r = E.run(["git", "merge", "--ff-only", up])
                        if r.returncode != 0:
                            core_err = f"無法 fast-forward：{E.last_err(r)}"
                            if ahead:
                                core_err += ("（本地有自訂 patch commit，分支已分岔；"
                                             "請把 CORE_UPDATE_MODE 設成 \"rebase\"）")
        if core_err:
            print(f"[ERROR] {core_err}")

core1 = E.core_info()
core_changed = core0["hash"] != core1["hash"]
core_ahead = core_behind = 0
if core_changed:
    core_ahead = E.count(f"{core0['hash']}..{core1['hash']}")
    core_behind = E.count(f"{core1['hash']}..{core0['hash']}")
    log = E.run(["git", "log", "--oneline", "--no-decorate", "-n", "30",
                 f"{core0['hash']}..{core1['hash']}"]).stdout.strip()
    for line in log.splitlines():
        print(f"        {line}")


# ══════════ 2. 本體 requirements ══════════
# 本體一動過，之前套的安全修補就沒了（git merge / reset 會覆蓋檔案）。
# 這裡無條件檢查一次，已經在的話只印一行。
E.apply_core_patch(quiet=True)

E.header("同步本體 requirements")
if E.PIN_CORE and not core_changed:
    print("[SKIP] 核心鎖版且本次未變動，跳過")
else:
    for fn in ("requirements.txt", "manager_requirements.txt"):
        p = os.path.join(E.ROOT, fn)
        if os.path.exists(p):
            print(f"[INFO] {fn}")
            E.install_tracked(rep, ["-r", p], "本體 requirements", fn)


# ══════════ 3. 節點依賴 ══════════
E.header("節點依賴檢查")
scan = E.scan_nodes()
hit = skipped = 0
snap_nodes = E.snapshot()
for m in E.enabled_nodes(scan):
    if not m["has_req"]:
        continue
    hit += 1
    res = E.install_node_reqs(rep, m, snap=snap_nodes)
    if res == "skip":
        skipped += 1
    else:
        snap_nodes = E.snapshot()   # 真的跑過 pip，環境變了，重拍
print(f"[INFO] 已檢查 {hit} 個節點的 requirements.txt"
      f"（{skipped} 個已滿足、未呼叫 pip；{hit - skipped} 個有實際安裝）")


# ══════════ 4. 自訂節點 git 更新 ══════════
E.header("自訂節點更新")
updated, uptodate, failed, pinned = [], [], [], []
for m in scan["git"]:
    name, path = m["name"], m["path"]
    if E.run(["git", "fetch", "origin", "--tags"], cwd=path).returncode != 0:
        failed.append((name, "無法連線"))
        print(f"[WARN] 無法連線：{name}")
        continue

    if name in E.NODE_PINS:
        want = E.NODE_PINS[name]
        target = E.resolve(want, path)
        if not target:
            failed.append((name, f"找不到固定版本 {want}"))
            continue
        if E.resolve("HEAD", path) == target:
            state = "已在固定版本"
        else:
            r = E.run(["git", "checkout", "--detach", target], cwd=path)
            state = "已切換" if r.returncode == 0 else f"切換失敗：{E.last_err(r)}"
        pinned.append((name, want, state))
        print(f"[INFO] 固定：{name} @ {want}（{state}）")
        continue

    if E.current_branch(path) == "HEAD":
        failed.append((name, "detached HEAD，未更新（要固定請寫進 NODE_PINS）"))
        continue
    up = E.upstream_ref(path)
    n = E.count(f"HEAD..{up}", path)
    if n == 0:
        uptodate.append(name)
        continue

    old = E.resolve("HEAD", path)
    st = E.run(["git", "stash"], cwd=path)
    stashed = st.returncode == 0 and "No local changes" not in st.stdout
    r = E.run(["git", "merge", "--ff-only", up], cwd=path)
    if r.returncode != 0:
        why = f"無法 fast-forward：{E.last_err(r)}"
        if stashed:
            why += "（本地改動已 stash，到該節點目錄下 git stash pop 可取回）"
        failed.append((name, why))
        print(f"[WARN] 更新失敗：{name}")
        continue
    new = E.resolve("HEAD", path)
    updated.append((name, old[:8], new[:8], n, stashed))
    print(f"[INFO] 已更新：{name}  {old[:8]} → {new[:8]}（{n} 個提交）"
          + ("，本地修改已 git stash" if stashed else ""))
    # 重新檢查：更新後才新增 requirements.txt 的節點，前面那輪掃不到
    req = os.path.join(path, "requirements.txt")
    if os.path.exists(req):
        # 走同一條路：別名、黑名單、已滿足就跳過
        E.install_node_reqs(rep, {"path": path, "name": name}, stage="節點更新")


# ══════════ 5. 自訂 wheel ══════════
E.header("自訂 wheel 檢查")
ws = E.wheel_status()
bad = [w for w in ws if not w["ok"]]
wheels_fixed = []
if bad and E.AUTO_REINSTALL_WHEELS:
    print(f"[INFO] {len(bad)} 個 wheel 被覆蓋，自動重裝")
    E.install_wheels(only={w["file"] for w in bad})
    wheels_fixed = [w["name"] for w in bad]
    ws = E.wheel_status()
print(f"[INFO] 共 {len(ws)} 個，{sum(w['ok'] for w in ws)} 個正常")


# ══════════ 6. 環境快照 ══════════
snap_end = E.snapshot()
scan = E.scan_nodes()
try:
    E.write_state(scan, snap_end)
    state_msg = (E.STATE_DIR if E.SPLIT else os.path.relpath(E.STATE_DIR, E.ROOT))
except Exception as ex:
    state_msg = f"寫入失敗：{ex}"


# ══════════ 7. 摘要 ══════════
E.header("更新摘要")
E.print_env(snap_end)

E.section("本體安全修補")
_st, _p = E.patch_status()
print("  " + {
    "patched":    "✓ StringFormat 格式字串限制　已套用",
    "vulnerable": "✗ StringFormat 未修補 —— 惡意工作流可藉此執行程式碼",
    "unknown":    "⚠ 認不出要修補的那一行，上游可能已自行修正，請人工確認",
    "missing":    "⚠ 找不到 comfy_extras/nodes_string.py",
}[_st])

E.section("ComfyUI 本體")
print(f"  模式：{core_mode}")
if core_changed:
    move = f"前進 {core_ahead} 個提交" + (f"、回退 {core_behind} 個" if core_behind else "")
    print(f"  版本：v{core0['version']} → v{core1['version']}")
    print(f"  提交：{core0['short']} → {core1['short']}（{move}）")
else:
    print(f"  版本：v{core1['version']}（未變）")
print(f"  目前：{core1['short']}  {core1['date']}  {core1['subject']}")
fe = snap_end.get("comfyui-frontend-package")
fe0 = snap_start.get("comfyui-frontend-package")
if fe:
    print(f"  前端：comfyui-frontend-package {fe0} → {fe}" if fe0 and fe0 != fe
          else f"  前端：comfyui-frontend-package {fe}")
if core_err:
    print(f"  ✗ {core_err}")

E.print_events(rep)

E.section("套件異動（依來源）")
if not rep.pkg_changes:
    print("  （無）")
for stage, src, a, rm, ch in rep.pkg_changes:
    print(f"  [{stage}] {src}")
    E.print_changes(E.fmt_changes(a, rm, ch), indent="      ", limit=6)
a, rm, ch = E.diff(snap_start, snap_end)
print(f"  本次淨異動：新增 {len(a)}、改版 {len(ch)}、移除 {len(rm)}"
      "（中途被移除 / 還原的不算）")
E.print_changes(E.fmt_changes(a, rm, ch), indent="      ", limit=15)

E.section("自訂節點")
print(f"  已更新 {len(updated)}　已是最新 {len(uptodate)}　失敗 {len(failed)}"
      f"　固定 {len(pinned)}")
for name, o, n, cnt, stashed in updated:
    print(f"  ↑ {name}  {o} → {n}（{cnt} 個提交）"
          + ("　⚠ 本地修改已 stash，git stash pop 可取回" if stashed else ""))
for name, want, state in pinned:
    print(f"  📌 {name} @ {want}（{state}）")
for name, why in failed:
    print(f"  ✗ {name}：{why}")
if uptodate:
    print(f"  已是最新：{', '.join(uptodate)}")

present = {m["name"] for m in scan["excluded"]}
if E.EXCLUDE_NODES:
    print("  排除（不自動更新，依賴仍檢查）：")
    for name, why in E.EXCLUDE_NODES.items():
        print(f"      - {name} — {why}" + ("" if name in present else "（未安裝）"))
if scan["disabled"]:
    print(f"  停用 {len(scan['disabled'])} 個（不更新、不裝依賴）：")
    for m in scan["disabled"]:
        print(f"      - {E.describe(m)}")
if scan["nogit"]:
    print(f"  無 git（Manager / Registry 安裝，請在 Manager 內更新）"
          f"{len(scan['nogit'])} 個：")
    for m in scan["nogit"]:
        print(f"      - {E.describe(m)}")

bad_nodes = [m for m in E.enabled_nodes(scan) if not m.get("valid", True)]
if bad_nodes:
    print(f"  ✗ {len(bad_nodes)} 個資料夾沒有 __init__.py，"
          "ComfyUI 啟動時會對它們報 FileNotFoundError：")
    for m in bad_nodes:
        print(f"      - {m['name']}　→ 確認是殘骸後刪除 custom_nodes\\{m['name']}")

E.section("自訂 wheel")
E.print_wheels(ws)
if wheels_fixed:
    print(f"  已自動重裝：{', '.join(wheels_fixed)}")
elif any(not w["ok"] for w in ws):
    print("  → 執行 python install_wheels.py 重裝"
          "（或在 comfy_env.py 設 AUTO_REINSTALL_WHEELS = True）")
ck_bad = False
for name, ok, msg in E.import_checks():
    print(f"  {'✓' if ok else '✗'} 匯入 {name:14} {msg}")
    if name == "comfy_kitchen" and not ok:
        ck_bad = True
if ck_bad:
    print("  → comfy_kitchen 太舊或未安裝：執行 python build_comfy_kitchen.py")

# 最後回答「這些退退補補之後，環境到底符不符合各節點的宣告」
E.print_dependency_check(scan, snap_end)

print()
print(f"  環境快照：{state_msg}")
print(f"  紀錄檔：{(log_path if E.SPLIT else os.path.relpath(log_path, E.ROOT))}")
print("=" * 54)

if NO_LAUNCH:
    print()
    print("[INFO] --no-launch：純更新完成，未啟動 ComfyUI")
else:
    print()
    try:
        ans = input("是否立即啟動 ComfyUI？(Y/N): ").strip().upper()
    except EOFError:        # 被 GUI / 排程以無輸入方式呼叫
        ans = "N"
    if ans == "Y":
        subprocess.run([sys.executable, "main.py"] + E.LAUNCH_ARGS, cwd=E.ROOT)
