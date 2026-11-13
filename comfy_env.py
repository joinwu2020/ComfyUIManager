# -*- coding: utf-8 -*-
"""
ComfyUI 環境共用設定與工具 —— update.py / install.py 共用

要改鎖定版本、黑名單、排除 / 固定節點、啟動參數，只改最上面的「設定區」。
"""
import atexit
import datetime
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys

try:
    import tomllib
except ImportError:
    tomllib = None

try:
    from packaging.version import Version, InvalidVersion
except ImportError:
    try:
        from pip._vendor.packaging.version import Version, InvalidVersion
    except ImportError:
        Version = None
        InvalidVersion = Exception

try:
    from packaging.requirements import Requirement
except ImportError:
    try:
        from pip._vendor.packaging.requirements import Requirement
    except ImportError:
        Requirement = None


# ══════════════════════════ 設定區 ══════════════════════════

PY_MAJOR_MINOR = (3, 12)
CUDA_TAG = "cu130"
TORCH_INDEX = f"https://download.pytorch.org/whl/{CUDA_TAG}"
TORCH_PINS = {                       # 精確版本，不接受更高版
    "torch":       "2.13.0",
    "torchvision": "0.28.0",
    "torchaudio":  "2.11.0",
}

# 只鎖「最低版本」，不限制上限：被某個節點降版時會自動升回來。
# numpy：tensorflow 系列（comfyui-utils-nodes）會把它降成 1.26.4，
#        而 pip uninstall tensorflow 不會把 numpy 還原，所以必須另外顧。
FLOOR_PINS = {
    "numpy": "2.0",
    # 範例：onnx 1.23 需要 protobuf >= 6.31.1。啟用前先確認沒有節點把它壓在 5.x
    # （例如舊版 Google Gemini SDK 的 grpcio-status 要求 protobuf<6），
    # 否則那個節點會「安裝失敗」。
    # "protobuf": "6.31.1",
}

# True：每次 pip install 都帶約束檔（env_state/constraints.txt），
#       任何 requirements 都無法把 torch 三件套換版、也不能把 FLOOR_PINS 降到下限以下。
#       想違反的節點會「安裝失敗」並列在摘要裡，而不是默默換掉。
USE_CONSTRAINTS = True

# install.py 在 torch 之後安裝的額外套件（原 run.bat 的內容）
# opencv-contrib-python 必須在這裡：BLOCKLIST 擋掉其他 opencv 變體，
# 而 SHARED_DIR_REPAIR 移除它們之後會把 contrib 補回來。
EXTRA_PACKAGES = [
    "opencv-contrib-python",
    "triton-windows",
    "gitpython",          # ComfyUI-Manager 需要
]

# 裝了會炸的套件：{套件: 原因}
# tensorflow：transformers/image_transforms.py 偵測到就會 import 它，
#             TF 的 wheel 是用 NumPy 1.x 編譯的，配 NumPy 2.x 會讓
#             所有依賴 transformers 的節點連鎖崩潰。
BLOCKLIST = {
    "tensorflow":       "transformers 會自動 import，與 NumPy 2.x 不相容",
    "tensorflow-intel": "tensorflow 的相依套件",
    "tensorboard":      "隨 tensorflow 一起進來",
    # 四個 opencv 變體共用同一個 site-packages/cv2/，互相覆蓋。
    # 統一用 opencv-contrib-python（功能最完整；LayerStyle 的
    # ximgproc.guidedFilter 需要它），其他三個擋掉。
    "opencv-python-headless":
        "會覆蓋 opencv-contrib-python 的 cv2/，使 ximgproc.guidedFilter 消失",
    "opencv-contrib-python-headless":
        "headless 變體與 opencv-contrib-python 互相覆蓋",
    "opencv-python":
        "opencv-contrib-python 是它的超集，兩者共用 cv2/ 會互相覆蓋",
    # 範例：不用 Gemini 相關節點時，可擋掉舊版 Google SDK，
    # 避免 grpcio-status 把 protobuf 壓在 5.x 而與 onnx 衝突。
    # 擋掉之後，依賴 Gemini API 的節點功能會失效。
    # "google-generativeai":          "舊版 Gemini SDK；連帶把 protobuf 壓在 5.x",
    # "google-ai-generativelanguage": "要求 protobuf<6",
    # "grpcio-status":                "要求 protobuf<6，與 onnx 衝突",
    # "google-genai":                 "Gemini SDK",
}

# 不自動 git 更新的節點：{資料夾名: 原因}（依賴仍會檢查）
EXCLUDE_NODES = {
    # "my_custom_node": "自製節點，不要 git pull",
}

# 套件在這台機器上換個名字：{requirements 裡的名字: (實際套件, 原因)}
#
# pip install -r 是「一行壞、整份死」—— 檔案裡只要有一個在本平台
# 找不到的套件，同一份 requirements 的其他套件全都不會裝。
#
# 這裡列出的名稱，在交給 pip 之前會被換掉：
#     實際套件已安裝  → 整行剔除（需求已經滿足了）
#     實際套件沒裝    → 換成實際套件名（版本條件丟掉，跨套件沒有意義）
#
# BLOCKLIST 不適合拿來處理這種情形：它的語意是「裝到就移除」，
# 而 triton-windows 在某些版本的 metadata 名稱就是 triton，
# 會被自己的守門機制反過來解除安裝。
REQ_ALIASES = {
    "triton": ("triton-windows",
               "PyPI 的 triton 只發布 Linux wheel，Windows 對應套件是 triton-windows"),
}

# ComfyUI 本體在哪裡。
#
#   None        自動尋找（建議）—— 先看腳本自己的目錄，再找上層和鄰近
#               目錄裡有 main.py + comfy/ 的那一個。可攜碟換到別台電腦、
#               磁碟代號變了也照樣找得到。
#   相對路徑     以腳本目錄為基準，例 r"..\ComfyUI"。可攜用這個最穩。
#   絕對路徑     例 r"D:\AI\ComfyUI"
#
# 環境變數 COMFY_ROOT 優先於這裡的設定。
COMFY_ROOT = None

# 核心鎖版：填 commit 或 tag（例 "a8686f2b"、"v0.3.60"）；None = 跟著上游更新
PIN_CORE = None

# 本體來源。comfyanonymous/ComfyUI 已轉移到 Comfy-Org/ComfyUI（舊網址會重導向）
CORE_REPO = "https://github.com/Comfy-Org/ComfyUI.git"
CORE_BRANCH = "master"

# 節點固定版本：{資料夾名: commit 或 tag}，只對有 .git 的節點有效
NODE_PINS = {}

# wheels/ 底下的自訂 wheel 被覆蓋時自動重裝；False = 只在摘要提醒
AUTO_REINSTALL_WHEELS = False

# ComfyUI 版控裡的空佔位檔。它們只是用來讓空資料夾存在於 git，
# 內容無意義，但一旦被刪掉（清 output、整理 models 都會）git 就算成本地改動，
# 於是每次更新都被擋下。更新前自動把「被刪掉」的這類檔案還原，
# 不會動到任何被「修改」的檔案。比對的是檔名，不是路徑。
PLACEHOLDER_GLOBS = [
    "put_*_here",
    "*_will_be_put_here",
    "*.example",
]

# update.py 結束時啟動 ComfyUI 的參數（與 run.bat 一致）
# 刻意不放 --enable-cors-header "*"：
#   那等於允許你瀏覽的「任何網頁」對本機的 ComfyUI 發請求。
#   ComfyUI 沒有身分驗證，而綁 127.0.0.1 擋得住外網、擋不住你自己的瀏覽器
#   —— 惡意網頁可以直接 POST /upload/image 和 /prompt。
#   真的需要跨網域呼叫 API，改成從 Python 直接打（不受 CORS 限制）。
LAUNCH_ARGS = [
    "--reserve-vram",
    "1",
    "--enable-manager",
    "--listen",
    "127.0.0.1",
]

# 自訂 wheel 的匯入驗證
# 放進 wheels/ 的自訂 wheel，裝完後用這段程式碼驗證能否 import。
# {顯示名稱, 一行 Python}；印出的內容會出現在更新摘要。
WHEEL_CHECKS = [
    # ("flash_attn",    "import flash_attn; print(flash_attn.__version__)"),
    # ("sageattention", "from importlib.metadata import version; "
    #                   "print(version('sageattention'))"),
]

TORCH_CHECK = (
    "import torch\n"
    "a = torch.cuda.is_available()\n"
    "print(torch.__version__, torch.version.cuda, a,\n"
    "      torch.cuda.get_device_name(0) if a else '-',\n"
    "      'sm_%d%d' % torch.cuda.get_device_capability(0) if a else '-', sep='|')"
)


# ══════════════════════════ 路徑 ══════════════════════════

# TOOL_DIR = 這套腳本放在哪（comfy_env.py 自己的目錄）
# ROOT     = ComfyUI 本體在哪（main.py、custom_nodes、models、venv）
# 兩者可以是同一個目錄（舊的放法），也可以分開。
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))


# 判斷一個目錄是不是 ComfyUI 本體：這兩個同時存在才算
CORE_MARKERS = ("main.py", "comfy")

