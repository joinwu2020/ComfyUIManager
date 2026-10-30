# -*- coding: utf-8 -*-
"""
ComfyUI 環境管理介面

把散落的腳本收成一個視窗：環境參數設定、純更新、安全檢查、重新安裝、
啟動 ComfyUI、節點備份還原，每項任務各有自己的紀錄分頁。

放在 ComfyUI 根目錄，用系統 Python 或 venv 的 Python 執行都可以：
    python comfy_manager.py
或雙擊 manager.bat

需要同目錄的：comfy_env.py、update.py、install.py、comfy_audit.py、
              comfy_config_io.py、comfy_nodes_backup.py
（缺哪個就只是那顆按鈕不能用，其他照常）
"""
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

FROZEN = getattr(sys, "frozen", False)

# 每個子程序都要帶這個。無 console 模式（pythonw / console=False 的 EXE）下，
# 子程序若沒帶這個旗標，Windows 會幫它另外配一個 console —— 就是一閃而過的黑窗。
NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def sh(cmd, **kw):
    """subprocess.run 的統一包裝：固定帶 CREATE_NO_WINDOW、utf-8 解碼，
    而且不會丟例外（失敗就回一個 returncode=1 的假結果）。

    背景執行緒專用；主執行緒請透過 App.bg.submit()。
    """
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    kw.setdefault("creationflags", NOWIN)
    try:
        return subprocess.run(cmd, **kw)
    except (OSError, subprocess.SubprocessError) as ex:
        class R:
            returncode, stdout, stderr = 1, "", str(ex)
        return R()


# ComfyUI 根目錄的判斷依據：這兩個檔案同時存在
# 腳本目錄的判斷依據。main.py 不再是必要條件 —— 本體可能在別的地方
# （comfy_env.py 的 COMFY_ROOT）。
MARKERS = ("comfy_env.py",)


def _is_root(d):
    return all(os.path.exists(os.path.join(d, m)) for m in MARKERS)


def find_root():
    """找出 ComfyUI 根目錄。

    PyInstaller --onedir 會把 EXE 放在 dist\\ComfyManager\\ 底下，
    直接用 EXE 所在目錄當根目錄會找不到 comfy_env.py 和 main.py。
    所以依序試：
      1. 環境變數 COMFY_ROOT
      2. 同目錄的 comfy_root.txt 裡寫的路徑
      3. 自己所在目錄
      4. 往上找最多 4 層（EXE 放在子資料夾的情況）
      5. 往下找一層（EXE 放在 ComfyUI 的上一層）
    都找不到就回自己所在目錄，由主程式提示使用者。
    """
    here = os.path.dirname(os.path.abspath(
        sys.executable if FROZEN else __file__))

    env = os.environ.get("COMFY_ROOT", "").strip().strip('"')
    if env and _is_root(env):
        return os.path.abspath(env)

    hint = os.path.join(here, "comfy_root.txt")
    if os.path.isfile(hint):
        try:
            with open(hint, encoding="utf-8") as f:
                p = f.read().strip().strip('"')
            if p and _is_root(p):
                return os.path.abspath(p)
        except OSError:
            pass

    d = here
    for _ in range(5):
        if _is_root(d):
            return d
        up = os.path.dirname(d)
        if up == d:
            break
        d = up

    for name in os.listdir(here) if os.path.isdir(here) else []:
        sub = os.path.join(here, name)
        if os.path.isdir(sub) and _is_root(sub):
            return sub

    return here


APP_DIR = find_root()          # 腳本所在目錄（comfy_env.py、update.py…）


def _comfy_dir():
    """
    ComfyUI 本體在哪。以 comfy_env.py 的 COMFY_ROOT 為準。

    這套腳本可以放在 ComfyUI 外面，本體路徑在 comfy_env.py 裡設定，
    這樣重裝或 reset 本體都不會動到管理工具。
    兩者相同時行為跟以前完全一樣。
    """
    try:
        sys.path.insert(0, APP_DIR)
        import comfy_env as _e
        import importlib
        importlib.reload(_e)       # 設定可能剛被介面改過
        return _e.ROOT
    except Exception:
        return APP_DIR


COMFY_DIR = _comfy_dir()


def _venv_dir():
    return os.path.join(COMFY_DIR, "venv")


def _in_venv(path):
    """這個解譯器是不是 ComfyUI 那個 venv 裡的"""
    if not path:
        return False
    try:
        return os.path.normcase(os.path.abspath(path)).startswith(
            os.path.normcase(os.path.abspath(_venv_dir())) + os.sep)
    except Exception:
        return False


# ══════════════════════════ 解譯器的分工 ══════════════════════════
#
# 管理介面「本身」跑在系統環境：它只用標準庫 + tkinter，
# comfy_config_io / comfy_nodes_backup / comfy_sys 也都只用標準庫。
# 要不要用 venv 是「每個動作各自決定」的事：
#
#   venv  —— 啟動 ComfyUI、更新、安裝依賴、安全檢查、修復、裝 wheel
#            （這些會 import torch 或改 site-packages）
#   系統  —— 重建 venv（不能從 venv 裡面重建自己）、純 git 操作
#
# 這樣 host_py() 不會因為「介面是從哪裡啟動的」而拿到錯的東西。
# 不過使用者還是可能先 activate 再執行，所以底下仍然檢查路徑。


def host_py():
    """
    「系統的」Python，保證不是 venv 裡那一支。

    給 install.py --recreate 用：不能從 venv 裡面重建 venv，
    install.py 偵測到會直接拒絕（--recreate 不能在 venv 內執行）。

    兩種會拿到錯誤解譯器的情況：
      1. 打包成 EXE —— sys.executable 指向 EXE 自己
      2. 從 venv 啟動管理介面 —— 捷徑指向 venv 底下的 pythonw.exe，
         或先 activate 再跑 manager.bat。此時 sys.executable 就是 venv 的。
    所以不能只看 FROZEN，要實際檢查路徑在不在 venv 底下。

    順序：sys.executable（若不在 venv 也沒凍結）→ PATH 上的 python
          → py -3.12 → py -3 → 放棄（回 None，呼叫端負責報錯）
    """
    if not FROZEN and not _in_venv(sys.executable):
        return sys.executable
    for name in ("python", "python3"):
        p = shutil.which(name)
        if p and not _in_venv(p):
            return p
    if os.name == "nt" and shutil.which("py"):
        for tag in ("-3.12", "-3"):
            r = sh(["py", tag, "-c", "import sys;print(sys.executable)"])
            cand = r.stdout.strip() if r.returncode == 0 else ""
            if cand and not _in_venv(cand):
                return cand
    return None


sys.path.insert(0, APP_DIR)
try:
    import comfy_config_io as CFG
except ImportError:
    CFG = None
try:
    import comfy_nodes_backup as NB
except ImportError:
    NB = None
try:
    import comfy_sys as SYS
    kill_tree = SYS.kill_tree
except ImportError:
    SYS = None

    def kill_tree(pid):
        try:
            if os.name == "nt":
                sh(["taskkill", "/F", "/T", "/PID", str(pid)])
            else:
                os.kill(int(pid), 9)
            return True
        except Exception:
            return False


# ══════════════════════════ 淺色主題 ══════════════════════════

BG = "#f4f5f7"          # 視窗底色
CARD = "#ffffff"         # 面板 / 輸入框底色
FG = "#1a1a1a"           # 主文字
MUTED = "#6b7280"        # 次要文字
LINE = "#d8dbe0"         # 分隔線
OK = "#15803d"
WARN = "#b45309"
ERR = "#b91c1c"
INFO = "#1d4ed8"
ACCENT = "#2563eb"

MONO = ("Consolas", 10)
UI = ("Microsoft JhengHei UI", 10)
UI_B = ("Microsoft JhengHei UI", 10, "bold")
UI_H = ("Microsoft JhengHei UI", 12, "bold")


def apply_theme(root):
    root.configure(bg=BG)
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    st.configure(".", background=BG, foreground=FG, font=UI,
                 fieldbackground=CARD, bordercolor=LINE, lightcolor=BG,
                 darkcolor=BG, troughcolor="#e5e7eb")
    st.configure("TFrame", background=BG)
    st.configure("Card.TFrame", background=CARD, relief="solid", borderwidth=1)
    st.configure("TLabel", background=BG, foreground=FG)
    st.configure("Card.TLabel", background=CARD, foreground=FG)
    st.configure("Muted.TLabel", background=BG, foreground=MUTED)
    st.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)
    st.configure("H.TLabel", background=BG, foreground=FG, font=UI_H)
    st.configure("TButton", background="#e9eaee", foreground=FG, padding=(10, 6))
    st.map("TButton", background=[("active", "#dcdee3"),
                                  ("disabled", "#f0f0f2")],
           foreground=[("disabled", "#a0a3aa")])
    st.configure("Go.TButton", background=ACCENT, foreground="white",
                 font=UI_B, padding=(12, 8))
    st.map("Go.TButton", background=[("active", "#1d4ed8"),
                                     ("disabled", "#b9c6e8")])
    st.configure("Danger.TButton", background="#fee2e2", foreground=ERR,
                 padding=(10, 6))
    st.map("Danger.TButton", background=[("active", "#fecaca")])
    st.configure("TNotebook", background=BG, borderwidth=0)
    st.configure("TNotebook.Tab", background="#e6e7eb", foreground=MUTED,
                 padding=(14, 7))
    st.map("TNotebook.Tab", background=[("selected", CARD)],
           foreground=[("selected", FG)])
    st.configure("TEntry", fieldbackground=CARD, foreground=FG)
    st.configure("TCombobox", fieldbackground=CARD, foreground=FG)
    st.configure("TCheckbutton", background=BG, foreground=FG)
    st.configure("Card.TCheckbutton", background=CARD, foreground=FG)
    st.configure("Treeview", background=CARD, fieldbackground=CARD,
                 foreground=FG, rowheight=24, borderwidth=1)
    st.configure("Treeview.Heading", background="#e9eaee", foreground=FG,
                 font=UI_B)
    st.map("Treeview", background=[("selected", "#dbe5fb")],
           foreground=[("selected", FG)])


# ══════════════════════════ 紀錄面板 ══════════════════════════

class Bg:
    """
    背景工作佇列。

    Tkinter 只能在主執行緒碰，但介面卡頓的來源全是「在主執行緒上開子程序」：
    一次 git 大約 30-80ms、一次 PowerShell/WMI 查詢 300-800ms、
    import torch 兩到五秒。這些只要發生在回呼裡，視窗就整個凍住，
    切分頁也會停住——因為切分頁的重繪也排在同一個事件迴圈上。

    所以規則是：會開子程序、會掃資料夾、會讀 git 的，一律 submit 到這裡，
    結果透過佇列回到主執行緒再更新畫面。
    """

    def __init__(self, app, workers=2):
        self.app = app
        self.jobs = queue.Queue()
        self.res = queue.Queue()
        self.inflight = set()       # 同一個 key 不重複排隊
        self._lock = threading.Lock()
        self._stop = False
        for _ in range(workers):
            threading.Thread(target=self._loop, daemon=True).start()
        app.after(80, self._drain)

    def submit(self, fn, cb=None, key=None):
        """fn 在背景執行緒跑；cb(結果, 例外) 回到主執行緒執行"""
        if key:
            with self._lock:
                if key in self.inflight:
                    return False
                self.inflight.add(key)
        self.jobs.put((fn, cb, key))
        return True

    def _loop(self):
        while not self._stop:
            try:
                fn, cb, key = self.jobs.get(timeout=0.4)
            except queue.Empty:
                continue
            try:
                out, err = fn(), None
            except Exception as ex:          # 背景的例外不該弄掉整個介面
                out, err = None, ex
            if key:
                with self._lock:
                    self.inflight.discard(key)
            self.res.put((cb, out, err))

    def _drain(self):
        for _ in range(20):
            try:
                cb, out, err = self.res.get_nowait()
            except queue.Empty:
                break
            if cb:
                try:
                    cb(out, err)
                except Exception:
                    pass
        if not self._stop:
            self.app.after(80, self._drain)

    def stop(self):
        self._stop = True