# 附近找不到時，依序試這些慣例位置。
# ~ 在 Windows 上會展開成 C:\Users\你的帳號，所以換電腦或改帳號名都不用改設定。
# OneDrive 那幾條是因為 Windows 常把「文件」重新導向到 OneDrive 底下。
COMMON_LOCATIONS = (
    "~/Documents/comfy/ComfyUI",
    "~/Documents/ComfyUI",
    "~/OneDrive/Documents/comfy/ComfyUI",
    "~/OneDrive/Documents/ComfyUI",
    "~/comfy/ComfyUI",
    "~/ComfyUI",
    "~/Desktop/ComfyUI",
)


# 設定檔。這支「不會」出現在更新包裡，所以升級不會洗掉你的設定。
# 內容就是一行路徑（絕對或相對於腳本目錄）。介面存檔時寫的是這個。
ROOT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "comfy_root.txt")


def read_root_file():
    try:
        with open(ROOT_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.split("#")[0].strip().strip('"')
                if line:
                    return line
    except OSError:
        pass
    return ""


def write_root_file(path):
    """介面用這支存設定。傳空字串 = 刪掉設定，回到自動尋找。"""
    try:
        if not (path or "").strip():
            if os.path.exists(ROOT_FILE):
                os.remove(ROOT_FILE)
            return True, "已清除設定，改回自動尋找"
        with open(ROOT_FILE, "w", encoding="utf-8") as f:
            f.write("# ComfyUI 本體的位置。這個檔案不會被更新包覆蓋。\n")
            f.write("# 可以是絕對路徑，或相對於這個資料夾的路徑（例 ..\\ComfyUI）。\n")
            f.write(path.strip() + "\n")
        return True, f"已寫入 {ROOT_FILE}"
    except OSError as ex:
        return False, f"寫入失敗：{ex}"


def is_comfy_root(d):
    """嚴格判斷：確實是一棵 ComfyUI 本體"""
    try:
        return all(os.path.exists(os.path.join(d, m)) for m in CORE_MARKERS)
    except (OSError, TypeError):
        return False


def looks_like_comfy(d):
    """
    寬鬆判斷：看起來「曾經是」ComfyUI，只是本體檔案被刪了。

    把 main.py 砍掉準備重裝是很常見的動作，這時嚴格判斷會認不出來，
    自動尋找就會退回腳本目錄 —— 然後 venv、install 全部跑錯地方。
    有 custom_nodes 又有 models 或 user，就夠了。
    """
    try:
        if is_comfy_root(d):
            return True
        has_nodes = os.path.isdir(os.path.join(d, "custom_nodes"))
        has_data = (os.path.isdir(os.path.join(d, "models"))
                    or os.path.isdir(os.path.join(d, "user"))
                    or os.path.isdir(os.path.join(d, "venv")))
        return has_nodes and has_data
    except (OSError, TypeError):
        return False


def _expand(cand):
    """相對路徑以「腳本目錄」為基準，不是以當前工作目錄。

    可攜碟插到別台電腦，磁碟代號會變（D: → E:），絕對路徑就失效了；
    寫成「上一層的 ComfyUI」這種相對路徑則永遠正確。
    """
    cand = os.path.expanduser(cand)
    if not os.path.isabs(cand):
        cand = os.path.join(TOOL_DIR, cand)
    return os.path.abspath(cand)


def _autodetect():
    """
    沒設定時自己找。回傳找到的目錄，找不到回 None。

    只找「附近」，而且範圍刻意收得很窄：
        1. 腳本自己的目錄            （工具放在 ComfyUI 裡，舊放法）
        2. 腳本目錄底下一層          （工具在上層，ComfyUI 是子資料夾）
        3. 腳本目錄的上一層          （工具是 ComfyUI 的子資料夾）
        4. 上一層的其他子資料夾      （工具和 ComfyUI 並排）

    不往上爬更多層。一路往上掃會在磁碟根目錄那種地方撞到別人的
    ComfyUI，認錯目錄比找不到更糟 —— 找不到至少會提示你去設定。
    """
    def kids(d):
        try:
            return [os.path.join(d, n) for n in sorted(os.listdir(d))]
        except OSError:
            return []

    def candidates():
        yield TOOL_DIR
        for sub in kids(TOOL_DIR):
            if os.path.isdir(sub):
                yield sub
        up = os.path.dirname(TOOL_DIR)
        if up and up != TOOL_DIR:
            yield up
            for sub in kids(up):
                if os.path.isdir(sub):
                    yield sub
        # 慣例位置（Windows 上 ~ 會展開成 C:\Users\你的帳號）
        for rel in COMMON_LOCATIONS:
            yield os.path.abspath(os.path.expanduser(rel))

    cands = list(candidates())
    # 第一輪：嚴格（真的有 main.py 和 comfy/）
    for p in cands:
        if is_comfy_root(p):
            return p
    # 第二輪：寬鬆（本體被刪掉，但 custom_nodes / models / venv 還在）
    for p in cands:
        if looks_like_comfy(p):
            print(f"[INFO] {p} 沒有 main.py，但有 custom_nodes 和使用者資料，"
                  "當成本體目錄")
            return p
    return None


def _resolve_root():
    """
    環境變數 → 設定區（絕對或相對）→ 自動尋找 → 腳本目錄

    ⚠ 明確指定的路徑「無條件採用」，即使那裡還沒有 main.py。

      本體還沒裝、或你剛把本體砍掉準備重裝，正是 install.py 和
      「覆蓋核心」要處理的情況。如果因為「看起來不像 ComfyUI」就把
      設定丟掉改去自動尋找，那兩個功能就永遠裝不到你指定的地方
      —— 還會退回腳本目錄，連 venv 都找錯位置。

      驗證只用在「自動尋找」：那裡是用猜的，猜錯比找不到更糟。
    """
    for src, cand in (("環境變數 COMFY_ROOT",
                       os.environ.get("COMFY_ROOT", "").strip().strip('"')),
                      ("comfy_root.txt", read_root_file()),
                      ("comfy_env.py 的 COMFY_ROOT",
                       (COMFY_ROOT or "").strip().strip('"'))):
        if not cand:
            continue
        p = _expand(cand)
        if is_comfy_root(p):
            return p
        if os.path.isdir(p):
            print(f"[INFO] {src} 指到 {p}")
            print("       裡面還沒有 main.py —— 當成「要安裝到這裡」，"
                  "跑 install.py 會把本體裝進去")
            return p
        if os.path.isdir(os.path.dirname(p)):
            print(f"[INFO] {src} 指到尚未建立的 {p}")
            print("       install.py 會建立這個目錄並把本體 clone 進去")
            return p
        print(f"[WARN] {src} 指到 {p}，連上層目錄都不存在，改用自動尋找")
        break
    found = _autodetect()
    if found:
        return found
    return TOOL_DIR


ROOT = _resolve_root()
SPLIT = os.path.normcase(ROOT) != os.path.normcase(TOOL_DIR)

# 本體底下的東西
NODES_DIR = os.path.join(ROOT, "custom_nodes")
DISABLED_DIR = os.path.join(NODES_DIR, ".disabled")   # ComfyUI-Manager 停用區
VENV_DIR = os.path.join(ROOT, "venv")

# 我們自己產生的東西：跟腳本放一起，這樣重裝 / reset 本體都不會波及
LOG_DIR = os.path.join(TOOL_DIR, "update_logs")
STATE_DIR = os.path.join(TOOL_DIR, "env_state")       # install.py 重建用快照

# wheels/ 先找腳本目錄，沒有才找本體目錄（相容舊的放法）
WHEELS_DIR = os.path.join(TOOL_DIR, "wheels")
if not os.path.isdir(WHEELS_DIR) and os.path.isdir(os.path.join(ROOT, "wheels")):
    WHEELS_DIR = os.path.join(ROOT, "wheels")
NODES_MANIFEST = os.path.join(STATE_DIR, "nodes.json")
PIP_FREEZE = os.path.join(STATE_DIR, "pip_freeze.txt")
CONSTRAINTS = os.path.join(STATE_DIR, "constraints.txt")

PIP = [sys.executable, "-m", "pip"]


# ══════════════════════════ 基本工具 ══════════════════════════

# 無 console 模式（pythonw / console=False 的 EXE）下，子程序沒帶這個旗標
# 就會被 Windows 另外配一個 console —— 畫面上就是一閃而過的黑窗。
# 節點掃描會對每個節點跑一次 git，四十個節點就是四十個黑窗連閃。
NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          creationflags=NOWIN)


def last_err(r, n=6):
    """從失敗的指令輸出抓最有用的幾行。

    細節通常在標題行的「後面」，不是標題行本身。例如 git 的：
        error: Your local changes to the following files would be overwritten by merge:
                main.py            ← 真正要看的是這幾行
                requirements.txt
    所以找到第一行錯誤後，連同後續幾行一起帶出來。
    """
    text = (r.stderr or "") + "\n" + (r.stdout or "")
    lines = [l.strip() for l in text.splitlines()
             if l.strip() and not l.lstrip().startswith("[notice]")]
    if not lines:
        return f"exit code {r.returncode}"
    for i, l in enumerate(lines):
        if l.startswith(("ERROR", "error", "fatal", "Aborting")):
            return " | ".join(lines[i:i + n])
    return " | ".join(lines[-n:])