class LogPane(ttk.Frame):
    """一個任務的輸出區：彩色標記 + 自動捲動 + 另存"""

    def __init__(self, master):
        super().__init__(master)
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 4))
        self.status = ttk.Label(bar, text="待執行", style="Muted.TLabel")
        self.status.pack(side="left")
        ttk.Button(bar, text="另存", command=self.save).pack(side="right")
        ttk.Button(bar, text="清除", command=self.clear).pack(side="right", padx=4)
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自動捲動", variable=self.follow).pack(
            side="right", padx=8)

        wrap = ttk.Frame(self, style="Card.TFrame")
        wrap.pack(fill="both", expand=True)
        self.text = tk.Text(wrap, bg=CARD, fg=FG, font=MONO, wrap="none",
                            relief="flat", padx=8, pady=6,
                            insertbackground=FG, state="disabled")
        ys = ttk.Scrollbar(wrap, orient="vertical", command=self.text.yview)
        xs = ttk.Scrollbar(wrap, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        for tag, color in (("ok", OK), ("warn", WARN), ("err", ERR),
                           ("info", INFO), ("muted", MUTED)):
            self.text.tag_configure(tag, foreground=color)
        self.text.tag_configure("head", foreground=FG, font=("Consolas", 10, "bold"))
        # ComfyUI 的 ANSI 色票（淺色底，所以不用終端機那組亮色）
        for tag, col in (("ansi_black", "#2b2f36"), ("ansi_red", "#c0392b"),
                         ("ansi_green", "#1e8449"), ("ansi_yellow", "#b07400"),
                         ("ansi_blue", "#1f5fb4"), ("ansi_magenta", "#8e44ad"),
                         ("ansi_cyan", "#0e7490"), ("ansi_white", "#4b5563"),
                         ("ansi_gray", "#8a93a0")):
            self.text.tag_configure(tag, foreground=col)
        self.text.tag_configure("ansi_bold", font=("Consolas", 10, "bold"))
        self._lines = 0          # 自己記行數，不要每次都問 widget
        self.max_lines = 6000    # 超過就從頭砍，Text 太長會拖慢整個介面

    @staticmethod
    def _tag(line):
        s = line.lstrip()
        if s.startswith(("[ERROR]", "[FAIL]", "✗", "fatal", "error:")):
            return "err"
        if s.startswith(("[WARN]", "⚠")):
            return "warn"
        if s.startswith(("[OK]", "✓")):
            return "ok"
        if s.startswith(("[INFO]", "[SKIP]")):
            return "info"
        if s.startswith("=" * 6) or s.startswith("【"):
            return "head"
        return None

    def write(self, line):
        self.write_many((line,))

    def write_many(self, lines):
        """
        一次塞進多行。

        原本是一行一次 write()：每行都 configure(normal) → insert →
        configure(disabled) → see("end")，而 see() 會強迫重算排版。
        更新腳本一次吐幾千行時，介面就是卡在這裡。
        現在整批進一次，捲動也只做一次。
        """
        if not lines:
            return
        t = self.text
        t.configure(state="normal")
        for line in lines:
            tag = self._tag(line)
            t.insert("end", line, (tag,) if tag else ())
        self._lines += sum(l.count("\n") or 1 for l in lines)
        if self._lines > self.max_lines + 1000:
            cut = self._lines - self.max_lines
            t.delete("1.0", f"{cut + 1}.0")
            self._lines = self.max_lines
        t.configure(state="disabled")
        if self.follow.get():
            t.see("end")

    def write_tagged(self, chunks):
        """寫入 [(文字, 標籤tuple), ...]，給 ANSI 彩色輸出用"""
        if not chunks:
            return
        t = self.text
        t.configure(state="normal")
        n = 0
        for text, tags in chunks:
            if not text:
                continue
            t.insert("end", text, tags)
            n += text.count("\n")
        self._lines += n
        if self._lines > self.max_lines + 1000:
            cut = self._lines - self.max_lines
            t.delete("1.0", f"{cut + 1}.0")
            self._lines = self.max_lines
        t.configure(state="disabled")
        if self.follow.get():
            t.see("end")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._lines = 0

    def save(self):
        p = filedialog.asksaveasfilename(
            defaultextension=".log", filetypes=[("紀錄檔", "*.log"), ("全部", "*.*")])
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write(self.text.get("1.0", "end"))

    def set_status(self, msg, kind="muted"):
        color = {"ok": OK, "err": ERR, "warn": WARN, "run": ACCENT}.get(kind, MUTED)
        self.status.configure(text=msg, foreground=color)


# ══════════════════════════ 任務執行 ══════════════════════════

class Runner:
    """一次只跑一個外部任務：pip / git 同時跑會互相破壞"""

    def __init__(self, app):
        self.app = app
        self.proc = None
        self.q = queue.Queue()
        self.busy = False
        self.name = ""

    def start(self, name, argv, pane, cwd=None, on_done=None, env=None):
        if self.busy:
            messagebox.showwarning("還有任務在跑",
                                   f"「{self.name}」還沒結束。\n"
                                   "pip 和 git 同時執行會互相干擾，請等它跑完。")
            return False
        self.busy, self.name = True, name
        pane.clear()
        pane.set_status(f"執行中：{name}", "run")
        pane.write(f"$ {' '.join(argv)}\n\n")
        self.app.set_busy(True, name)
        t = threading.Thread(target=self._work,
                             args=(argv, cwd or APP_DIR, env), daemon=True)
        t.start()
        self.app.after(60, lambda: self._drain(pane, on_done, time.time()))
        return True

    def _work(self, argv, cwd, env):
        e = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                 PYTHONUNBUFFERED="1", COMFY_NO_LAUNCH="1")
        if env:
            e.update(env)
        try:
            flags = 0
            if os.name == "nt":
                flags = subprocess.CREATE_NO_WINDOW
            self.proc = subprocess.Popen(
                argv, cwd=cwd, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, env=e, creationflags=flags)
        except Exception as ex:
            self.q.put(("line", f"[ERROR] 無法啟動：{ex}\n"))
            self.q.put(("done", 1))
            return
        try:
            # 腳本結尾若有 input()（例如 repair_env.py、update.py 的啟動詢問），
            # 餵幾個換行讓它自己結束，不要卡在那裡等人。
            self.proc.stdin.write("\n\n\n")
            self.proc.stdin.flush()
            self.proc.stdin.close()
        except Exception:
            pass
        for line in self.proc.stdout:
            self.q.put(("line", line))
        self.q.put(("done", self.proc.wait()))

    def _drain(self, pane, on_done, t0):
        batch = []
        try:
            while len(batch) < 500:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    batch.append(payload)
                    continue
                else:
                    pane.write_many(batch)
                    batch = []
                    secs = int(time.time() - t0)
                    good = payload == 0
                    pane.write(f"\n{'─' * 60}\n")
                    pane.write(f"{'[OK]' if good else '[ERROR]'} "
                               f"結束碼 {payload}，耗時 {secs // 60}分{secs % 60}秒\n")
                    pane.set_status(
                        f"完成（{secs // 60}分{secs % 60}秒）" if good
                        else f"失敗　結束碼 {payload}", "ok" if good else "err")
                    self.busy, self.proc = False, None
                    self.app.set_busy(False, "")
                    if on_done:
                        on_done(payload)
                    return
        except queue.Empty:
            pass
        pane.write_many(batch)
        # 有東西就跟緊一點，沒東西就放慢，閒置時不要每 60ms 叫醒主執行緒
        self.app.after(60 if batch else 180,
                       lambda: self._drain(pane, on_done, t0))

    def stop(self):
        """中止目前的任務。kill_tree 最多等 20 秒，所以丟背景跑，
        否則按下「中止」之後視窗會凍住。"""
        if not self.proc:
            return
        pid = self.proc.pid
        proc = self.proc

        def work():
            kill_tree(pid)
            try:
                if os.name != "nt":
                    proc.terminate()
            except Exception:
                pass
            return True
        try:
            self.app.bg.submit(work, None, key="killtask")
        except Exception:
            work()      # 介面關閉途中 bg 可能已停，退回同步


# ══════════════════════════ ComfyUI 程序控制 ══════════════════════════

# ══════════════════════════ 用 HTTP 讀 ComfyUI 的 console ══════════════════════════

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")

# ComfyUI 的 ColoredFormatter 用的就是這幾個 SGR 碼（app/logger.py）
ANSI_TAGS = {
    30: "ansi_black", 31: "ansi_red", 32: "ansi_green", 33: "ansi_yellow",
    34: "ansi_blue", 35: "ansi_magenta", 36: "ansi_cyan", 37: "ansi_white",
    90: "ansi_gray", 91: "ansi_red", 92: "ansi_green", 93: "ansi_yellow",
    94: "ansi_blue", 95: "ansi_magenta", 96: "ansi_cyan", 97: "ansi_white",
}


def ansi_split(text, state=None):
    """
    把帶 ANSI 色碼的字串拆成 [(文字, 標籤tuple), ...]。

    state 是跨段落延續的 (顏色標籤, 是否粗體)，因為一個 \x1b[32m
    可能在這一段開啟、下一段才 reset。
    """
    color, bold = state or (None, False)
    out, pos = [], 0
    for m in ANSI_RE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()],
                        tuple(t for t in (color, "ansi_bold" if bold else None) if t)))
        for code in (m.group(1) or "0").split(";"):
            try:
                c = int(code or 0)
            except ValueError:
                continue
            if c == 0:
                color, bold = None, False
            elif c == 1:
                bold = True
            elif c == 22:
                bold = False
            elif c == 39:
                color = None
            elif c in ANSI_TAGS:
                color = ANSI_TAGS[c]
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:],
                    tuple(t for t in (color, "ansi_bold" if bold else None) if t)))
    return out, (color, bold)