def header(title):
    print()
    print("=" * 54)
    print(f"  {title}")
    print("=" * 54)


def section(title):
    print()
    print(f"【{title}】")


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _v(s):
    if Version is None or not s:
        return None
    try:
        return Version(s)
    except InvalidVersion:
        return None


def same_version(a, b):
    va, vb = _v(a), _v(b)
    if va is not None and vb is not None:
        return va == vb
    return a == b


def arrow(old, new):
    vo, vn = _v(old), _v(new)
    return "↓" if (vo is not None and vn is not None and vn < vo) else "↑"


# ══════════════════════════ 紀錄檔 ══════════════════════════

class _Tee:
    def __init__(self, stream, fh):
        self.stream, self.fh = stream, fh

    def write(self, s):
        self.stream.write(s)
        if not self.fh.closed:
            self.fh.write(s)
        return len(s)

    def flush(self):
        self.stream.flush()
        if not self.fh.closed:
            self.fh.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def start_log(kind):
    """之後所有 print 同時寫入 update_logs/<kind>_時間.log"""
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR,
                        f"{kind}_{datetime.datetime.now():%Y%m%d_%H%M%S}.log")
    fh = open(path, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.stdout, fh)
    atexit.register(fh.close)
    return path


def log_only(text):
    """只寫紀錄檔、不顯示在畫面（長清單用）"""
    if isinstance(sys.stdout, _Tee) and not sys.stdout.fh.closed:
        sys.stdout.fh.write(text + "\n")


# ══════════════════════════ 套件快照 ══════════════════════════

_SNAP_CODE = (
    "import json, importlib.metadata as m\n"
    "d = {}\n"
    "for x in m.distributions():\n"
    "    n = x.metadata['Name']\n"
    "    if n: d[n] = x.version\n"
    "print(json.dumps(d))"
)


def snapshot():
    """{正規化套件名: 版本}，用子行程讀，確保拿到 pip 剛寫入的狀態"""
    r = run([sys.executable, "-c", _SNAP_CODE])
    try:
        raw = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f"無法讀取已安裝套件清單：{last_err(r)}")
    return {norm(k): v for k, v in raw.items()}


def diff(before, after):
    added = {k: after[k] for k in after.keys() - before.keys()}
    removed = {k: before[k] for k in before.keys() - after.keys()}
    changed = {k: (before[k], after[k]) for k in before.keys() & after.keys()
               if before[k] != after[k]}
    return added, removed, changed


def fmt_changes(added, removed, changed):
    out = [f"+ {k} {v}" for k, v in sorted(added.items())]
    out += [f"{arrow(o, n)} {k} {o} → {n}" for k, (o, n) in sorted(changed.items())]
    out += [f"- {k} {v}" for k, v in sorted(removed.items())]
    return out


def print_changes(lines, indent="        ", limit=8):
    for l in lines[:limit]:
        print(indent + l)
    if len(lines) > limit:
        print(f"{indent}…共 {len(lines)} 項，完整清單見紀錄檔")
        for l in lines[limit:]:
            log_only(indent + l)


# ══════════════════════════ 事件紀錄 ══════════════════════════

class Report:
    def __init__(self):
        self.pkg_changes = []   # (stage, source, added, removed, changed)
        self.restored = []      # dict(stage, culprit, why, detail, ok)
        self.purged = []        # dict(stage, culprit, pkg, ver, reason, ok)
        self.pip_fail = []      # (stage, source, msg)


# ══════════════════════════ 安裝 + 守門 ══════════════════════════

def constraint_args():
    if not USE_CONSTRAINTS:
        return []
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(CONSTRAINTS, "w", encoding="utf-8") as f:
        f.write("# 自動產生（comfy_env.py）：禁止任何套件換掉 torch、或把 numpy 等降版\n")
        for p, v in TORCH_PINS.items():
            f.write(f"{p}=={v}\n")
        for p, v in FLOOR_PINS.items():
            f.write(f"{p}>={v}\n")
    return ["-c", CONSTRAINTS]


def req_lines(path):
    """
    讀 requirements.txt 的原始行（保留註解與空行，給改寫用）。

    節點作者的編碼什麼都有 —— UTF-8、帶 BOM 的 UTF-8、UTF-16（Windows
    記事本「Unicode」存檔的預設）、CP950。一個節點的檔案編碼怪，不該
    讓整次更新當掉，所以這裡逐一嘗試，最後退回 latin-1（永遠不會失敗，
    純 ASCII 的套件名仍然讀得對）。
    """
    try:
        raw = open(path, "rb").read()
    except OSError:
        return []
    # UTF-16 先靠 BOM 認出來。不能把它排進一般的嘗試序列——
    # 沒有 BOM 的 UTF-8 內容只要長度是偶數，用 utf-16 解也「會成功」，
    # 只是解出一堆亂碼，那比直接失敗更糟。
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16").splitlines()
        except UnicodeError:
            pass
    for enc in ("utf-8-sig", "utf-8", "cp950", "latin-1"):
        try:
            return raw.decode(enc).splitlines()
        except (UnicodeDecodeError, UnicodeError):
            continue
    return []


def apply_req_aliases(path, snap=None):
    """
    依 REQ_ALIASES 改寫節點的 requirements.txt。

    回傳 (要交給 pip 的檔案路徑, [說明字串])。
    沒有任何一行需要改寫時，直接回傳原路徑，不產生暫存檔。
    暫存檔寫在 STATE_DIR，不碰節點目錄。
    """
    if not REQ_ALIASES or not os.path.exists(path):
        return path, []
    snap = snap if snap is not None else snapshot()
    alias = {norm(k): v for k, v in REQ_ALIASES.items()}
    bl = {norm(k): v for k, v in BLOCKLIST.items()}

    out, notes, changed = [], [], False
    for line in req_lines(path):
        body = line.split("#", 1)[0].strip()
        if not body:
            out.append(line)
            continue
        # 取套件名：切在第一個版本/環境標記符號之前
        name = re.split(r"[\s\[<>=!~;]", body, 1)[0].strip()

        # 黑名單先剔除，不要等裝完再移除。
        # 有些套件「裝了就回不去」——例如四個 opencv 變體共用同一個
        # site-packages/cv2/，裝下去就把前一個蓋掉，事後解除安裝只會
        # 留下一個殘缺的 cv2。先攔住才有意義。
        if norm(name) in bl:
            changed = True
            notes.append(f"{name} → 黑名單，未送進 pip（{bl[norm(name)]}）")
            continue

        hit = alias.get(norm(name))
        if not hit:
            out.append(line)
            continue
        real, why = hit
        changed = True
        if norm(real) in snap:
            notes.append(f"{name} → 已由 {real} {snap[norm(real)]} 滿足，略過（{why}）")
        else:
            notes.append(f"{name} → 改裝 {real}（{why}）")
            out.append(real)

    if not changed:
        return path, []
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = os.path.join(STATE_DIR, "req_alias.txt")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    except OSError as ex:
        return path, [f"（改寫失敗，改用原檔：{ex}）"]
    return tmp, notes


def reqs_satisfied(lines, snap):
    """
    這幾行 requirements 是不是「全部已經滿足」？

    只要有一行無法判斷（URL、git+、-e、本地路徑、解析失敗），
    就回傳 False，交給 pip 去處理 —— 寧可多跑一次，不要漏裝。
    """
    if Requirement is None:
        return False
    for raw in lines:
        s = raw.split("#")[0].strip()
        if not s:
            continue
        if s.startswith("-") or "://" in s or s.startswith(".") or "@" in s:
            return False
        try:
            r = Requirement(s)
        except Exception:
            return False
        if r.marker is not None:
            try:
                if not r.marker.evaluate():
                    continue
            except Exception:
                return False
        have = snap.get(norm(r.name))
        if have is None:
            return False
        if r.specifier:
            try:
                if not r.specifier.contains(have.split("+")[0], prereleases=True):
                    return False
            except Exception:
                return False
    return True


def install_node_reqs(rep, m, stage="節點依賴", snap=None):
    """
    裝一個節點的 requirements.txt。

    先套用 REQ_ALIASES / BLOCKLIST 改寫，然後判斷是否「全部已滿足」：
    已滿足就完全不呼叫 pip。

    這不只是為了快。pip install -r 就算目標套件都已經裝好，
    仍然會重新檢查「它們的相依」—— 例如 insightface、ultralytics
    宣告了 opencv-python，而本機刻意不裝它（用 contrib 代替），
    pip 就會每次都把它補回來，工具再把它移除，無限循環。
    不呼叫 pip，循環就不會開始。

    回傳 "skip"（已滿足，未呼叫 pip）、True（安裝成功）、False（失敗）
    """
    src = os.path.join(m["path"], "requirements.txt")
    snap = snap if snap is not None else snapshot()
    use, notes = apply_req_aliases(src, snap=snap)
    if reqs_satisfied(req_lines(use), snap):
        for n in notes:
            log_only(f"[INFO] {m['name']}：{n}")
        return "skip"
    for n in notes:
        print(f"[INFO] {m['name']}：{n}")
    return install_tracked(rep, ["-r", use], stage, m["name"])


def install_tracked(rep, pip_args, stage, source, constrained=True):
    """pip install 並記下這一次讓哪些套件變動，接著立刻守門（歸咎到 source）"""
    before = snapshot()
    cmd = PIP + ["install"] + pip_args + ["-q"]
    if constrained:
        cmd += constraint_args()
    r = run(cmd)
    after = snapshot()
    a, rm, ch = diff(before, after)
    if a or rm or ch:
        rep.pkg_changes.append((stage, source, a, rm, ch))
        print(f"[INFO] {source}：套件異動 {len(a) + len(rm) + len(ch)} 項")
        print_changes(fmt_changes(a, rm, ch))
    if r.returncode != 0:
        msg = last_err(r)
        rep.pip_fail.append((stage, source, msg))
        print(f"[WARN] {source}：安裝失敗 — {msg}")
    guard(rep, stage, culprit=source, snap=after)
    return r.returncode == 0


def torch_status(snap):
    for pkg, want in TORCH_PINS.items():
        v = snap.get(norm(pkg))
        if not v:
            return False, f"{pkg} 未安裝"
        if v.split("+")[0] != want:
            return False, f"{pkg} 是 {v}，應為 {want}"
        if CUDA_TAG not in v:
            return False, f"{pkg} 是 {v}，不是 {CUDA_TAG} build"
    return True, ""


def fix_torch(rep, stage, culprit=None, snap=None):
    snap = snap if snap is not None else snapshot()
    ok, why = torch_status(snap)
    if ok:
        return True
    before = {p: snap.get(norm(p)) for p in TORCH_PINS}
    print(f"[WARN] PyTorch 不符鎖定（{stage}）：{why}")
    spec = [f"{p}=={v}" for p, v in TORCH_PINS.items()]
    print(f"[INFO] 還原：{' '.join(spec)}（{TORCH_INDEX}）")
    run(PIP + ["uninstall"] + list(TORCH_PINS) + ["-y"])
    r = run(PIP + ["install"] + spec + ["--index-url", TORCH_INDEX])
    after = snapshot()
    ok2, why2 = torch_status(after)
    detail = "、".join(
        f"{p} {before[p] or '未安裝'} → {after.get(norm(p)) or '未安裝'}"
        for p in TORCH_PINS if before[p] != after.get(norm(p)))
    rep.restored.append(dict(what="PyTorch", stage=stage, culprit=culprit, why=why,
                             detail=detail or "（版本未變）", ok=ok2))
    if ok2:
        print(f"[INFO] 已還原：{detail}")
    else:
        print(f"[ERROR] 還原失敗：{why2} — {last_err(r)}")
    return ok2


def purge_blocklist(rep, stage, culprit=None, snap=None):
    snap = snap if snap is not None else snapshot()
    hits = [(p, snap[norm(p)]) for p in BLOCKLIST if norm(p) in snap]
    if not hits:
        return False
    print(f"[WARN] 偵測到黑名單套件（{stage}" +
          (f"，來源：{culprit}" if culprit else "") + "）：")
    for p, v in hits:
        print(f"        {p} {v} — {BLOCKLIST[p]}")
    run(PIP + ["uninstall"] + [p for p, _ in hits] + ["-y"])
    after = snapshot()
    for p, v in hits:
        ok = norm(p) not in after
        rep.purged.append(dict(stage=stage, culprit=culprit, pkg=p, ver=v,
                               reason=BLOCKLIST[p], ok=ok))
    left = [p for p, _ in hits if norm(p) in after]
    if left:
        print(f"[ERROR] 移除失敗：{', '.join(left)}")
    else:
        print(f"[INFO] 已移除 {len(hits)} 個套件")
    repair_shared_dirs(rep, stage, culprit, [p for p, _ in hits], after)
    return True


# 共用安裝目錄的套件：移除其中一個之後，另一個也會殘缺。
#   四個 opencv 變體全部裝進同一個 site-packages/cv2/。
#   pip 認為它們是不同套件，但檔案是同一批，後裝的覆蓋先裝的；
#   解除安裝時 pip 只照自己的紀錄刪，留下一個殘缺的 cv2——
#   典型症狀就是 import cv2 成功、cv2.ximgproc.guidedFilter 卻不見了。
# 所以移除之後必須把「本機該用的那一個」強制重裝，把檔案補回去。
SHARED_DIR_REPAIR = {
    "opencv-python-headless":         "opencv-contrib-python",
    "opencv-contrib-python-headless": "opencv-contrib-python",
    "opencv-python":                  "opencv-contrib-python",
}


def repair_shared_dirs(rep, stage, culprit, removed, snap=None):
    """移除共用目錄的套件之後，把正牌的那一個補回來"""
    want = {SHARED_DIR_REPAIR[p] for p in removed if p in SHARED_DIR_REPAIR}
    if not want:
        return
    snap = snap if snap is not None else snapshot()
    for pkg in sorted(want):
        have = snap.get(norm(pkg))
        if not have:
            continue        # 本來就沒裝，不是我們弄壞的，別自作主張裝上
        print(f"[INFO] {pkg} {have} 與剛移除的套件共用 site-packages，"
              "強制重裝以補回被覆蓋的檔案…")
        cmd = PIP + ["install", "--force-reinstall", "--no-deps",
                     f"{pkg}=={have}", "-q"]
        if USE_CONSTRAINTS:
            cmd += constraint_args()
        r = run(cmd)
        if r.returncode == 0:
            print(f"[OK]   {pkg} 已重裝")
        else:
            msg = last_err(r)
            print(f"[ERROR] {pkg} 重裝失敗 — {msg}")
            rep.pip_fail.append((stage, culprit or pkg, f"{pkg} 重裝失敗：{msg}"))


def floor_status(snap):
    """回傳 [(套件, 目前版本, 下限)]，只列出低於下限的"""
    bad = []
    for p, floor in FLOOR_PINS.items():
        have = snap.get(norm(p))
        if not have:
            continue            # 沒裝就不管，需要它的節點自己會裝
        vh, vf = _v(have), _v(floor)
        if vh is not None and vf is not None and vh < vf:
            bad.append((p, have, floor))
    return bad


def fix_floors(rep, stage, culprit=None, snap=None):
    """把被降版的套件（numpy 等）升回下限以上。
    pip uninstall 黑名單套件不會還原它降掉的相依套件，所以這步是必要的。"""
    snap = snap if snap is not None else snapshot()
    bad = floor_status(snap)
    if not bad:
        return True
    print(f"[WARN] 偵測到套件被降版（{stage}" +
          (f"，來源：{culprit}" if culprit else "") + "）：")
    for p, have, floor in bad:
        print(f"        {p} {have}，下限 {floor}")
    spec = [f"{p}>={floor}" for p, _, floor in bad]
    print(f"[INFO] 升回：{' '.join(spec)}")
    run(PIP + ["install", "--upgrade"] + spec + constraint_args())
    after = snapshot()
    ok_all = True
    for p, have, floor in bad:
        now = after.get(norm(p))
        ok = not any(x[0] == p for x in floor_status(after))
        ok_all = ok_all and ok
        rep.restored.append(dict(what=p, stage=stage, culprit=culprit,
                                 why=f"被降到 {have}（下限 {floor}）",
                                 detail=f"{p} {have} → {now or '未安裝'}", ok=ok))
        print(f"[{'INFO' if ok else 'ERROR'}] {p} {have} → {now or '未安裝'}")
    return ok_all


def guard(rep, stage, culprit=None, snap=None):
    snap = snap if snap is not None else snapshot()
    purge_blocklist(rep, stage, culprit, snap)
    fix_torch(rep, stage, culprit, snap)   # 移除黑名單不會動到 torch，快照可沿用
    fix_floors(rep, stage, culprit)        # 但可能動到 numpy，要重拍快照


# ══════════════════════════ git / 核心 ══════════════════════════

def resolve(ref, cwd=None):
    r = run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd)
    return r.stdout.strip() or None


def current_branch(cwd=None):
    return run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd).stdout.strip()


def upstream_ref(cwd=None):
    """目前分支的上游（origin/xxx）；沒設定就用 origin 的預設分支"""
    r = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return f"origin/{default_branch(cwd)}"


def default_branch(cwd=None):
    r = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd)
    if r.returncode == 0 and "/" in r.stdout:
        return r.stdout.strip().split("/", 1)[1]
    for b in ("master", "main"):
        if resolve(f"origin/{b}", cwd):
            return b
    return "master"


def count(rng, cwd=None):
    out = run(["git", "rev-list", "--count", rng], cwd).stdout.strip()
    return int(out) if out.isdigit() else 0


_XY = {"M": "修改", "A": "新增", "D": "刪除", "R": "改名", "C": "複製", "U": "衝突"}


def dirty_label(xy):
    """把 git status 的兩欄代碼翻成人話。
    第一欄 = 暫存區（已 git add），第二欄 = 工作區（未 add）。
    這個差別會決定還原指令：已暫存的要用 git restore --staged --worktree，
    git checkout -- 對它無效（會報 did not match any file(s)）。"""
    x, y = (xy + "  ")[0], (xy + "  ")[1]
    parts = []
    if x.strip():
        parts.append("已暫存的" + _XY.get(x, x))
    if y.strip():
        parts.append("未暫存的" + _XY.get(y, y))
    return "、".join(parts) or xy