class HttpLog:
    """
    直接跟 ComfyUI 的 HTTP 端點要 console 內容。

    為什麼要這樣做，而不是讀檔或接管線：
      1. 管線只有「建立它的程序」讀得到。ComfyUI 如果是你用 run.bat 或
         其他方式開的，這個介面接不到任何輸出 —— 就是你遇到的
         「外部啟動就看不到 console」。
      2. 讀檔案要嘛靠 run.bat 用 Tee 轉寫（只有走 run.bat 才有），
         要嘛讀 user 目錄下的 comfyui.log（那是 logging 的 FileHandler
         寫的，已經被格式化成 [時間][等級] 而且沒有顏色）。
      3. GET /internal/logs/raw 回的是 LogInterceptor 原樣攔下來的
         stdout/stderr，ANSI 色碼原封不動 —— 跟網頁版底部那個
         Logs 面板讀的是同一份資料。

    所以只要 ComfyUI 的連接埠通，不管它是誰啟動的，都看得到、而且有顏色。

    限制：那個緩衝區是 deque，預設只留最後 300 筆。輪詢間隔內如果吐超過
    300 筆就會漏掉中間，這時會標示「略過 N 筆」而不是假裝連續。
    """

    def __init__(self, app, port=8188, host="127.0.0.1"):
        self.app = app
        self.host, self.port = host, port
        self.pane = None
        self._last = None          # 最後看過的 (t, m)
        self._state = (None, False)
        self._stop = True
        self._miss = 0

    def base(self):
        return f"http://{self.host}:{self.port}"

    # ── 背景執行緒 ──
    def fetch(self):
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            self.base() + "/internal/logs/raw",
            headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=4) as r:
            return _json.loads(r.read().decode("utf-8", "replace"))

    def alive(self):
        """用 HTTP 問，比掃程序表準，而且不用 WMI"""
        import urllib.request
        try:
            with urllib.request.urlopen(
                    self.base() + "/system_stats", timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    # ── 主執行緒 ──
    def start(self, pane):
        self.pane = pane
        self._stop = False
        self._tick()

    def stop(self):
        self._stop = True

    def reset(self):
        """重新連線時從頭抓一次"""
        self._last = None
        self._state = (None, False)

    def _tick(self):
        if self._stop:
            return
        self.app.bg.submit(self._poll, self._apply, key="httplog")
        self.app.after(900, self._tick)

    def _poll(self):
        try:
            return self.fetch(), None
        except Exception as ex:
            return None, ex

    def _apply(self, out, err):
        if self._stop or out is None:
            return
        data, ex = out
        if ex is not None or not isinstance(data, dict):
            self._miss += 1
            if self._miss == 3 and self.pane:
                self.pane.set_status("連不上 ComfyUI 的 log 端點", "warn")
            return
        if self._miss and self.pane:
            self.pane.set_status("已連線", "ok")
        self._miss = 0
        entries = data.get("entries") or []
        if not entries:
            return

        if self._last is None:
            new = entries
        else:
            idx = None
            for i in range(len(entries) - 1, -1, -1):
                e = entries[i]
                if (e.get("t"), e.get("m")) == self._last:
                    idx = i
                    break
            if idx is None:
                # 緩衝區滾掉了，中間有斷層
                new = entries
                if self.pane:
                    self.pane.write_many(
                        ["\n[...緩衝區已滾動，中間的輸出來不及取得...]\n"])
            else:
                new = entries[idx + 1:]
        if not new:
            return
        self._last = (new[-1].get("t"), new[-1].get("m"))
        self._render(new)

    def _render(self, entries):
        if not self.pane:
            return
        chunks = []
        for e in entries:
            parts, self._state = ansi_split(e.get("m") or "", self._state)
            chunks.extend(parts)
        self.pane.write_tagged(chunks)


class ServerCtl:
    """ComfyUI 的啟動 / 停止 / 狀態。

    跟 Runner 分開：ComfyUI 是長期執行的，不能跟 pip、git 搶同一個任務鎖。
    輸出收進自己的分頁，不另開 console 視窗。
    """

    MAX_LINES = 4000        # 輸出上限，ComfyUI 跑久了會塞爆記憶體

    def __init__(self, app):
        self.app = app
        self.proc = None
        self.q = queue.Queue()
        self.pane = None
        self.port = 8188
        self.started = None
        self.owned_pid = None       # 這個介面啟動的那一隻（跨重開記得住）
        self.http = HttpLog(app, self.port)
        self._alive_cache = (0.0, False)
        self._owned_alive = False   # 由背景探測維護，主執行緒只讀
        self._busy = False          # 正在停止中，避免重複按

    # ── 狀態 ──
    def own_pid(self):
        """
        我們這隻 ComfyUI 的 PID。這支會被每 1.5 秒的狀態輪詢呼叫，
        所以「絕對不能阻塞」。

        以前這裡會在找不到 self.proc 的時候去跑 SYS.find_comfy()，
        那是 WMI / PowerShell 查詢，一次 300-800ms。
        而「重啟 ComfyUI」或「更新後接管」之後 self.proc 正好是 None
        （接管的是別人啟動的程序），於是每 1.5 秒就在主執行緒卡兩次
        —— 這就是重啟或更新之後才開始卡死的原因。

        現在改成只讀背景探測寫下的旗標，主執行緒零 I/O。
        """
        if self.proc and self.proc.poll() is None:
            return self.proc.pid
        # self.proc 不在了（介面重開 / 接管），用背景探測的結果
        if self.owned_pid and self._owned_alive:
            return self.owned_pid
        return None

    def refresh_owned(self):
        """背景執行緒專用：確認 owned_pid 是不是還活著"""
        if not self.owned_pid or SYS is None:
            self._owned_alive = False
            return False
        try:
            ok = any(p == self.owned_pid
                     for p, c in SYS.find_comfy(COMFY_DIR))
        except Exception:
            ok = False
        self._owned_alive = ok
        return ok

    def http_alive(self):
        """連接埠通不通 —— 這才是 ComfyUI 到底有沒有在跑的正解。

        掃程序表會誤判：節點的 worker 子程序、剛結束但還沒被回收的殼、
        名字裡剛好有 main.py 的別的程式。HTTP 回得了 200 就是真的活著。
        """
        return self.http.alive()

    def external(self):
        """不是這個介面啟動的 ComfyUI（例如之前用 run.bat 開的）。

        ⚠ 背景執行緒專用：裡面是 WMI / PowerShell 查詢，一次 300-800ms。
          主執行緒請用 alive()（讀快取）或 own_pid()（讀旗標）。
        """
        if SYS is None:
            return []
        mine = self.own_pid()
        return [(p, c) for p, c in SYS.find_comfy(COMFY_DIR) if p != mine]

    def alive(self):
        """主執行緒安全：只看旗標與快取，不做任何 I/O。

        需要「當下最準」的答案時（例如關閉前的確認），請在背景呼叫
        http_alive()，不要用這支。
        """
        return bool(self.own_pid()) or self._alive_cache[1]

    def launch_args(self):
        args = ["--reserve-vram", "1", "--enable-manager",
                "--listen", "0.0.0.0", "--enable-cors-header", "*"]
        if CFG:
            try:
                v = CFG.read_assignments(os.path.join(APP_DIR, "comfy_env.py"),
                                         {"LAUNCH_ARGS"})
                args = list(v.get("LAUNCH_ARGS", args))
            except Exception:
                pass
        return args

    # ── 動作 ──
    def start(self, pane):
        if self.own_pid():
            messagebox.showinfo("已經在執行", f"ComfyUI 正在跑（PID {self.own_pid()}）")
            return
        # 用背景探測的結果，不要在按鈕回呼裡跑 WMI
        if self._alive_cache[1] and not messagebox.askyesno(
                "已經有 ComfyUI 在跑",
                f"連接埠 {self.port} 已經有 ComfyUI 在回應，"
                "但不是由這個介面啟動的。\n\n"
                "同時跑兩份會搶 VRAM 和連接埠。\n仍要再啟動一份嗎？"):
            return
        args = self.launch_args()
        self.port = SYS.port_from_args(args) if SYS else 8188
        self.pane = pane
        pane.clear()
        py = self.app.need_venv()
        if not py:
            return
        argv = [py, "main.py"] + args
        pane.write(f"$ {' '.join(argv)}\n\n")
        pane.write("[INFO] 啟動中，連得上之後會接上 ComfyUI 的 console…\n")
        try:
            # 旗標的選擇（踩過坑，不要再改）：
            #
            #   CREATE_NO_WINDOW         給它一個 console，但不顯示。
            #   CREATE_NEW_PROCESS_GROUP 自成一組，父程序收到 Ctrl+C
            #                            不會轉送過去。
            #
            #   不要加 DETACHED_PROCESS！它和 CREATE_NO_WINDOW 互斥：
            #   DETACHED 的意思是「不繼承父程序的 console」，而 ComfyUI 是
            #   console 應用程式，拿不到 console 時它會自己配一個新的 ——
            #   畫面上就冒出一個黑窗。
            #
            #   而且根本不需要它：Windows 不會因為父程序結束就連帶殺掉子
            #   程序。之前會被拖死是因為「管線」，而 stdout 現在接 DEVNULL
            #   （輸出走 HTTP），所以介面關掉 ComfyUI 照樣跑。
            flags = 0
            if os.name == "nt":
                flags = (subprocess.CREATE_NO_WINDOW
                         | subprocess.CREATE_NEW_PROCESS_GROUP)
            self.proc = subprocess.Popen(
                argv, cwd=COMFY_DIR,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                         PYTHONUNBUFFERED="1"),
                creationflags=flags,
                start_new_session=(os.name != "nt"))
        except Exception as ex:
            pane.write(f"[ERROR] 啟動失敗：{ex}\n")
            pane.set_status("啟動失敗", "err")
            return
        self.started = time.time()
        self.owned_pid = self.proc.pid
        self.save_state()
        pane.set_status(f"啟動中　PID {self.proc.pid}", "run")
        self.attach(pane)
        self.app.refresh_server_state()

    # ── 狀態檔：關掉介面再打開時認得回來 ──
    def state_path(self):
        return os.path.join(APP_DIR, "env_state", "manager_state.json")

    def save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_path()), exist_ok=True)
            with open(self.state_path(), "w", encoding="utf-8") as f:
                json.dump({"pid": self.owned_pid, "port": self.port,
                           "started": self.started}, f)
        except OSError:
            pass

    def load_state(self):
        try:
            with open(self.state_path(), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def attach(self, pane):
        """接上 HTTP log 串流（不管 ComfyUI 是誰啟動的）"""
        self.pane = pane
        self.http.port = self.port
        self.http.reset()
        self.http.start(pane)

    def adopt(self, pane):
        """
        介面重新開啟時，把還在跑的 ComfyUI 接回來。

        判斷依據是「連接埠有沒有回應」，不是 PID。
        PID 可能被系統回收給別的程式，而且外部啟動的 ComfyUI
        本來就不在我們的紀錄裡。HTTP 問得到就是活的。
        """
        st = self.load_state()
        self.port = st.get("port") or self.port
        if not self.http_alive():
            return False
        self.started = st.get("started")
        pid = st.get("pid")
        if pid and SYS and any(p == pid for p, c in SYS.find_comfy(COMFY_DIR)):
            self.owned_pid = pid        # 真的是上次我們開的那隻
        pane.write(f"[INFO] 接回執行中的 ComfyUI（連接埠 {self.port}）\n\n")
        self.attach(pane)
        return True

    def _pump(self):
        try:
            for line in self.proc.stdout:
                self.q.put(line)
        except Exception:
            pass
        self.q.put(None)

    def _drain(self):
        batch = []
        try:
            while len(batch) < 500:
                line = self.q.get_nowait()
                if line is None:
                    self.pane.write_many(batch)
                    batch = []
                    rc = self.proc.poll() if self.proc else "?"
                    self.pane.write(f"\n{'-' * 60}\n[INFO] ComfyUI 已結束"
                                    f"（結束碼 {rc}）\n")
                    self.pane.set_status("已停止", "muted")
                    self.proc = None
                    self.app.refresh_server_state()
                    return
                batch.append(line)
        except queue.Empty:
            pass
        self.pane.write_many(batch)       # 行數上限由 LogPane 自己管
        if self.proc:
            self.app.after(100 if batch else 250, self._drain)

    def _trim(self):
        """保留給舊呼叫點；實際裁切已經由 LogPane.write_many 處理。

        原本這支每寫一行就被呼叫一次，而 index("end-1c") 是 O(行數) 的查詢，
        一次 200 行就是 200 次整份掃描——那是 ComfyUI 吐 log 時卡頓的主因。
        """
        self.pane.max_lines = min(self.pane.max_lines, self.MAX_LINES)

    def stop(self, ask=True, on_done=None):
        # 不要在這裡呼叫 external()（WMI，300-800ms）。
        # 自己啟動的用 owned_pid；接管的情況交給背景去找。
        targets = []
        if self.own_pid():
            targets.append(self.own_pid())
        if not targets and self._alive_cache[1]:
            self.kill_unknown_async(ask=ask, on_done=on_done)
            return
        if not targets:
            messagebox.showinfo("沒有在執行", "找不到正在跑的 ComfyUI")
            return
        if ask and not messagebox.askyesno(
                "終止 ComfyUI",
                f"要終止這 {len(targets)} 個程序嗎？\n\n"
                + "\n".join(f"　PID {p}" for p in targets)
                + "\n\n會連同子程序一起結束（不然會留下孤兒程序佔著 VRAM）。\n"
                  "正在算圖的工作會中斷。"):
            return
        self.kill_async(targets, on_done=on_done)

    def kill_unknown_async(self, ask=True, on_done=None):
        """連接埠通但沒有已知 PID（外部啟動 / 介面重開過）：背景去找再殺"""
        if ask and not messagebox.askyesno(
                "終止 ComfyUI",
                "偵測到 ComfyUI 正在執行，但不是由這個介面啟動的。\n\n"
                "要找出它的程序並終止嗎？\n"
                "會連同子程序一起結束，正在算圖的工作會中斷。"):
            return
        pane = self.pane or self.app.pane("ComfyUI")
        pane.set_status("尋找程序中…", "warn")

        def find():
            return [p for p, c in SYS.find_comfy(COMFY_DIR)] if SYS else []

        def got(pids, err):
            if err or not pids:
                pane.set_status("找不到程序", "warn")
                pane.write("[WARN] 連接埠通但找不到對應的程序，"
                           "可能是別的程式佔用，或權限不足\n")
                if on_done:
                    on_done(False)
                return
            self.kill_async(pids, on_done=on_done)

        self.app.bg.submit(find, got, key="findkill")

    def kill_async(self, targets, on_done=None):
        """
        殺程序 + 等它真的消失，全部在背景做。

        kill_tree 每個最多等 20 秒（要確認程序真的死透，taskkill 回來
        不代表結束），放在主執行緒上就是整個視窗凍住 —— 按「終止」或
        「重啟」之後的卡死就是這個。
        """
        if self._busy:
            return
        self._busy = True
        pane = self.pane or self.app.pane("ComfyUI")
        pane.set_status("停止中…", "warn")
        pane.write(f"[INFO] 終止 {len(targets)} 個程序，等待檔案句柄釋放…\n")
        self.http.stop()

        def work():
            out = []
            for p in targets:
                out.append((p, kill_tree(p)))
            # 等到連接埠真的不通，Windows 才算把 .pyd / .dll 放掉
            dead = self.wait_dead_blocking()
            return out, dead

        def done(res, err):
            self._busy = False
            if err or res is None:
                pane.write(f"[ERROR] 終止失敗：{err}\n")
                pane.set_status("終止失敗", "err")
            else:
                out, dead = res
                for p, ok in out:
                    pane.write(f"[{'OK' if ok else 'FAIL'}]   終止 PID {p}\n")
                self.proc = None
                self.owned_pid = None
                self._owned_alive = False
                self._alive_cache = (0.0, False)
                pane.write("[OK]   ComfyUI 已關閉\n" if dead
                           else "[WARN] 還偵測得到 ComfyUI，請用工作管理員確認\n")
                pane.set_status("已終止" if dead else "可能還在執行",
                                "muted" if dead else "warn")
                self.save_state()
            self.app.refresh_server_state()
            if on_done:
                on_done(bool(res) and res[1])

        self.app.bg.submit(work, done, key="killcomfy")

    def wait_dead_blocking(self, timeout=20):
        """背景執行緒專用：等到連接埠不通為止"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not self.http_alive():
                time.sleep(1.0)      # 多等一下讓 Windows 放掉檔案句柄
                return not self.http_alive()
            time.sleep(0.4)
        return not self.http_alive()

    def restart(self, pane):
        """停止 → 等真的死透 → 再啟動。全程不阻塞主執行緒。"""
        if not self.alive():
            self.start(pane)
            return
        self.stop(ask=False,
                  on_done=lambda ok: self.app.after(1500,
                                                    lambda: self.start(pane)))


# ══════════════════════════ 設定分頁 ══════════════════════════

def split_args(raw, label="啟動參數"):
    """
    把一行命令列參數拆成 token。

    不能直接用 shlex.split()（posix 模式）：它會把反斜線當跳脫字元，
    所以 --output-directory C:\\My\\Outputs 會變成 C:MyOutputs。
    Windows 路徑一定會踩到。

    改用 posix=False —— 它不處理反斜線，但引號會留在 token 裡
    （"*" 會變成字面上的 "*"），所以最後自己把成對的外層引號剝掉。
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        toks = shlex.split(raw, posix=False)
    except ValueError as ex:
        raise ValueError(f"{label}：引號沒有成對（{ex}）")
    out = []
    for t in toks:
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
            t = t[1:-1]
        out.append(t)
    return out


ENV_FIELDS = [
    ("__ROOT_FILE__", "ComfyUI 本體路徑", "path",
     "存進 comfy_root.txt（更新包不會覆蓋它）。留空 = 自動尋找。"
     "相對路徑以腳本目錄為基準，可攜碟換電腦也不會失效。改完要重開介面"),
    ("PY_MAJOR_MINOR", "Python 版本", "tuple", "鎖定的 Python 主版本，例 3.12"),
    ("CUDA_TAG", "CUDA 標籤", "str", "例 cu130；TORCH_INDEX 會自動跟著變"),
    ("TORCH_PINS", "PyTorch 精確版本", "dict", "套件 = 版本，不接受更高版"),
    ("FLOOR_PINS", "最低版本下限", "dict", "套件 = 最低版本，被降版會自動升回"),
    ("BLOCKLIST", "黑名單套件", "dict", "套件 = 原因，裝到就移除"),
    ("USE_CONSTRAINTS", "啟用約束檔", "bool", "讓任何 requirements 都無法換掉 torch"),
    ("EXCLUDE_NODES", "排除更新的節點", "dict", "資料夾名 = 原因，依賴仍會檢查"),
    ("NODE_PINS", "節點固定版本", "dict", "資料夾名 = commit 或 tag"),
    ("PIN_CORE", "核心鎖版", "str_none", "填 commit 或 tag；留空 = 跟著上游"),
    ("AUTO_REINSTALL_WHEELS", "wheel 被覆蓋時自動重裝", "bool", ""),
    ("PLACEHOLDER_GLOBS", "空佔位檔樣式", "list", "被刪掉時自動還原，避免擋住更新"),
    ("EXTRA_PACKAGES", "額外套件", "list", "install.py 在 torch 之後安裝"),
    ("LAUNCH_ARGS", "ComfyUI 啟動參數", "argline",
     "整行照著命令列寫，例 --reserve-vram 1 --listen 127.0.0.1"),
]

UPD_FIELDS = [
    ("CORE_UPDATE_MODE", "本體更新方式", "choice:ff,rebase",
     "ff = 只接受 fast-forward；rebase = 核心有自訂 patch commit 時用"),
    ("CORE_AUTO_STASH", "自動 stash 未提交的改動", "bool",
     "關閉時只點名檔案並跳過更新，不碰你的改動"),
]


class ConfigTab(ttk.Frame):
    def __init__(self, master, app, defer=False):
        super().__init__(master, padding=10)
        self.app = app
        self.widgets = {}
        self._defer = defer

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="環境參數", style="H.TLabel").pack(side="left")
        ttk.Button(bar, text="重新讀取", command=self.load).pack(side="right")
        ttk.Button(bar, text="儲存到檔案", style="Go.TButton",
                   command=self.save).pack(side="right", padx=6)
        self.msg = ttk.Label(bar, text="", style="Muted.TLabel")
        self.msg.pack(side="right", padx=10)

        canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.body = ttk.Frame(canvas)
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        win = canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>",
                       lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width - 4))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(-int(e.delta / 120), "units"))

        self._ready = False
        if not defer:
            self.build()

    def build(self):
        """真正把幾十個欄位建出來並讀檔。延後到第一次切過來才做。"""
        if self._ready:
            return
        self._ready = True
        self._section("comfy_env.py", ENV_FIELDS, "env")
        self._section("update.py", UPD_FIELDS, "upd")
        self.load()

    def _section(self, title, fields, prefix):
        card = ttk.Frame(self.body, style="Card.TFrame", padding=12)
        card.pack(fill="x", pady=(0, 12))
        ttk.Label(card, text=title, style="Card.TLabel",
                  font=UI_B).grid(row=0, column=0, columnspan=2, sticky="w",
                                  pady=(0, 8))
        for i, (key, label, kind, hint) in enumerate(fields, start=1):
            ttk.Label(card, text=label, style="Card.TLabel").grid(
                row=i * 2, column=0, sticky="nw", padx=(0, 12), pady=3)
            w = self._field(card, kind)
            w["frame"].grid(row=i * 2, column=1, sticky="ew", pady=3)
            if hint:
                ttk.Label(card, text=hint, style="CardMuted.TLabel",
                          font=("Microsoft JhengHei UI", 8)).grid(
                    row=i * 2 + 1, column=1, sticky="w", pady=(0, 4))
            self.widgets[(prefix, key)] = (kind, w)
        card.columnconfigure(1, weight=1)

    def _field(self, parent, kind):
        f = ttk.Frame(parent, style="Card.TFrame")
        if kind == "bool":
            var = tk.BooleanVar()
            ttk.Checkbutton(f, variable=var, text="啟用",
                            style="Card.TCheckbutton").pack(anchor="w")
            return {"frame": f, "var": var}
        if kind.startswith("choice:"):
            var = tk.StringVar()
            cb = ttk.Combobox(f, textvariable=var, state="readonly",
                              values=kind.split(":", 1)[1].split(","), width=18)
            cb.pack(anchor="w")
            return {"frame": f, "var": var}
        if kind == "path":
            # 路徑用打的很容易錯（反斜線一多更難檢查），給個選目錄的按鈕
            var = tk.StringVar()
            ttk.Entry(f, textvariable=var, font=MONO).pack(
                side="left", fill="x", expand=True)

            def pick():
                d = filedialog.askdirectory(
                    title="選擇 ComfyUI 本體資料夾（裡面要有 main.py）",
                    initialdir=var.get().strip() or COMFY_DIR)
                if not d:
                    return
                d = os.path.normpath(d)
                if not os.path.exists(os.path.join(d, "main.py")):
                    if not messagebox.askyesno(
                            "這裡沒有 main.py",
                            f"{d}\n\n看起來不像 ComfyUI 本體資料夾。\n"
                            "還是要用這個路徑嗎？"):
                        return
                # 離得近就存成相對路徑，可攜碟換電腦 / 磁碟代號變了也不會失效
                try:
                    rel = os.path.relpath(d, APP_DIR)
                    if not rel.startswith(".." + os.sep + ".." + os.sep):
                        d = rel
                except ValueError:
                    pass          # 跨磁碟，只能用絕對路徑
                var.set(d)

            ttk.Button(f, text="瀏覽…", command=pick, width=7).pack(
                side="left", padx=(6, 0))
            return {"frame": f, "var": var}
        if kind == "argline":
            # 一行輸入。原本一個 token 一列（--listen 一列、127.0.0.1 一列）
            # 很容易漏掉或多打一列，而且跟你在命令列看到的樣子對不起來。
            var = tk.StringVar()
            ttk.Entry(f, textvariable=var, font=MONO).pack(fill="x")
            return {"frame": f, "var": var}
        if kind in ("dict", "list"):
            t = tk.Text(f, height=4, bg=CARD, fg=FG, font=MONO, wrap="none",
                        relief="solid", borderwidth=1, padx=6, pady=4)
            t.pack(fill="x")
            return {"frame": f, "text": t}
        var = tk.StringVar()
        ttk.Entry(f, textvariable=var).pack(fill="x")
        return {"frame": f, "var": var}

    # ── 值 ↔ 介面 ──
    @staticmethod
    def _to_ui(kind, val):
        if kind == "dict":
            return "".join(f"{k} = {v}\n" for k, v in (val or {}).items())
        if kind == "list":
            return "".join(f"{v}\n" for v in (val or []))
        if kind == "argline":
            # 含空白或特殊字元的參數要加引號，貼回命令列也能直接用
            return " ".join(
                (f'"{v}"' if (not str(v) or any(c in str(v) for c in ' "\''))
                 else str(v))
                for v in (val or []))
        if kind == "tuple":
            return ".".join(str(x) for x in (val or ()))
        if kind in ("str_none", "path"):
            return "" if val is None else str(val)
        return val

    @staticmethod
    def _from_ui(kind, raw, label):
        if kind == "dict":
            out = {}
            for ln in raw.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                if "=" not in ln:
                    raise ValueError(f"{label}：「{ln}」缺少等號，格式是 名稱 = 值")
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip()
            return out
        if kind == "list":
            return [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if kind == "argline":
            return split_args(raw, label)
        if kind == "tuple":
            parts = [p for p in str(raw).replace(",", ".").split(".") if p.strip()]
            if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
                raise ValueError(f"{label}：要像 3.12 這樣的兩段數字")
            return tuple(int(p) for p in parts)
        if kind in ("str_none", "path"):
            return raw.strip().strip('"') or None
        return raw

    def load(self):
        if not self._ready:
            return
        if CFG is None:
            self.msg.configure(text="缺少 comfy_config_io.py", foreground=ERR)
            return
        for prefix, fname, fields in (("env", "comfy_env.py", ENV_FIELDS),
                                      ("upd", "update.py", UPD_FIELDS)):
            path = os.path.join(APP_DIR, fname)
            if not os.path.exists(path):
                continue
            try:
                vals = CFG.read_assignments(path, {k for k, *_ in fields})
            except CFG.ConfigError as ex:
                self.msg.configure(text=str(ex), foreground=ERR)
                continue
            for key, label, kind, hint in fields:
                if key == "__ROOT_FILE__":
                    try:
                        import comfy_env as _e
                        v = _e.read_root_file()
                    except Exception:
                        v = ""
                    self.widgets[(prefix, key)][1]["var"].set(v)
                    continue
                if key not in vals:
                    continue
                kd, w = self.widgets[(prefix, key)]
                v = self._to_ui(kd, vals[key])
                if "text" in w:
                    w["text"].delete("1.0", "end")
                    w["text"].insert("1.0", v)
                else:
                    w["var"].set(v)
        self.msg.configure(text="已從檔案讀取", foreground=MUTED)

    def save(self):
        if not self._ready:
            return
        if CFG is None:
            return
        root_file_val = None
        try:
            batches = {}
            for prefix, fname, fields in (("env", "comfy_env.py", ENV_FIELDS),
                                          ("upd", "update.py", UPD_FIELDS)):
                ups = {}
                for key, label, kind, hint in fields:
                    kd, w = self.widgets[(prefix, key)]
                    raw = (w["text"].get("1.0", "end") if "text" in w
                           else w["var"].get())
                    if key == "__ROOT_FILE__":
                        root_file_val = raw.strip().strip('"')
                        continue
                    if kd == "bool":
                        ups[key] = bool(w["var"].get())
                    else:
                        ups[key] = self._from_ui(kd, raw, label)
                batches[os.path.join(APP_DIR, fname)] = ups
        except ValueError as ex:
            messagebox.showerror("格式錯誤", str(ex))
            return

        changed = []
        # 本體路徑寫進 comfy_root.txt，不是寫進 comfy_env.py ——
        # 這樣下次換新版的腳本蓋過來，設定不會被洗掉。
        if root_file_val is not None:
            try:
                import comfy_env as _e
                import importlib
                importlib.reload(_e)
                if (root_file_val or "") != (_e.read_root_file() or ""):
                    ok, msg = _e.write_root_file(root_file_val)
                    if ok:
                        changed.append(f"comfy_root.txt：{root_file_val or '（清除，改回自動尋找）'}")
                    else:
                        messagebox.showerror("寫入失敗", msg)
                        return
            except Exception as ex:
                messagebox.showerror("寫入失敗", f"comfy_root.txt：{ex}")
                return

        for path, ups in batches.items():
            if not os.path.exists(path):
                continue
            try:
                # 只送檔案裡真的有的變數，避免 update.py 沒有那些欄位時報錯
                have = CFG.read_assignments(path, set(ups))
                changed += [f"{os.path.basename(path)}：{n}" for n in
                            CFG.write_assignments(
                                path, {k: v for k, v in ups.items() if k in have})]
            except CFG.ConfigError as ex:
                messagebox.showerror("寫入失敗", str(ex))
                return
        if changed:
            self.msg.configure(text=f"已儲存 {len(changed)} 項（已留 .bak 備份）",
                               foreground=OK)
            if any("comfy_root" in c or "COMFY_ROOT" in c for c in changed):
                changed.append("⚠ 改了本體路徑，請重新開啟這個介面才會生效")
            messagebox.showinfo("已儲存", "變更：\n  " + "\n  ".join(changed)
                                + "\n\n原檔已備份為 .bak_時間")
        else:
            self.msg.configure(text="沒有任何變更", foreground=MUTED)


# ══════════════════════════ 節點分頁 ══════════════════════════

class NodesTab(ttk.Frame):
    def __init__(self, master, app, defer=False):
        super().__init__(master, padding=10)
        self.app = app
        self._ready = not defer
        self._rows = []

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="自訂節點", style="H.TLabel").pack(side="left")
        ttk.Button(bar, text="重新掃描", command=self.refresh).pack(side="right")
        ttk.Button(bar, text="停用選取", command=lambda: self.toggle(False)
                   ).pack(side="right", padx=4)
        ttk.Button(bar, text="啟用選取", command=lambda: self.toggle(True)
                   ).pack(side="right", padx=4)

        add = ttk.Frame(self, style="Card.TFrame", padding=10)
        add.pack(fill="x", pady=(0, 8))
        ttk.Label(add, text="安裝節點", style="Card.TLabel",
                  font=UI_B).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(add, text="貼上 git 網址後按 Enter。支援 使用者/倉庫、"
                            "/tree/分支、/commit/雜湊、git@ 形式；"
                            "clone 後自動裝它的 requirements.txt",
                  style="CardMuted.TLabel",
                  font=("Microsoft JhengHei UI", 8)).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))
        self.url = tk.StringVar()
        e = ttk.Entry(add, textvariable=self.url, font=MONO)
        e.grid(row=2, column=0, sticky="ew", padx=(0, 6))
        e.bind("<Return>", lambda _ev: self.install())
        self.nodeps = tk.BooleanVar(value=False)
        ttk.Checkbutton(add, text="不裝依賴", variable=self.nodeps).grid(
            row=2, column=1, padx=(0, 6))
        self.ignore_bl = tk.BooleanVar(value=False)
        ttk.Checkbutton(add, text="忽略黑名單", variable=self.ignore_bl).grid(
            row=2, column=2, padx=(0, 6))
        ttk.Button(add, text="安裝", style="Go.TButton",
                   command=self.install).grid(row=2, column=3)
        add.columnconfigure(0, weight=1)

        cols = ("state", "name", "ver", "src", "note")
        self.tree = ttk.Treeview(self, columns=cols, show="headings",
                                 selectmode="extended", height=14)
        for c, t, w in (("state", "狀態", 60), ("name", "節點", 300),
                        ("ver", "commit", 90), ("src", "來源", 90),
                        ("note", "備註", 260)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        ys = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side="top", fill="both", expand=True)
        self.tree.tag_configure("off", foreground=MUTED)
        self.tree.tag_configure("bad", foreground=ERR)

        box = ttk.Frame(self, style="Card.TFrame", padding=10)
        box.pack(fill="x", pady=(10, 0))
        ttk.Label(box, text="版本備份", style="Card.TLabel",
                  font=UI_B).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(box, text="備份存的是每個節點的 git commit 與啟停狀態（幾十 KB），"
                            "不是複製整個資料夾", style="CardMuted.TLabel",
                  font=("Microsoft JhengHei UI", 8)).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(box, text="說明", style="Card.TLabel").grid(row=2, column=0)
        self.note = tk.StringVar()
        ttk.Entry(box, textvariable=self.note, width=30).grid(row=2, column=1,
                                                              sticky="ew", padx=6)
        ttk.Button(box, text="建立備份", command=self.save).grid(row=2, column=2)
        ttk.Label(box, text="還原自", style="Card.TLabel").grid(
            row=3, column=0, pady=(8, 0))
        self.pick = ttk.Combobox(box, state="readonly", width=46)
        self.pick.grid(row=3, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Button(box, text="比對差異", command=self.diff).grid(
            row=3, column=2, pady=(8, 0))
        ttk.Button(box, text="執行還原", style="Danger.TButton",
                   command=self.restore).grid(row=3, column=3, padx=6, pady=(8, 0))
        box.columnconfigure(1, weight=1)

        if not defer:
            self.refresh()

    def build(self):
        if self._ready:
            return
        self._ready = True
        self.refresh()

    def _pane(self):
        return self.app.pane("節點")

    def install(self):
        """貼一個 git 網址就安裝節點"""
        url = self.url.get().strip()
        if not url:
            messagebox.showinfo("要裝哪一個？", "請先貼上節點的 git 網址。")
            return
        if NB is None:
            messagebox.showerror("缺少檔案", "找不到 comfy_nodes_backup.py")
            return
        _u, name, _ref, err = NB.parse_url(url)
        if err:
            messagebox.showerror("網址看不懂", err)
            return
        def go():
            py = (self.app.venv_py() if not self.nodeps.get()
                  else (self.app.venv_py() or host_py() or sys.executable))
            if not py:
                self.app.need_venv()
                return
            argv = [py, "comfy_nodes_backup.py", "add", url]
            if self.nodeps.get():
                argv.append("--no-deps")
            if self.ignore_bl.get():
                argv.append("--force")
            pane = self._pane()
            pane.clear()
            self.app.nb.select(self.app.panes["節點"].master)
            self._start(argv, name, pane)

        # 要裝依賴就得動 site-packages，ComfyUI 開著的話 .pyd 被鎖住
        if self.nodeps.get():
            go()
        else:
            self.app.run_when_stopped("安裝節點", go)

    def _start(self, argv, name, pane):
        self.app.runner.start(
            f"安裝 {name}", argv, pane,
            on_done=lambda rc: (self.url.set("") if rc == 0 else None,
                                self.refresh(force=True)))

    def refresh(self, force=False):
        """
        掃描丟到背景。

        NB.scan() 會對每個節點跑 git（拿 commit、看有沒有本地改動），
        四十個節點就是四十次子程序，在主執行緒上做就是好幾秒的凍結。
        結果有快取，所以切來切去不會一直重掃；改動過節點才 force。
        """
        if NB is None:
            self.tree.delete(*self.tree.get_children())
            self.tree.insert("", "end", values=("", "缺少節點後端模組",
                                                "", "", ""))
            return
        if force:
            self.app.invalidate_nodes()
        cached = None if force else self.app.nodes_snapshot()
        if cached is not None:
            self._fill(cached, NB.backups())
            return
        if not self.tree.get_children():
            self.tree.insert("", "end", values=("", "掃描中…", "", "", ""))
        self.app.bg.submit(
            lambda: (self.app._nodes_cached(force=force), NB.backups()),
            self._refresh_done, key="nodes")

    def _refresh_done(self, out, err):
        if err or out is None:
            self.tree.delete(*self.tree.get_children())
            self.tree.insert("", "end", values=("", f"掃描失敗：{err}",
                                                "", "", ""))
            return
        self._fill(*out)

    def _fill(self, nodes, rows):
        self.tree.delete(*self.tree.get_children())
        for n in nodes:
            tags = []
            note = []
            if n["disabled"]:
                tags.append("off")
            if not n["valid"]:
                tags.append("bad")
                note.append("沒有 __init__.py，ComfyUI 載不起來")
            if n.get("dirty"):
                note.append("有本地改動")
            self.tree.insert("", "end", values=(
                "停用" if n["disabled"] else "啟用", n["name"],
                (n.get("commit") or "")[:8],
                "git" if n["git"] else "手動",
                "　".join(note)), tags=tags)
        self.pick["values"] = [f"{t}　{c} 個節點　{note}　[{f}]"
                               for f, p, t, note, c in rows]
        self._rows = rows
        if rows:
            self.pick.current(0)

    def _selected_names(self):
        return [self.tree.item(i, "values")[1] for i in self.tree.selection()]

    def toggle(self, enable):
        names = self._selected_names()
        if not names:
            messagebox.showinfo("沒有選取", "先在清單裡選一個或多個節點")
            return
        verb = "啟用" if enable else "停用"
        if not messagebox.askyesno(f"{verb}節點",
                                   f"要{verb}這 {len(names)} 個節點嗎？\n\n"
                                   + "\n".join("　" + n for n in names[:12])
                                   + ("\n　…" if len(names) > 12 else "")
                                   + "\n\n節點會在 custom_nodes 和 "
                                     "custom_nodes_disabled 之間搬移，\n"
                                     "改完要重啟 ComfyUI 才生效。"):
            return
        pane = self._pane()
        pane.clear()
        for n in names:
            ok, msg = NB.set_enabled(n, enable)
            pane.write(f"[{'OK' if ok else 'FAIL'}]   {n}：{verb} — {msg}\n")
        pane.write(f"\n完成。重啟 ComfyUI 後生效。\n")
        pane.set_status(f"已{verb} {len(names)} 個節點", "ok")
        self.refresh(force=True)

    def save(self):
        if NB is None:
            return
        p = NB.save(self.note.get().strip())
        self.note.set("")
        self.refresh(force=True)
        pane = self._pane()
        pane.clear()
        pane.write(f"已建立備份：{p}\n\n")
        pane.set_status("備份完成", "ok")
        # 列出內容用剛才那次掃描的快取，不要再對每個節點跑一次 git
        def _list(out, err):
            ns = out or []
            pane.write_many([
                f"  {'停用' if n['disabled'] else '啟用'}  {n['name']:40}"
                f" {(n.get('commit') or '-')[:8]}\n" for n in ns])
        self.app.bg.submit(lambda: self.app._nodes_cached(), _list)

    def _picked(self):
        i = self.pick.current()
        if i < 0 or not getattr(self, "_rows", None):
            messagebox.showinfo("沒有備份", "先建立一個備份")
            return None
        return self._rows[i][1]

    def diff(self):
        p = self._picked()
        if not p:
            return
        pane = self._pane()
        pane.clear()
        pane.write(f"比對：{os.path.basename(p)}\n\n")
        for action, name, why in NB.plan(p):
            pane.write(f"  [{action:8}] {name:40} {why}\n")
        pane.set_status("比對完成（沒有做任何變更）", "ok")

    def restore(self):
        p = self._picked()
        if not p:
            return
        acts = [a for a in NB.plan(p) if a[0] not in ("ok", "extra", "manual")]
        if not acts:
            messagebox.showinfo("不用還原", "目前狀態已經和備份一致")
            return
        if not messagebox.askyesno(
                "執行還原",
                f"要套用 {len(acts)} 項變更嗎？\n\n"
                + "\n".join(f"　[{a}] {n} {w}" for a, n, w in acts[:12])
                + ("\n　…" if len(acts) > 12 else "")
                + "\n\n節點會被 git reset --hard 到備份當時的 commit，\n"
                  "該節點未提交的本地改動會消失。"):
            return
        pane = self._pane()
        pane.clear()
        pane.write(f"還原：{os.path.basename(p)}\n\n")
        ok, fail = NB.restore(p, log=lambda s: pane.write(s + "\n"))
        pane.write(f"\n完成 {ok}　失敗 {fail}\n重啟 ComfyUI 後生效。\n")
        pane.set_status(f"還原完成（{ok} 成功 / {fail} 失敗）",
                        "ok" if not fail else "warn")
        self.refresh(force=True)


# ══════════════════════════ 紀錄檔分頁 ══════════════════════════

class LogsTab(ttk.Frame):
    def __init__(self, master, app, defer=False):
        super().__init__(master, padding=10)
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="歷史紀錄檔", style="H.TLabel").pack(side="left")
        ttk.Button(bar, text="重新整理", command=self.refresh).pack(side="right")
        ttk.Button(bar, text="開啟資料夾", command=self.open_dir).pack(
            side="right", padx=6)

        split = ttk.Frame(self)
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split)
        left.pack(side="left", fill="y")
        self.lb = tk.Listbox(left, width=42, bg=CARD, fg=FG, font=MONO,
                             relief="solid", borderwidth=1,
                             selectbackground="#dbe5fb", selectforeground=FG,
                             activestyle="none")
        self.lb.pack(side="left", fill="y", expand=True)
        ttk.Scrollbar(left, orient="vertical", command=self.lb.yview).pack(
            side="left", fill="y")
        self.lb.bind("<<ListboxSelect>>", lambda e: self.show())

        self.view = LogPane(split)
        self.view.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self._ready = not defer
        if not defer:
            self.refresh()

    def build(self):
        if self._ready:
            return
        self._ready = True
        self.refresh()

    def _dirs(self):
        return [os.path.join(APP_DIR, d) for d in ("update_logs", "node_backups")]

    def refresh(self):
        self.lb.delete(0, "end")
        self.files = []
        for d in self._dirs():
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d), reverse=True):
                if f.endswith((".log", ".txt", ".json")):
                    p = os.path.join(d, f)
                    self.files.append(p)
                    size = os.path.getsize(p)
                    self.lb.insert("end", f"{f}　{size // 1024}KB")
        for f in sorted(os.listdir(APP_DIR), reverse=True):
            if f.startswith("comfy_audit_") and f.endswith((".txt", ".json")):
                self.files.append(os.path.join(APP_DIR, f))
                self.lb.insert("end", f)
        if not self.files:
            self.lb.insert("end", "（還沒有紀錄檔）")

    def show(self):
        sel = self.lb.curselection()
        if not sel or sel[0] >= len(self.files):
            return
        p = self.files[sel[0]]
        self.view.clear()
        self.view.set_status(os.path.basename(p))
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    self.view.write(line)
        except OSError as ex:
            self.view.write(f"[ERROR] 無法讀取：{ex}\n")

    def open_dir(self):
        d = os.path.join(APP_DIR, "update_logs")
        os.makedirs(d, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(d)
            else:
                subprocess.Popen(["xdg-open", d])
        except Exception as ex:
            messagebox.showerror("無法開啟", str(ex))


# ══════════════════════════ 主視窗 ══════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ComfyUI 環境管理")
        self.geometry("1180x780")
        self.minsize(980, 640)
        apply_theme(self)
        self.runner = Runner(self)
        self.server = ServerCtl(self)
        self.panes = {}
        self.bg = Bg(self)
        self._nodes, self._nodes_at = [], 0.0      # 節點掃描快取
        self._ext, self._listening = [], False     # 外部程序/連接埠快取
        self._ext_at = 0.0
        self._built = set()                        # 已經填好內容的分頁

        self._header()
        body = ttk.Frame(self, padding=(10, 0, 10, 10))
        body.pack(fill="both", expand=True)
        self._sidebar(body)

        self.nb = ttk.Notebook(body)
        self.nb.pack(side="left", fill="both", expand=True, padx=(10, 0))
        # 分頁內容延後到第一次切過去才建。
        # ConfigTab 要 AST 解析 comfy_env.py、NodesTab 要逐一 git 掃節點、
        # LogsTab 要列目錄——全部在開窗時做完，就是「打開很慢」。
        self.config_tab = ConfigTab(self.nb, self, defer=True) if CFG else None
        if self.config_tab:
            self.nb.add(self.config_tab, text="環境參數")
        for name in ("ComfyUI", "更新", "安全檢查", "安裝 / 修復",
                     "GPU", "節點"):
            if name == "節點":
                continue
            f = ttk.Frame(self.nb, padding=8)
            pane = LogPane(f)
            pane.pack(fill="both", expand=True)
            self.panes[name] = pane
            self.nb.add(f, text=name)
        self.nodes_tab = NodesTab(self.nb, self, defer=True) if NB else None
        if self.nodes_tab:
            self.nb.add(self.nodes_tab, text="節點")
        f = ttk.Frame(self.nb, padding=8)
        pane = LogPane(f)
        pane.pack(fill="both", expand=True)
        self.panes["節點"] = pane
        self.nb.add(f, text="節點操作紀錄")
        self.logs_tab = LogsTab(self.nb, self, defer=True)
        self.nb.add(self.logs_tab, text="歷史紀錄")
        self.nb.bind("<<NotebookTabChanged>>", self._tab_changed)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self.refresh_status)
        self.after(300, self.refresh_server_state)
        self.after(150, self._adopt_running)

    def _adopt_running(self):
        """開窗時如果 ComfyUI 已經在跑（自己之前開的、或 run.bat 開的），接回來"""
        pane = self.pane("ComfyUI")

        def probe():
            return self.server.adopt(pane)

        def done(ok, err):
            if ok:
                self.refresh_server_state()

        self.bg.submit(probe, done, key="adopt")

    def _tab_changed(self, _ev=None):
        """第一次切到某個分頁才真正把它的內容建起來"""
        try:
            w = self.nb.nametowidget(self.nb.select())
        except Exception:
            return
        if w in self._built:
            return
        self._built.add(w)
        if hasattr(w, "build"):
            w.build()

    # ── 版面 ──
    def _header(self):
        h = ttk.Frame(self, padding=(12, 10, 12, 6))
        h.pack(fill="x")
        ttk.Label(h, text="ComfyUI 環境管理", style="H.TLabel").pack(side="left")
        where = (f"{APP_DIR}　→　本體 {COMFY_DIR}"
                 if os.path.normcase(APP_DIR) != os.path.normcase(COMFY_DIR)
                 else APP_DIR)
        self.where = ttk.Label(h, text=where, style="Muted.TLabel")
        self.where.pack(side="left", padx=12)
        self.busy_lbl = ttk.Label(h, text="", foreground=ACCENT,
                                  background=BG, font=UI_B)
        self.busy_lbl.pack(side="right")
        self.stop_btn = ttk.Button(h, text="中止", command=self.runner.stop,
                                   state="disabled")
        self.stop_btn.pack(side="right", padx=8)

        s = ttk.Frame(self, style="Card.TFrame", padding=8)
        s.pack(fill="x", padx=10, pady=(0, 8))
        self.status = ttk.Label(s, text="讀取環境狀態…", style="Card.TLabel",
                                font=MONO)
        self.status.pack(side="left")
        ttk.Button(s, text="重新檢查", command=self.refresh_status).pack(side="right")
        self.restart_after = tk.BooleanVar(value=False)
        ttk.Checkbutton(s, text="任務完成後自動重新啟動 ComfyUI",
                        variable=self.restart_after,
                        style="Card.TCheckbutton").pack(side="right", padx=12)

    def _sidebar(self, parent):
        side = ttk.Frame(parent, style="Card.TFrame", padding=12)
        side.pack(side="left", fill="y")

        def group(title):
            ttk.Label(side, text=title, style="CardMuted.TLabel",
                      font=UI_B).pack(anchor="w", pady=(10, 4))

        def btn(text, cmd, style="TButton"):
            b = ttk.Button(side, text=text, command=cmd, style=style, width=22)
            b.pack(fill="x", pady=2)
            return b

        group("ComfyUI")
        self.srv_lbl = ttk.Label(side, text="狀態：查詢中…", style="CardMuted.TLabel")
        self.srv_lbl.pack(anchor="w", pady=(0, 4))
        self.b_run = btn("▶  啟動", self.act_run, "Go.TButton")
        self.b_stop = btn("■  終止", self.act_stop, "Danger.TButton")
        self.b_restart = btn("↻  重啟", self.act_restart)
        self.b_open = btn("在瀏覽器開啟", self.act_open_web)

        group("日常")
        self.b_upd = btn("純更新（不啟動）", self.act_update)
        self.b_chk = btn("唯讀環境檢查", self.act_envcheck)

        group("檢查 / 修復")
        self.b_gpu = btn("GPU 狀態 nvidia-smi", self.act_gpu)
        self.b_audit = btn("安全檢查 comfy_audit", self.act_audit)
        self.b_repair = btn("修復 torch / wheel", self.act_repair)
        self.b_wheels = btn("重裝自訂 wheel", self.act_wheels)

        group("安裝 / 重建")
        self.b_inst = btn("補齊環境 install.py", self.act_install)
        self.b_recr = btn("重建 venv（保留舊的）", self.act_recreate, "Danger.TButton")
        self.b_reset = btn("覆蓋核心到最新", self.act_reset_core, "Danger.TButton")

        # 執行 pip / git 期間會被停用的按鈕。啟動/終止 ComfyUI 不受影響，
        # 因為那是獨立的長期程序，隨時要能停掉。
        self.buttons = [self.b_upd, self.b_chk, self.b_gpu, self.b_audit,
                        self.b_repair, self.b_wheels, self.b_inst,
                        self.b_recr, self.b_reset]

        ttk.Label(side, text="模型、工作流、custom_nodes\n一律不會被這些動作刪除",
                  style="CardMuted.TLabel",
                  font=("Microsoft JhengHei UI", 8)).pack(anchor="w", pady=(16, 0))

    # ── 狀態 ──
    def set_busy(self, busy, name):
        self.busy_lbl.configure(text=f"執行中：{name}" if busy else "")
        self.stop_btn.configure(state="normal" if busy else "disabled")
        for b in self.buttons:
            b.configure(state="disabled" if busy else "normal")

    def pane(self, name):
        p = self.panes[name]
        for i in range(self.nb.index("end")):
            if self.nb.tab(i, "text") in (name, "節點操作紀錄" if name == "節點"
                                          else name):
                self.nb.select(i)
                break
        return p

    def venv_path(self):
        return os.path.join(COMFY_DIR, "venv",
                            "Scripts" if os.name == "nt" else "bin",
                            "python.exe" if os.name == "nt" else "python")

    def venv_py(self):
        """venv 的 Python。找不到就回 None —— 不可以退回系統 Python。

        更新、裝依賴、跑 ComfyUI 都必須在 venv 裡，退回系統 Python 會把
        套件裝到系統環境去，比直接報錯更難查。
        """
        p = self.venv_path()
        return p if os.path.exists(p) else None

    def need_venv(self):
        """
        取得 venv 的 python。只有「會動到 site-packages」的動作該用這支：
        純更新、環境修復、重裝 wheel、安全檢查。

        安裝 / 重建那三顆按鈕不要用 —— 它們本來就是來建環境的，
        要求環境先存在邏輯不通。那邊一律走系統 Python。
        """
        p = self.venv_py()
        if p is None:
            if messagebox.askyesno(
                    "還沒有 venv",
                    f"預期的位置：\n{self.venv_path()}\n\n"
                    f"本體目錄：\n{COMFY_DIR}\n\n"
                    "這個動作會安裝或更換套件，必須在 venv 裡做"
                    "（用系統 Python 會把套件裝到系統環境去）。\n\n"
                    "要現在建立環境嗎？"):
                self._install_dialog()
        return p

    def refresh_status(self):
        """
        狀態列。整段在背景跑。

        這裡面有 import torch（兩到五秒）、git log、以及逐一 git 的節點掃描。
        原本是開窗後直接在主執行緒跑，所以「打開就卡住」——視窗畫出來了
        但完全沒反應，要等 torch 載完。
        """
        self.status.configure(text="讀取環境狀態…")
        self.bg.submit(self._status_probe, self._status_done, key="status")

    def _status_probe(self):
        """背景執行緒：不要碰任何 widget"""
        bits = []
        py = self.venv_py()
        bits.append("venv ✓" if py else "venv ✗（請先 install.bat）")
        if os.path.normcase(COMFY_DIR) != os.path.normcase(APP_DIR):
            ok = os.path.exists(os.path.join(COMFY_DIR, "main.py"))
            bits.append(f"本體 {'✓' if ok else '✗'} {COMFY_DIR}")
        probe = py or host_py() or sys.executable
        r = sh([probe, "-c", "import torch;print(torch.__version__)"], timeout=40)
        bits.append(f"torch {r.stdout.strip()}" if r.returncode == 0
                    else "torch ✗")
        r = sh(["git", "log", "-1", "--format=%h %ad", "--date=short"],
               cwd=COMFY_DIR, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            bits.append(f"核心 {r.stdout.strip()}")
        if NB:
            ns = self._nodes_cached(force=True)
            bits.append(f"節點 {sum(1 for n in ns if not n['disabled'])} 啟用 / "
                        f"{sum(1 for n in ns if n['disabled'])} 停用")
        return "　｜　".join(bits)

    def _status_done(self, text, err):
        self.status.configure(text=text if not err else f"狀態讀取失敗：{err}")

    # ── 節點掃描快取 ──
    # NB.scan() 會對每個節點跑 git，四十個節點就是四十次子程序。
    # 切到「節點」分頁時重掃一次要好幾秒，所以存在這裡共用。
    NODES_TTL = 20.0

    def _nodes_cached(self, force=False):
        """背景執行緒專用。主執行緒請用 nodes_snapshot()"""
        now = time.time()
        if not force and self._nodes and now - self._nodes_at < self.NODES_TTL:
            return self._nodes
        ns = NB.scan() if NB else []
        self._nodes, self._nodes_at = ns, now
        return ns

    def nodes_snapshot(self):
        """主執行緒：只回快取，沒有就回 None（呼叫端自己去背景要）"""
        if self._nodes and time.time() - self._nodes_at < self.NODES_TTL:
            return self._nodes
        return None

    def invalidate_nodes(self):
        self._nodes_at = 0.0

    # ── 動作 ──
    def run_when_stopped(self, what, fn):
        """
        確定 ComfyUI 關掉之後才執行 fn。

        ensure_stopped 現在是非同步的（殺程序和等待都在背景），所以
        呼叫端不能再寫「if not ensure_stopped(): return」然後往下跑 ——
        那樣在需要關閉的情況下會直接不做事。統一走這支：
          沒在跑   → 立刻執行 fn
          在跑     → 問使用者 → 背景關閉 → 關好才執行 fn
          使用者取消 / 關不掉 → fn 不會被執行
        """
        if self.ensure_stopped(what, then=fn):
            fn()

    def ensure_stopped(self, what, then=None):
        """會動到套件或檔案的任務之前，必須先確定 ComfyUI 沒在跑。

        Windows 會鎖住載入中的 .pyd / .dll：ComfyUI 開著的時候
        pip 換不掉 torch、git 也覆蓋不了本體檔案，而且 VRAM 還被佔著。
        回傳 True = 可以繼續。
        """
        # 這裡只能用「不阻塞」的判斷：own_pid 讀旗標，alive_cache 是
        # 背景探測寫下的。絕對不要在這裡呼叫 external()（WMI）或
        # http_alive()（網路），那會讓按下按鈕的瞬間整個視窗凍住。
        own = self.server.own_pid()
        running = bool(own) or self.server._alive_cache[1]
        if not running:
            return True
        ext = []
        pids = ([own] if own else []) + [p for p, c in ext]
        tail = ("\n\n（這份 ComfyUI 不是由這個介面啟動的）"
                if not own and ext else "")
        if not messagebox.askyesno(
                "要先關閉 ComfyUI",
                f"「{what}」會動到套件或本體檔案，ComfyUI 開著不能做：\n\n"
                "・Windows 鎖住載入中的 .pyd / .dll，pip 換不掉 torch\n"
                "・git 覆蓋不了正在使用的檔案\n"
                "・VRAM 還被佔著\n\n"
                f"現在執行中：PID {', '.join(str(p) for p in pids)}{tail}\n\n"
                "要關閉 ComfyUI 並繼續嗎？", icon="warning"):
            return False
        pane = self.pane("ComfyUI")
        pane.write(f"\n[INFO] 為了執行「{what}」先關閉 ComfyUI\n")
        self._was_running = True

        def after_kill(dead):
            if not dead:
                messagebox.showerror(
                    "關不掉",
                    "等了 20 秒，還是偵測得到 ComfyUI。\n\n"
                    "請到工作管理員手動結束 python.exe 後再試，\n"
                    "否則 pip 會因為檔案被鎖住而失敗。")
                return
            pane.write("[OK]   ComfyUI 已關閉，檔案句柄已釋放\n")
            if then:
                then()

        targets = ([own] if own else []) + [p for p, c in ext]
        if not targets and SYS:
            # 接管的情況下沒有已知 PID，背景去找
            def find_and_kill():
                return [p for p, c in SYS.find_comfy(COMFY_DIR)]

            def got(pids, err):
                if pids:
                    self.server.kill_async(pids, on_done=after_kill)
                else:
                    after_kill(True)
            self.bg.submit(find_and_kill, got, key="findkill")
        else:
            self.server.kill_async(targets, on_done=after_kill)
        return False        # 關閉是非同步的，呼叫端這一輪先不要繼續

    def _after_task(self, code):
        """任務結束：需要時把 ComfyUI 開回來"""
        self.refresh_status()
        if getattr(self, "_was_running", False) and self.restart_after.get():
            self._was_running = False
            self.pane("ComfyUI").write("\n[INFO] 任務結束，自動重新啟動 ComfyUI\n")
            self.after(1500, lambda: self.server.start(self.pane("ComfyUI")))
        elif getattr(self, "_was_running", False):
            self._was_running = False
            if messagebox.askyesno("要把 ComfyUI 開回來嗎？",
                                   "剛才為了執行任務把 ComfyUI 關掉了。\n\n"
                                   "現在重新啟動嗎？"):
                self.server.start(self.pane("ComfyUI"))

    def _need(self, fname, in_comfy=False):
        base = COMFY_DIR if in_comfy else APP_DIR
        p = os.path.join(base, fname)
        if not os.path.exists(p):
            messagebox.showerror("找不到檔案",
                                 f"{fname} 不在 {base}\n"
                                 + ("請確認 comfy_env.py 的 COMFY_ROOT "
                                    "指向正確的 ComfyUI 目錄。"
                                    if in_comfy else
                                    "請確認這支程式和其他腳本放在一起。"))
            return None
        return p

    def act_run(self):
        if not self._need("main.py", in_comfy=True):
            return
        if self.runner.busy and not messagebox.askyesno(
                "還在更新",
                f"「{self.runner.name}」還在跑。\n"
                "更新途中啟動 ComfyUI 可能載到半裝好的套件。\n\n仍要啟動嗎？"):
            return
        self.server.start(self.pane("ComfyUI"))

    def act_stop(self):
        self.server.stop()

    def act_restart(self):
        if not self._need("main.py", in_comfy=True):
            return
        self.server.restart(self.pane("ComfyUI"))

    def act_open_web(self):
        import webbrowser
        port = self.server.port
        if SYS and not SYS.port_open(port):
            if not messagebox.askyesno(
                    "連接埠沒有回應",
                    f"127.0.0.1:{port} 現在沒有在監聽。\n"
                    "ComfyUI 可能還在載入模型，或還沒啟動。\n\n還是要開啟瀏覽器嗎？"):
                return
        webbrowser.open(f"http://127.0.0.1:{port}")

    def refresh_server_state(self):
        """
        側邊欄狀態 + 按鈕啟用狀態。

        拆成兩段的理由：
          便宜（主執行緒，每 1.5 秒）—— 自己的子程序還活著嗎、開機多久了
          昂貴（背景，最多每 10 秒）—— 掃外部程序要走 WMI/PowerShell，
                                       一次 300-800ms；連接埠查詢也丟背景
        而且我們自己在跑 ComfyUI 的時候，外部掃描完全不必做。
        原本是每 3 秒在主執行緒做完整套，那就是你感覺到的週期性卡頓。
        """
        self._paint_server_state()
        now = time.time()
        own_running = bool(self.server.own_pid())
        gap = 30.0 if own_running else 10.0
        if now - self._ext_at > gap:
            self._ext_at = now
            self.bg.submit(self._probe_external, self._probe_external_done,
                           key="ext")
        self.after(1500, self.refresh_server_state)

    def _probe_external(self):
        self.server.refresh_owned()      # 順便更新 owned_pid 的存活旗標
        """
        ComfyUI 到底在不在 —— 以 HTTP 為準。

        掃程序表會誤判成「已經關閉」：節點的 worker 子程序、剛結束還沒被
        回收的殼、WMI 偶爾查不到。連接埠回得了 200 就是真的活著，
        而且不管是誰啟動的都一樣準。
        """
        listening = self.server.http_alive()
        mine = self.server.own_pid()
        ext = []
        if listening and not mine and SYS:
            ext = [(p, c) for p, c in SYS.find_comfy(COMFY_DIR)]
        return ext, listening

    def _probe_external_done(self, out, err):
        if err or out is None:
            return
        self._ext, self._listening = out
        # 主執行緒的 alive() 只讀這個快取
        self.server._alive_cache = (time.time(), bool(self._listening))
        self._paint_server_state()

    def _paint_server_state(self):
        own = self.server.own_pid()
        ext = [] if own else self._ext
        port = self.server.port
        listening = self._listening
        if own:
            up = int(time.time() - (self.server.started or time.time()))
            txt = (f"執行中　PID {own}\n"
                   f"{up // 60}分{up % 60}秒"
                   f"　埠 {port} {'通' if listening else '尚未就緒'}")
            color = OK
        elif listening:
            who = (f"PID {', '.join(str(p) for p, c in ext[:3])}"
                   if ext else "（外部啟動）")
            txt = f"執行中　{who}\n埠 {port} 通　console 已接上"
            color = WARN
        else:
            txt = "未執行"
            color = MUTED
        self.srv_lbl.configure(text="狀態：" + txt, foreground=color)
        alive = bool(own or ext)
        self.b_stop.configure(state="normal" if alive else "disabled")
        self.b_open.configure(state="normal" if alive or listening else "disabled")
        self.b_run.configure(state="disabled" if own else "normal")

    def act_update(self):
        if not self._need("update.py"):
            return
        py = self.need_venv()
        if not py:
            return
        self.run_when_stopped("純更新", lambda: self.runner.start(
            "純更新", [py, "update.py", "--no-launch"],
            self.pane("更新"), on_done=self._after_task))

    def act_envcheck(self):
        if not self._need("comfy_env.py"):
            return
        py = self.need_venv()
        if not py:
            return
        self.runner.start("唯讀檢查", [py, "comfy_env.py"],
                          self.pane("更新"))

    def act_gpu(self):
        """直接跑 nvidia-smi，輸出丟到 GPU 分頁"""
        exe = shutil.which("nvidia-smi")
        if not exe and os.name == "nt":
            guess = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                 "System32", "nvidia-smi.exe")
            exe = guess if os.path.exists(guess) else None
        if not exe:
            messagebox.showerror("找不到 nvidia-smi",
                                 "PATH 和 System32 裡都沒有 nvidia-smi.exe。\n"
                                 "通常隨 NVIDIA 驅動一起安裝。")
            return
        self.runner.start("nvidia-smi", [exe], self.pane("GPU"))

    def act_audit(self):
        if not self._need("comfy_audit.py"):
            return
        win = tk.Toplevel(self)
        win.title("安全檢查選項")
        win.configure(bg=BG)
        win.transient(self)
        f = ttk.Frame(win, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="comfy_audit.py　唯讀檢查，不會修改任何檔案",
                  style="Muted.TLabel").grid(row=0, column=0, columnspan=2,
                                             sticky="w", pady=(0, 10))
        ttk.Label(f, text="回溯天數").grid(row=1, column=0, sticky="w")
        days = tk.StringVar(value="14")
        ttk.Entry(f, textvariable=days, width=8).grid(row=1, column=1, sticky="w")
        quick = tk.BooleanVar(value=False)
        verbose = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="快速模式（略過 site-packages 全面掃描）",
                        variable=quick).grid(row=2, column=0, columnspan=2,
                                             sticky="w", pady=(8, 0))
        ttk.Checkbutton(f, text="詳細（列出資訊級項目）", variable=verbose).grid(
            row=3, column=0, columnspan=2, sticky="w")

        def go():
            argv = [self.venv_py() or host_py() or sys.executable,
                    "comfy_audit.py",
                    "--root", COMFY_DIR,
                    "--days", (days.get().strip() or "14")]
            if quick.get():
                argv.append("--quick")
            if verbose.get():
                argv.append("--verbose")
            win.destroy()
            self.runner.start("安全檢查", argv, self.pane("安全檢查"),
                              on_done=lambda c: self.logs_tab.refresh())

        ttk.Button(f, text="開始檢查", style="Go.TButton", command=go).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(14, 0))

    def act_repair(self):
        if not self._need("repair_env.py"):
            return
        py = self.need_venv()
        if not py:
            return
        def go():
            if not messagebox.askyesno(
                    "修復環境",
                    "會做這三件事：\n\n"
                    "1. 移除黑名單套件（tensorflow 等）\n"
                    "2. 把 torch 三件套還原成鎖定的 cu130 版本\n"
                    "3. 重新安裝 wheels/ 底下的自訂 wheel\n\n"
                    "模型和工作流不受影響。要繼續嗎？"):
                return
            self.runner.start("環境修復", [py, "repair_env.py"],
                              self.pane("安裝 / 修復"),
                              on_done=self._after_task)
        self.run_when_stopped("環境修復", go)

    def act_wheels(self):
        if not self._need("install_wheels.py"):
            return
        py = self.need_venv()
        if not py:
            return
        self.run_when_stopped("重裝 wheel", lambda: self.runner.start(
            "重裝 wheel", [py, "install_wheels.py"],
            self.pane("安裝 / 修復")))

    def act_install(self):
        """補齊環境：不需要既有的 venv，install.py 會自己處理"""
        if not self._need("install.py"):
            return
        self.run_when_stopped("補齊環境",
                              self._install_dialog)

    def act_recreate(self):
        if not self._need("install.py"):
            return
        self.run_when_stopped("重建 venv", self._recreate_dialog)

    def _recreate_dialog(self):
        """
        重建 venv —— 只做這一件事。

        刻意不跟「補齊環境」混在一起：重建是把環境砍掉重來，補齊是
        把套件裝好，兩件事的風險和前提都不同。混在一顆按鈕裡的話，
        你想重建卻會順便跑完整套安裝，想補齊卻要先回答重建的問題。
        """
        hp = host_py()
        if not hp:
            messagebox.showerror(
                "找不到系統 Python",
                "重建 venv 必須用 venv 以外的 Python"
                "（不能從 venv 裡面重建自己）。\n\n"
                "PATH 上找不到另一支 Python 3.12。")
            return
        if not messagebox.askyesno(
                "重建 venv",
                f"位置：\n{self.venv_path()}\n\n"
                "舊的 venv 會改名成 venv.bak_時間保留，不是刪除。\n"
                "models、user（工作流）、custom_nodes、本體都不動。\n\n"
                "重建出來的是「空的」環境，沒有任何套件 ——\n"
                "接著要按「補齊環境 install.py」把套件裝回去。\n\n"
                "⚠ 補齊之後 comfy_kitchen 會變成 wheels/ 裡的版本，\n"
                "　 需要的話再跑一次 build_comfy_kitchen.py。\n\n"
                "要繼續嗎？", icon="warning"):
            return
        self.runner.start(
            "重建 venv", [hp, "install.py", "--recreate", "--venv-only"],
            self.pane("安裝 / 修復"), on_done=self._after_task)

    def _install_dialog(self):
        win = tk.Toplevel(self)
        win.title("補齊環境")
        win.configure(bg=BG)
        win.transient(self)
        f = ttk.Frame(win, padding=14)
        f.pack(fill="both", expand=True)
        txt = ("依 comfy_env.py 的鎖定版本補齊套件：\n"
               "本體（缺就 git clone）→ venv（沒有就建）→ pip → torch →\n"
               "本體 requirements → 額外套件 → 節點依賴 → wheel。\n"
               "已經正確的不會重裝。")
        ttk.Label(f, text=txt, style="Muted.TLabel", justify="left").grid(
            row=0, column=0, sticky="w", pady=(0, 12))
        lock = tk.BooleanVar(value=False)
        skip = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="--lock　依 env_state/pip_freeze.txt 還原版本",
                        variable=lock).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(f, text="--skip-nodes　不要自動 clone 缺少的節點",
                        variable=skip).grid(row=2, column=0, sticky="w")

        def go():
            # 這兩個動作都「不需要」既有的 venv —— 它們就是用來把環境
            # 建起來的。install.py 的外層本來就跑在系統 Python 上：
            # 建立（或沿用）venv，再用那個 venv 重新執行自己。
            # 所以一律用系統 Python，venv 不存在也完全正常。
            hp = host_py()
            if not hp:
                messagebox.showerror(
                    "找不到系統 Python",
                    "建立 / 重建 venv 必須用 venv 以外的 Python"
                    "（不能從 venv 裡面重建自己）。\n\n"
                    "PATH 上找不到另一支 Python 3.12。\n\n"
                    "解法：裝好 Python 3.12（記得勾 Add to PATH），"
                    "或開一個沒有 activate 的命令提示字元執行\n"
                    "    python install.py --recreate")
                return
            argv = [hp, "install.py"]
            if lock.get():
                argv.append("--lock")
            if skip.get():
                argv.append("--skip-nodes")
            win.destroy()
            self.runner.start("補齊環境", argv,
                              self.pane("安裝 / 修復"),
                              on_done=self._after_task)

        ttk.Button(f, text="開始", style="Go.TButton", command=go).grid(
            row=3, column=0, sticky="ew", pady=(14, 0))

    def act_reset_core(self):
        """把 ComfyUI 本體覆蓋成上游最新版，保留模型與工作流"""
        def go():
            # git status / git log 在大的工作樹上可能要好幾秒，不要卡住介面
            self.status.configure(text="讀取本體狀態…")
            self.bg.submit(self._core_state, self._reset_core_ask,
                           key="corestate")
        self.run_when_stopped("覆蓋核心", go)

    @staticmethod
    def _core_state():
        r = sh(["git", "status", "--porcelain", "-uno"], cwd=COMFY_DIR, timeout=60)
        dirty = [l for l in r.stdout.splitlines() if l.strip()]
        r2 = sh(["git", "log", "--oneline", "@{u}..HEAD"], cwd=COMFY_DIR, timeout=60)
        local = [l for l in r2.stdout.splitlines() if l.strip()]
        return dirty, local

    def _reset_core_ask(self, out, err):
        self.status.configure(text="")
        if err or out is None:
            messagebox.showerror("讀不到本體狀態", str(err))
            return
        dirty, local = out
        detail = ""
        if dirty:
            detail += f"\n未提交的改動 {len(dirty)} 個：\n" + "\n".join(
                "　" + l for l in dirty[:10]) + ("\n　…" if len(dirty) > 10 else "")
        if local:
            detail += f"\n\n本地 commit {len(local)} 個（自訂 patch）：\n" + "\n".join(
                "　" + l for l in local[:10])
        if not messagebox.askyesno(
                "覆蓋核心到最新版",
                "會執行：git fetch origin → git reset --hard origin/master\n\n"
                "保留：models、user（工作流）、custom_nodes、output、\n"
                "　　　venv、wheels、你的腳本（未被 git 追蹤的都不動）\n\n"
                "丟棄：ComfyUI 本體檔案的所有本地修改和本地 commit"
                + detail
                + "\n\n要繼續嗎？", icon="warning"):
            return
        pane = self.pane("安裝 / 修復")
        # 不要自己拼 git 指令：那會用「目前的工作目錄」，而腳本目錄可能
        # 根本不是 ComfyUI（COMFY_ROOT 把本體指到別處時就會 fatal: not a
        # git repository）。交給 comfy_env.overwrite_core()，它自己解析
        # ROOT、偵測分支（不假設是 master），順便把 origin 同步到 CORE_REPO。
        self.runner.start(
            "覆蓋核心",
            [host_py() or sys.executable, "-c",
             "import sys\n"
             "sys.path.insert(0, '.')\n"
             "import comfy_env as E\n"
             "print('本體目錄：' + E.ROOT, flush=True)\n"
             "ok, msg = E.ensure_core_tree()\n"
             "print(('[OK]   ' if ok else '[ERROR] ') + msg, flush=True)\n"
             "c = E.core_info()\n"
             "print('[INFO] 目前：v%s  %s  %s  %s'\n"
             "      % (c['version'], c['short'], c['date'], c['subject']),\n"
             "      flush=True)\n"
             "sys.exit(0 if ok else 1)"],
            pane, cwd=APP_DIR, on_done=self._after_task)

    def on_close(self):
        # 關掉管理介面「不會」動到 ComfyUI。
        # ComfyUI 沒有管線綁著（stdout 接 DEVNULL），所以它照常跑；
        # 下次開啟會用連接埠把它接回來（ServerCtl.adopt）。
        # 要停 ComfyUI 請按側邊欄的「終止」。
        self.bg.stop()
        self.server.http.stop()
        self.server.save_state()
        if self.runner.busy:
            if not messagebox.askyesno("還有任務在跑",
                                       f"「{self.runner.name}」還在執行。\n"
                                       "中途結束 pip 可能留下半裝好的套件。\n\n"
                                       "還是要關閉嗎？"):
                return
            self.runner.stop()
        self.destroy()


def main():
    missing = [f for f in ("comfy_env.py", "update.py")
               if not os.path.exists(os.path.join(APP_DIR, f))]
    if not missing and not os.path.exists(os.path.join(COMFY_DIR, "main.py")):
        missing.append(f"main.py（找的是 {COMFY_DIR}）")
    app = App()
    if missing:
        messagebox.showwarning(
            "不在 ComfyUI 根目錄？",
            f"這個資料夾裡找不到：{'、'.join(missing)}\n\n{APP_DIR}\n\n"
            "腳本要放在一起；ComfyUI 本體的位置由 comfy_env.py 的\n"
            "COMFY_ROOT 決定（留空 = 跟腳本同一層）。")
    app.mainloop()


if __name__ == "__main__":
    main()