def dirty_files(cwd=None):
    """已追蹤但有本地改動的檔案（不含未追蹤的新檔）→ [(兩欄狀態, 檔名)]
    本體更新前先問這個，就能在 merge 失敗前先點名是哪些檔案卡住。
    狀態保留原本兩欄（例 "M " / " D"），位置本身帶有意義，不要 strip。"""
    r = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd)
    out = []
    for l in r.stdout.splitlines():
        if l.strip():
            out.append((l[:2], l[3:].strip().strip('"')))
    return out


def is_placeholder(path):
    base = os.path.basename(path.replace("\\", "/"))
    return any(fnmatch.fnmatch(base, g) for g in PLACEHOLDER_GLOBS)


def restore_placeholders(cwd=None):
    """把「被刪掉」的空佔位檔還原，回傳實際還原的清單。

    只處理狀態含 D（刪除）且檔名符合 PLACEHOLDER_GLOBS 的檔案。
    被修改（M）的檔案一律不碰——那可能是真的程式碼改動。
    這類檔案在 ComfyUI 裡是空的，還原不會覆蓋掉任何東西。
    """
    targets = [fn for xy, fn in dirty_files(cwd)
               if "D" in xy and is_placeholder(fn)]
    if not targets:
        return []
    done = []
    for fn in targets:
        # restore --staged --worktree 才能同時處理「已暫存」和「未暫存」的刪除
        r = run(["git", "restore", "--staged", "--worktree", "--", fn], cwd)
        if r.returncode != 0:      # 舊版 git 沒有 restore
            r = run(["git", "checkout", "--", fn], cwd)
        if r.returncode == 0:
            done.append(fn)
    return done


def read_core_version():
    p = os.path.join(ROOT, "comfyui_version.py")
    try:
        with open(p, encoding="utf-8") as f:
            m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", f.read())
            return m.group(1) if m else "?"
    except OSError:
        return "?"


def core_info():
    r = run(["git", "log", "-1", "--date=short", "--format=%H%x1f%h%x1f%ad%x1f%s"])
    parts = r.stdout.strip().split("\x1f")
    if len(parts) < 4:
        parts = ["", "?", "?", "（無法讀取 git）"]
    return dict(hash=parts[0], short=parts[1], date=parts[2], subject=parts[3],
                branch=current_branch(), version=read_core_version())


def checkout_core_pin():
    """把核心切到 PIN_CORE；回傳 (ok, 說明)"""
    target = resolve(PIN_CORE)
    if not target:
        run(["git", "fetch", "origin", "--tags"])
        target = resolve(PIN_CORE)
    if not target:
        return False, f"找不到 {PIN_CORE}"
    if resolve("HEAD") == target:
        return True, "已在鎖定版本"
    r = run(["git", "checkout", "--detach", target])
    if r.returncode != 0:
        return False, f"切換失敗：{last_err(r)}"
    return True, "已切換到鎖定版本"


# ══════════════════════════ 本體取得 / 覆蓋 ══════════════════════════
#
# 本體永遠是一棵 git 工作樹，不是 pip 套件。
#   PyPI 上的 comfyui 0.0.1 是 2024-06 的第三方 fork，別用。
#   官方的 comfy-cli 底層也只是幫你 git clone。
#
# 這裡只做兩件事：缺檔案就補一棵 git 樹、要覆蓋就 reset --hard。
# 絕對不跑 git clean —— 本體的 .gitignore 只蓋掉 models/ user/ custom_nodes/
# 之類的目錄，這套管理腳本（comfy_env.py、*.bat、env_state/、wheels/…）
# 對 git 來說全是未追蹤檔案，clean 一下會全部消失。
# reset --hard 只動「被追蹤的」檔案，所以是安全的。

CORE_FILES = ("main.py", "nodes.py", "comfy", "comfy_extras")


def core_present():
    """本體原始碼在不在（不管 git）"""
    return all(os.path.exists(os.path.join(ROOT, f)) for f in CORE_FILES)


def has_git_dir():
    return os.path.isdir(os.path.join(ROOT, ".git"))


def git_available():
    return shutil.which("git") is not None


def core_remote():
    r = run(["git", "remote", "get-url", "origin"])
    return r.stdout.strip() if r.returncode == 0 else ""


def narrow_fetch_refspec():
    """
    把 origin 的 fetch 範圍縮成「只有本體分支」。

    預設的 refspec 是 +refs/heads/*:refs/remotes/origin/*，會把上游
    所有開發者的分支都抓下來（ComfyUI 有好幾百個）。這些分支一個都用不到，
    卻會帶來兩個實際問題：

      1. fetch 很慢
      2. 其中任何一個的本地參照只要與上游對不上，整個 fetch 就會失敗：
             cannot lock ref 'refs/remotes/origin/<某人的分支>':
             is at X but expected Y
         於是本體更新被一個與你無關的分支擋住。

    縮窄之後 fetch 只碰 CORE_BRANCH，上述情況不會再發生。
    tag 仍然照抓（fetch 時帶 --tags），所以 PIN_CORE 鎖 tag 不受影響。

    回傳 (有沒有改, 說明)
    """
    want = f"+refs/heads/{CORE_BRANCH}:refs/remotes/origin/{CORE_BRANCH}"
    r = run(["git", "config", "--get-all", "remote.origin.fetch"])
    cur = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    if cur == [want]:
        return False, ""
    r = run(["git", "config", "--replace-all", "remote.origin.fetch", want])
    if r.returncode != 0:
        return False, f"（refspec 調整失敗：{last_err(r)}）"
    # 縮窄之後，先前抓下來的其他 origin/* 參照變成孤兒，順手清掉——
    # 壞掉的那些也會一併消失。prune 失敗不影響主流程。
    run(["git", "remote", "prune", "origin"])
    return True, f"fetch 範圍縮為 origin/{CORE_BRANCH}"


def sync_core_remote():
    """把 origin 指到 CORE_REPO 並縮窄 fetch 範圍；回傳 (有沒有改, 說明)"""
    cur = core_remote()
    if not cur:
        r = run(["git", "remote", "add", "origin", CORE_REPO])
        if r.returncode != 0:
            return False, f"新增 origin 失敗：{last_err(r)}"
        narrow_fetch_refspec()
        return True, f"新增 origin → {CORE_REPO}"

    def key(u):
        return u.rstrip("/").removesuffix(".git").lower().split("github.com")[-1].strip(":/")

    narrowed, nmsg = narrow_fetch_refspec()
    tail = f"，{nmsg}" if narrowed else ""

    if key(cur) == key(CORE_REPO):
        return narrowed, f"origin 已正確（{cur}）{tail}"
    r = run(["git", "remote", "set-url", "origin", CORE_REPO])
    if r.returncode != 0:
        return False, f"改 origin 失敗：{last_err(r)}"
    return True, f"origin 由 {cur} 改為 {CORE_REPO}{tail}"


def clone_core_into_root():
    """
    在「已經有東西」的目錄裡長出一棵 git 樹。

    git clone 不肯寫進非空目錄，所以先 --no-checkout clone 到暫存資料夾，
    把裡面的 .git 搬進根目錄，再 checkout -f 把被追蹤的檔案寫出來。
    checkout 只寫被追蹤的檔案，models/ user/ custom_nodes/ 這些不在版控裡，
    原封不動。

    回傳 (ok, 說明)
    """
    if has_git_dir():
        return False, ".git 已存在，不該走到這裡"
    os.makedirs(ROOT, exist_ok=True)
    tmp = os.path.join(ROOT, ".core_clone_tmp")
    if os.path.exists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"[INFO] git clone {CORE_REPO}（分支 {CORE_BRANCH}）…")
    r = run(["git", "clone", "--no-checkout", "--branch", CORE_BRANCH, CORE_REPO, tmp])
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return False, f"clone 失敗：{last_err(r)}"
    try:
        shutil.move(os.path.join(tmp, ".git"), os.path.join(ROOT, ".git"))
    except OSError as ex:
        shutil.rmtree(tmp, ignore_errors=True)
        return False, f"搬移 .git 失敗：{ex}"
    shutil.rmtree(tmp, ignore_errors=True)
    # checkout -f 會覆寫同名的未追蹤檔案，這正是「目錄裡已經有一份
    # 非 git 的本體」時要的行為。萬一 git 仍然拒絕（版本差異、
    # 檔案被佔用等），退回 reset --hard，它一定會把工作樹寫出來。
    r = run(["git", "checkout", "-f", CORE_BRANCH])
    if r.returncode != 0:
        r2 = run(["git", "reset", "--hard", f"origin/{CORE_BRANCH}"])
        if r2.returncode != 0:
            return False, (f"checkout 失敗：{last_err(r)}"
                           f"；改用 reset 也失敗：{last_err(r2)}")
    if not core_present():
        missing = [f for f in CORE_FILES if not os.path.exists(os.path.join(ROOT, f))]
        return False, f"checkout 完成但還是缺：{', '.join(missing)}"
    return True, "已建立 git 工作樹並取出本體"


# ══════════════════════ 本體安全修補 ══════════════════════
#
# ComfyUI 內建的 StringFormat（Format Text）節點做的是：
#     f_string.format(**values)        comfy_extras/nodes_string.py
# 兩個參數都來自工作流，而 str.format 允許 {a.b} 和 {a[k]}，
# 所以惡意工作流可以從傳進去的物件爬到 ctypes，載入
# input/ 底下上傳的 DLL —— 任意原生程式碼執行。上游至今未修。
#
# 每次更新 / 覆蓋核心之後會自動重新套用。必須自動，因為
# git reset --hard 會把修補連同本地提交一起丟掉。

PATCH_TARGET = os.path.join("comfy_extras", "nodes_string.py")
PATCH_OLD = "        return io.NodeOutput(f_string.format(**values))"
PATCH_NEW = """\
        # 安全性修補（comfy_env.py 自動套用，上游尚未修正）
        # str.format 允許 {a.b} 與 {a[k]}，而 f_string 和 values
        # 都來自工作流，未加限制時可被用來執行任意程式碼。
        import string as _string

        class _SafeFormatter(_string.Formatter):
            def get_field(self, field_name, args, kwargs):
                for _ch in ".[":
                    if _ch in field_name:
                        raise ValueError(
                            "格式字串不允許屬性存取或索引：{%s}"
                            % field_name)
                return super().get_field(field_name, args, kwargs)

        return io.NodeOutput(_SafeFormatter().vformat(f_string, (), values))"""


def patch_status():
    """回傳 ('patched' | 'vulnerable' | 'unknown' | 'missing', 路徑)"""
    p = os.path.join(ROOT, PATCH_TARGET)
    if not os.path.exists(p):
        return "missing", p
    try:
        src = open(p, encoding="utf-8").read()
    except OSError:
        return "unknown", p
    if "_SafeFormatter" in src:
        return "patched", p
    if PATCH_OLD in src:
        return "vulnerable", p
    return "unknown", p


def unpatch_for_update():
    """
    更新本體前，把自己套的安全修補暫時撤掉。

    修補是「未提交的修改」，git 會把它當成本地改動而拒絕更新，
    結果工具被自己的修補擋住。這裡確認那個檔案「除了修補之外
    沒有別的差異」才還原 —— 有任何其他改動就不碰，照常回報給使用者。
    更新完成後 apply_core_patch() 會再補回去。

    回傳 True 表示已撤下。
    """
    state, p = patch_status()
    if state != "patched":
        return False
    rel = PATCH_TARGET.replace(os.sep, "/")
    r = run(["git", "show", f"HEAD:{rel}"])
    if r.returncode != 0:
        return False
    try:
        cur = open(p, encoding="utf-8").read()
    except OSError:
        return False
    nl = lambda s: s.replace("\r\n", "\n")
    if nl(cur).replace(nl(PATCH_NEW), nl(PATCH_OLD), 1) != nl(r.stdout):
        return False        # 還有修補以外的改動，交給使用者決定
    return run(["git", "checkout", "--", rel]).returncode == 0


def apply_core_patch(quiet=False):
    """套用 StringFormat 的安全修補。回傳 (ok, 說明)"""
    state, p = patch_status()
    if state == "patched":
        if not quiet:
            print("[OK]   StringFormat 安全修補：已套用")
        return True, "已套用"
    if state == "missing":
        return False, f"找不到 {PATCH_TARGET}"
    if state == "unknown":
        msg = (f"{PATCH_TARGET} 認不出要修補的那一行，"
               "上游可能已經自己修掉，請人工確認")
        print(f"[WARN] {msg}")
        return False, msg
    try:
        src = open(p, encoding="utf-8").read()
        new = src.replace(PATCH_OLD, PATCH_NEW, 1)
        import ast as _ast
        _ast.parse(new)
        with open(p, "w", encoding="utf-8") as f:
            f.write(new)
    except Exception as ex:
        print(f"[ERROR] 安全修補套用失敗：{ex}")
        return False, str(ex)
    print("[INFO] 已重新套用 StringFormat 安全修補")
    return True, "已重新套用"


def ensure_core_tree():
    """
    把 ROOT 變成一棵「與上游一致」的 ComfyUI 工作樹。

    install.py 和介面的「覆蓋核心」共用這支，所以兩邊行為一定相同：
        沒有 .git        → 建立目錄並 clone（目錄非空也沒關係）
        有 .git 沒檔案   → fetch + reset
        有 .git 有檔案   → 同步 origin 後 fetch + reset（丟掉本地改動）
    回傳 (ok, 說明)
    """
    if not git_available():
        return False, "找不到 git，請先安裝：https://git-scm.com/download/win"
    os.makedirs(ROOT, exist_ok=True)
    if not has_git_dir():
        # 這裡以前會在「有本體檔案但沒有 .git」時直接拒絕，要人先手動刪檔。
        # 那是錯的：clone_core_into_root() 本來就是為了這個情境寫的
        # （clone 到暫存 → 搬 .git 進來 → 強制取出覆寫），而且這正是
        # 「重建乾淨環境」最需要它動作的時候。擋住只會讓人卡在這一步。
        ok, msg = clone_core_into_root()
        if ok:
            apply_core_patch()
        return ok, msg
    changed, msg = sync_core_remote()
    print(f"[{'INFO' if changed else 'OK'}] {msg}")
    ok, msg = overwrite_core()
    if ok:
        apply_core_patch()      # reset --hard 會把修補丟掉，馬上補回來
    return ok, msg


def overwrite_core():
    """fetch + reset --hard origin/分支。只動被追蹤的檔案。回傳 (ok, 說明)"""
    br = current_branch() or CORE_BRANCH
    if br == "HEAD":        # 處於 detached（PIN_CORE 鎖版過）
        br = CORE_BRANCH
    print(f"[INFO] git fetch origin {br}…")
    r = run(["git", "fetch", "origin", br, "--tags"])
    if r.returncode != 0:
        return False, f"fetch 失敗：{last_err(r)}"
    # 順序和旗標都很關鍵：
    #   先 reset --hard 把工作區清乾淨，再 checkout。
    #   反過來的話，只要本體有任何本地修改，checkout 就會拒絕：
    #     error: Your local changes would be overwritten by checkout
    #   —— 而「覆蓋核心」的本意就是要丟掉那些修改，拒絕等於整個功能失效。
    #   checkout 另外帶 -f，處理 detached HEAD 或分支不存在的情況。
    r = run(["git", "reset", "--hard", f"origin/{br}"])
    if r.returncode != 0:
        return False, f"reset 失敗：{last_err(r)}"
    r = run(["git", "checkout", "-f", "-B", br, f"origin/{br}"])
    if r.returncode != 0:
        return False, f"切到 {br} 失敗：{last_err(r)}"
    # 再 reset 一次：checkout -B 之後確保 HEAD 與上游完全一致
    run(["git", "reset", "--hard", f"origin/{br}"])
    return True, f"本體已覆蓋為 origin/{br}"

def node_meta(name, path):
    meta = dict(name=name, path=path,
                git=os.path.exists(os.path.join(path, ".git")),
                registry=os.path.exists(os.path.join(path, ".tracking")),
                has_req=os.path.exists(os.path.join(path, "requirements.txt")),
                # 沒有 __init__.py 的資料夾 ComfyUI 載不起來，啟動時會丟
                # FileNotFoundError: ...\custom_nodes\XXX\__init__.py
                # （常見於 clone 失敗、或誤把整個 repo 丟進 custom_nodes）
                valid=os.path.exists(os.path.join(path, "__init__.py")),
                version=None)
    pp = os.path.join(path, "pyproject.toml")
    if tomllib and os.path.exists(pp):
        try:
            with open(pp, "rb") as f:
                meta["version"] = tomllib.load(f).get("project", {}).get("version")
        except Exception:
            pass
    return meta


def describe(m):
    tags = []
    if m["version"]:
        tags.append(f"v{m['version']}")
    if m["registry"]:
        tags.append("Registry")
    s = m["name"] + (f"（{'，'.join(tags)}）" if tags else "")
    if not m.get("valid", True):
        s += "　⚠ 沒有 __init__.py，ComfyUI 載不起來（啟動會報錯，建議刪除）"
    return s


def scan_nodes():
    """分類：git / nogit / excluded / disabled"""
    out = {"git": [], "nogit": [], "excluded": [], "disabled": []}
    if not os.path.isdir(NODES_DIR):
        return out
    for name in sorted(os.listdir(NODES_DIR), key=str.lower):
        p = os.path.join(NODES_DIR, name)
        if not os.path.isdir(p) or name.startswith("__"):
            continue
        if name == ".disabled":
            for sub in sorted(os.listdir(p), key=str.lower):
                sp = os.path.join(p, sub)
                if os.path.isdir(sp):
                    out["disabled"].append(node_meta(sub, sp))
            continue
        if name.startswith("."):
            continue
        if name.endswith((".disabled", ".bak")):
            out["disabled"].append(node_meta(name, p))
            continue
        m = node_meta(name, p)
        if name in EXCLUDE_NODES:
            out["excluded"].append(m)
        elif m["git"]:
            out["git"].append(m)
        else:
            out["nogit"].append(m)
    return out


def enabled_nodes(scan):
    return sorted(scan["git"] + scan["nogit"] + scan["excluded"],
                  key=lambda m: m["name"].lower())


# ══════════════════════════ 自訂 wheel ══════════════════════════

_WHL_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.]+)-(?P<ver>[^-]+)-(?:\d[^-]*-)?[^-]+-[^-]+-[^-]+\.whl$",
    re.I)


def local_wheels():
    if not os.path.isdir(WHEELS_DIR):
        return []
    res = []
    for f in sorted(os.listdir(WHEELS_DIR)):
        m = _WHL_RE.match(f)
        if m:
            res.append((f, m.group("name"), m.group("ver")))
    return res


def wheel_status(snap=None):
    """比對 wheels/ 的檔名版本與實際安裝版本。

    兩個陷阱：
    1. 有些自編 wheel 檔名帶 local 標籤（flash_attn-2.9.2+cu132torch2.13.0...），
       但 wheel 內部 METADATA 只寫 2.9.2。拿檔名硬比會永遠不相等，
       於是每次更新都誤判「被覆蓋」並重裝一次。所以只比 public 版本。
    2. 自己重新編譯過的 wheel（comfy_kitchen 0.2.35 → 裝的是 0.2.37）
       是「比檔案新」，不是壞掉，不該報 ✗。
    state: ok（相符）/ newer（已安裝較新）/ bad（不符或未安裝）
    """
    snap = snap if snap is not None else snapshot()
    res = []
    for f, n, v in local_wheels():
        have = snap.get(norm(n))
        vw, vh = _v(v), _v(have)
        if not have:
            state = "bad"
        elif vw is not None and vh is not None:
            # 忽略 local 標籤（+cu132torch...），只看 public 版本
            if vh.public == vw.public:
                state = "ok"
            elif vh.public > vw.public:
                state = "newer"
            else:
                state = "bad"
        else:
            state = "ok" if same_version(v, have) else "bad"
        res.append(dict(file=f, name=n, want=v, have=have,
                        state=state, ok=state in ("ok", "newer")))
    return res


def install_wheels(only=None, skip_newer=True):
    """安裝 wheels/ 底下的 .whl。

    skip_newer=True：已安裝版本比 wheels/ 的檔案「新」時跳過。
    自己重新編譯過的套件（例如 comfy_kitchen 0.2.37，而 wheels/ 裡還是 0.2.35）
    若無條件 --force-reinstall 會被倒退回舊版，這是必須避開的。
    真的要強制覆蓋就傳 skip_newer=False。
    """
    newer = set()
    if skip_newer:
        newer = {w["file"] for w in wheel_status() if w["state"] == "newer"}
    ok_all = True
    for f, _, _ in local_wheels():
        if only and f not in only:
            continue
        if f in newer:
            print(f"[SKIP] {f}：已安裝版本較新，不覆蓋")
            continue
        r = run(PIP + ["install", "--force-reinstall", "--no-deps",
                       os.path.join(WHEELS_DIR, f)])
        if r.returncode == 0:
            print(f"[INFO] 已安裝 wheel：{f}")
        else:
            ok_all = False
            print(f"[ERROR] wheel 安裝失敗：{f} — {last_err(r)}")
    return ok_all


def import_checks():
    res = []
    for name, code in WHEEL_CHECKS:
        r = run([sys.executable, "-c", code])
        if r.returncode == 0:
            res.append((name, True, r.stdout.strip()))
        else:
            lines = (r.stderr or "").strip().splitlines()
            res.append((name, False, lines[-1] if lines else "匯入失敗"))
    return res


def print_wheels(ws):
    if not ws:
        print("  （wheels/ 沒有 .whl）")
    for w in ws:
        st = w.get("state", "ok" if w["ok"] else "bad")
        if st == "ok":
            print(f"  ✓ {w['name']} {w['have']}")
        elif st == "newer":
            print(f"  ✓ {w['name']} {w['have']}"
                  f"（已安裝版本比 wheels/ 的 {w['want']} 新，不需重裝）")
        else:
            print(f"  ✗ {w['name']}：wheel 為 {w['want']}，目前 {w['have'] or '未安裝'}")


# ══════════════════════════ 環境快照 ══════════════════════════

def write_state(scan=None, snap=None):
    """寫 env_state/nodes.json + pip_freeze.txt，install.py 重建時使用"""
    os.makedirs(STATE_DIR, exist_ok=True)
    scan = scan or scan_nodes()
    snap = snap if snap is not None else snapshot()
    nodes = []
    for kind in ("git", "excluded", "nogit", "disabled"):
        for m in scan[kind]:
            e = dict(name=m["name"], kind=kind, version=m["version"],
                     registry=m["registry"])
            if m["git"]:
                cwd = m["path"]
                e["url"] = run(["git", "remote", "get-url", "origin"], cwd).stdout.strip()
                e["commit"] = resolve("HEAD", cwd)
                e["branch"] = current_branch(cwd)
            nodes.append(e)
    data = dict(
        time=datetime.datetime.now().isoformat(timespec="seconds"),
        python=".".join(map(str, sys.version_info[:3])),
        torch={p: snap.get(norm(p)) for p in TORCH_PINS},
        core=core_info(),
        pin_core=PIN_CORE,
        nodes=nodes,
    )
    with open(NODES_MANIFEST, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(PIP_FREEZE, "w", encoding="utf-8") as f:
        f.write(run(PIP + ["freeze"]).stdout)


# ══════════════════════════ 摘要共用 ══════════════════════════

def print_events(rep):
    """已移除 / 已還原 / 安裝失敗"""
    section("已復原 / 已移除")
    if not rep.purged and not rep.restored:
        print("  （無）")
    for e in rep.purged:
        src = e["culprit"] or e["stage"]
        print(f"  {'✓' if e['ok'] else '✗'} 移除 {e['pkg']} {e['ver']}"
              f"　來源：{src}　原因：{e['reason']}")
    for e in rep.restored:
        src = e["culprit"] or e["stage"]
        print(f"  {'✓' if e['ok'] else '✗'} 還原 {e.get('what', 'PyTorch')}：{e['detail']}"
              f"　來源：{src}　（{e['why']}）")
    culprits = {}
    for e in rep.purged:
        if e["culprit"]:
            culprits.setdefault(e["culprit"], set()).add(e["pkg"])
    for c, pkgs in culprits.items():
        print(f"  提示：{c} 的 requirements 會把 {', '.join(sorted(pkgs))} 裝回來，"
              f"每次更新都會重複一次；可停用該節點或改它的 requirements.txt")

    section("安裝失敗")
    if not rep.pip_fail:
        print("  （無）")
    for stage, src, msg in rep.pip_fail:
        print(f"  ✗ [{stage}] {src}：{msg}")
    if rep.pip_fail and USE_CONSTRAINTS:
        print("  提示：若訊息提到 torch 版本衝突，是約束檔擋下了換 torch，屬預期行為")


def print_env(snap):
    section("環境鎖定")
    pv = sys.version_info
    print(f"  Python {pv.major}.{pv.minor}.{pv.micro}"
          f"（鎖定 {PY_MAJOR_MINOR[0]}.{PY_MAJOR_MINOR[1]}）")
    for p, want in TORCH_PINS.items():
        have = snap.get(norm(p))
        good = bool(have) and have.split("+")[0] == want and CUDA_TAG in have
        print(f"  {'✓' if good else '✗'} {p:12} {have or '未安裝':22}"
              f"鎖定 {want}+{CUDA_TAG}")
    for p, floor in FLOOR_PINS.items():
        have = snap.get(norm(p))
        vh, vf = _v(have), _v(floor)
        good = have is None or vh is None or vf is None or vh >= vf
        print(f"  {'✓' if good else '✗'} {p:12} {have or '未安裝':22}下限 {floor}")
    print(f"  約束檔：{'啟用（torch 不會被換版，numpy 不會被降版）' if USE_CONSTRAINTS else '停用'}")
    print(f"  黑名單：{', '.join(BLOCKLIST)}")


# ══════════════════════════ 相依性驗證 ══════════════════════════
# pip 是逐個節點安裝的，後面的會蓋掉前面的，最後狀態取決於資料夾順序。
# 這一段在「全部裝完之後」回頭檢查：每個節點的 requirements 到底有沒有被滿足。
# 只讀取、只比對，不安裝任何東西。

def parse_reqs(path):
    """讀 requirements.txt → [Requirement]，跳過選項行、URL 行、-r 遞迴"""
    if Requirement is None:
        return []
    out = []
    for raw in req_lines(path):                 # 自動處理 UTF-16 / BOM / CP950
        s = raw.split("#")[0].strip()
        if not s or s.startswith("-"):          # -r / --extra-index-url 等
            continue
        if "://" in s or s.startswith("."):      # 直接指向 URL 或本地路徑
            continue
        try:
            r = Requirement(s)
        except Exception:
            continue
        if r.marker is not None:                # python_version、sys_platform 等
            try:
                if not r.marker.evaluate():
                    continue
            except Exception:
                pass
        out.append(r)
    return out


def check_requirements(scan, snap):
    """回傳 (unmet, blocked, demands)
    unmet   : [(節點, 需求字串, 目前版本或 None)] —— 沒被滿足的
    blocked : [(節點, 套件)]                     —— 要求黑名單套件
    demands : {套件: [(節點, 需求字串)]}          —— 誰要求了這個套件
    """
    unmet, blocked, demands = [], [], {}
    bl = {norm(p) for p in BLOCKLIST}
    alias = {norm(k): v[0] for k, v in REQ_ALIASES.items()}
    subst = {norm(k): v for k, v in SHARED_DIR_REPAIR.items()}
    for m in enabled_nodes(scan):
        if not m["has_req"]:
            continue
        for r in parse_reqs(os.path.join(m["path"], "requirements.txt")):
            key = norm(r.name)
            demands.setdefault(key, []).append((m["name"], str(r)))

            # 這台機器用別的套件提供同一個東西 —— 需求其實是滿足的。
            #   REQ_ALIASES       triton       → triton-windows
            #   SHARED_DIR_REPAIR *-headless   → opencv-contrib-python
            # 不處理的話，摘要會把「已經可以用」的需求報成未滿足，
            # 久了就沒人看那份清單了。
            other = alias.get(key) or subst.get(key)
            if other and norm(other) in snap:
                continue

            if key in bl:
                blocked.append((m["name"], r.name))
                continue
            have = snap.get(key)
            if have is None:
                unmet.append((m["name"], str(r), None))
                continue
            if not r.specifier:
                continue
            try:
                # prereleases=True：不要因為是 rc 版就誤判成不滿足
                if not r.specifier.contains(have.split("+")[0], prereleases=True):
                    unmet.append((m["name"], str(r), have))
            except Exception:
                pass
    return unmet, blocked, demands


_PIPCHK_MISSING = re.compile(r"requires\s+([A-Za-z0-9._-]+),\s*which is not installed")


def pip_check_benign(line, snap):
    """
    這一行 pip check 抱怨的是「裝了替代品」而不是真的缺東西嗎？

    例：albumentations 宣告 opencv-python-headless，本機裝的是
        opencv-contrib-python。兩者提供同一個 cv2，import 完全正常，
        但 pip 只比對套件名，就會報成缺套件。

    只在「被抱怨的套件在 SHARED_DIR_REPAIR 裡，而且它的替代品確實
    已安裝」時才算無害 —— 其他情況一律照實報。
    """
    m = _PIPCHK_MISSING.search(line)
    if not m:
        return False
    sub = {norm(k): v for k, v in SHARED_DIR_REPAIR.items()}.get(norm(m.group(1)))
    return bool(sub) and norm(sub) in snap


def print_dependency_check(scan, snap):
    """摘要最後一段：真正回答「現在的環境符合各節點的相依宣告嗎」"""
    section("相依性驗證（全部裝完後的實際狀態）")
    if Requirement is None:
        print("  ✗ 缺少 packaging，無法驗證（pip install packaging）")
        return
    unmet, blocked, demands = check_requirements(scan, snap)

    r = run(PIP + ["check"])
    if r.returncode == 0:
        print("  ✓ pip check：已安裝套件之間沒有相依衝突")
    else:
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        real, benign = [], []
        for l in lines:
            (benign if pip_check_benign(l, snap) else real).append(l)
        if real:
            print(f"  ✗ pip check 回報 {len(real)} 個問題：")
            for l in real[:10]:
                print(f"      {l}")
            if len(real) > 10:
                print(f"      …共 {len(real)} 行，完整內容見紀錄檔")
                for l in real[10:]:
                    log_only(f"      {l}")
        else:
            print("  ✓ pip check：沒有實際衝突")
        if benign:
            # 不是真的缺套件 —— 要的是 cv2，而 cv2 由 contrib 提供。
            # pip 只比對套件名，看不出這件事，所以降級成說明。
            print(f"  ℹ {len(benign)} 項 pip check 訊息已確認無害"
                  "（所需模組由同目錄的替代套件提供）：")
            for l in benign:
                print(f"      {l}")

    if blocked:
        print(f"  ⚠ {len(blocked)} 個節點要求黑名單套件，該節點的相關功能不會有作用：")
        for name, pkg in blocked:
            print(f"      - {name} 要求 {pkg}")

    if not unmet:
        print("  ✓ 各節點的 requirements 都被滿足")
    else:
        print(f"  ✗ {len(unmet)} 項節點需求沒被滿足"
              "（被別的節點蓋掉、或被約束檔 / 黑名單擋下）：")
        for name, req, have in unmet:
            print(f"      - {name} 要求 {req}，目前 {have or '未安裝'}")
        # 同一個套件被多個節點要求、且其中有人不滿足 → 點名所有競爭者
        clash = {}
        for name, req, _ in unmet:
            key = norm(Requirement(req).name) if Requirement else req
            if len(demands.get(key, [])) > 1:
                clash[key] = demands[key]
        for pkg, who in clash.items():
            print(f"  ⚠ {pkg} 同時被 {len(who)} 個節點要求，彼此無法同時滿足：")
            for name, req in who:
                print(f"      - {name}：{req}")
            print("    → 這不是腳本抖動，是節點之間的硬衝突，只能取捨："
                  "停用其中一個、或改它的 requirements.txt")


# ══════════════════════════ 直接執行：唯讀檢查 ══════════════════════════
# 這個檔案平常是被 update.py / install.py import 的設定與工具庫。
# 直接執行時只「看」不「動」——不安裝、不移除、不 git、不改任何檔案。

def _status():
    header("環境現況檢查（唯讀，不會安裝或移除任何東西）")
    try:
        snap = snapshot()
    except RuntimeError as ex:
        print(f"[ERROR] {ex}")
        return 1
    print_env(snap)

    bad = [p for p in BLOCKLIST if norm(p) in snap]
    section("黑名單套件")
    if bad:
        for p in bad:
            print(f"  ✗ {p} {snap[norm(p)]} — {BLOCKLIST[p]}")
        print("  → 執行 update.py 會自動移除")
    else:
        print("  ✓ 都沒裝")

    section("ComfyUI 本體")
    c = core_info()
    print(f"  v{c['version']}　{c['short']}　{c['date']}　{c['subject']}")
    print(f"  分支：{c['branch']}　" +
          (f"鎖定於 {PIN_CORE}" if PIN_CORE else "未鎖版"))
    d = dirty_files()
    if d:
        print(f"  ⚠ {len(d)} 個檔案有本地改動，會擋住 fast-forward 更新：")
        for st, fn in d[:10]:
            print(f"      {st}  {fn}　({dirty_label(st)})")
        if len(d) > 10:
            print(f"      …共 {len(d)} 個")
    # fetch 只更新遠端參照，不會動到你的檔案；不 fetch 的話落後數字會是舊的
    if not PIN_CORE and run(["git", "fetch", "origin"]).returncode == 0:
        up = upstream_ref()
        behind, ahead = count(f"HEAD..{up}"), count(f"{up}..HEAD")
        msg = "已是最新" if behind == 0 else f"落後 {behind} 個提交（跑 update.bat 更新）"
        if ahead:
            msg += f"，本地領先 {ahead} 個提交（自訂 patch，更新需 rebase 模式）"
        print(f"  上游：{msg}")

    scan = scan_nodes()
    bad_nodes = [m for m in enabled_nodes(scan) + scan["disabled"]
                 if not m.get("valid", True)]
    if bad_nodes:
        section("無效的節點資料夾")
        for m in bad_nodes:
            print(f"  ✗ {m['name']} — 沒有 __init__.py，ComfyUI 啟動時會報 "
                  "FileNotFoundError，建議刪除")
    section("本體安全修補")
    _st, _p = patch_status()
    print("  " + {
        "patched":    "✓ StringFormat 格式字串限制　已套用",
        "vulnerable": "✗ StringFormat 未修補 —— 跑一次 update.py 會自動補上",
        "unknown":    "⚠ 認不出要修補的那一行，上游可能已自行修正",
        "missing":    "⚠ 找不到 comfy_extras/nodes_string.py",
    }[_st])

    section("自訂節點")
    print(f"  可更新（有 git）{len(scan['git'])}　"
          f"無 git {len(scan['nogit'])}　"
          f"排除 {len(scan['excluded'])}　停用 {len(scan['disabled'])}")
    for kind, label in (("git", "有 git"), ("nogit", "無 git（Manager 內更新）"),
                        ("excluded", "排除（不自動更新）"), ("disabled", "停用")):
        if scan[kind]:
            print(f"  【{label}】")
            for m in scan[kind]:
                pin = f"　📌 {NODE_PINS[m['name']]}" if m["name"] in NODE_PINS else ""
                print(f"      - {describe(m)}{pin}")

    section("自訂 wheel")
    print_wheels(wheel_status(snap))
    for name, ok, msg in import_checks():
        print(f"  {'✓' if ok else '✗'} 匯入 {name:14} {msg}")

    section("環境快照")
    if os.path.exists(NODES_MANIFEST):
        try:
            with open(NODES_MANIFEST, encoding="utf-8") as f:
                print(f"  最後紀錄：{json.load(f).get('time')}（install.py 依此重建）")
        except Exception as ex:
            print(f"  ✗ nodes.json 讀取失敗：{ex}")
    else:
        print("  （尚無，跑一次 update.py 就會產生）")

    print()
    print("=" * 54)
    print("  這支只做檢查。要更新跑 update.bat，要重建跑 install.bat")
    print("=" * 54)
    return 0


if __name__ == "__main__":
    sys.exit(_status())
