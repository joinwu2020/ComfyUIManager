# -*- coding: utf-8 -*-
"""
comfy_audit.py — ComfyUI 入侵檢查（唯讀：不刪除、不修改、不執行任何被檢查的程式碼）

針對「ComfyUI 被植入挖礦程式」這類事件，檢查：
  1. custom_nodes：只跑 prestartup、不提供節點的植入器樣態；來源不明的節點；可疑程式碼
  2. venv\\Scripts：冒充 python 的執行檔（pythonm.exe 這類）、體積異常、含礦池字串
  3. site-packages：會在 Python 啟動時自動執行的 .pth / sitecustomize；近期新裝的套件
  4. 網路暴露：ComfyUI 是否聽在 0.0.0.0、Manager 的 network_mode / security_level
  5. 自動啟動：登錄檔 Run 機碼、啟動資料夾、排程工作
  6. 執行中程序與對外連線：venv 底下的非 python 程序、礦池常用連接埠
  7. gpu.log 等挖礦紀錄檔：找出開始挖礦的時間
  8. Windows Defender 偵測紀錄
最後輸出時間軸，方便對照「什麼時候、從哪裡進來」。

用法（用任何 Python 3.8+ 執行皆可，只用標準函式庫）：
    python comfy_audit.py
    python comfy_audit.py --root "C:\\Users\\User\\Documents\\comfy\\ComfyUI" --days 30

輸出：主控台摘要 + 同資料夾下的 comfy_audit_<時間>.txt / .json
"""

import argparse
import base64
import configparser
import datetime as dt
import hashlib
import io
import json
import os
import re
import subprocess
# 無 console 模式下子程序會自己開黑窗，一律帶這個旗標
NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

import sys

IS_WIN = os.name == 'nt'

# ---------------------------------------------------------------- 參數與常數
DEFAULT_ROOT = r"C:\Users\User\Documents\comfy\ComfyUI"
COMFY_PORT = 8188

# 走訪時略過的大型資料夾（模型、輸出、暫存）
SKIP_DIRS = {'models', 'output', 'input', 'temp', '.git', '__pycache__',
             'node_modules', '.cache', 'update_logs', 'env_state'}

# 強指標：出現就幾乎確定是挖礦／本次事件
IOC_STRONG = [
    'pythonm.exe', 'kryptex', 'stratum+tcp', 'stratum+ssl', 'stratum2+',
    'fleet-id_', 'krxXGNGM24', 'xmrig', 'lolminer', 'srbminer', 'nanominer',
    'teamredminer', 'moneroocean', 'supportxmr', 'unmineable', 'nicehash',
    '--donate-level', '--api-port 0', 'peakminer',
    # comfyui-networksuite 後門（2026-10 事件）
    '102.220.163.167', '78.17.213.213', '121.127.34.173', '47.76.33.231',
    'pastebin.com/raw/SiCPwKB6', 'telegra.ph/keywords-09-30',
    'telegra.ph/my-poem-09-25', 'telegra.ph/my-thoughts-09-25',
    'cmd_execute_detached_shell', 'cmd_push_file', "CLUSTER='cluster5'",
]
# 這些字串只是「名稱」，出現在 ComfyUI-Manager 的安全掃描清單裡屬正常
MINER_NAME_ONLY = {'xmrig', 'lolminer', 'srbminer', 'nanominer', 'teamredminer',
                   'nicehash', 'moneroocean', 'supportxmr', 'unmineable'}
SCANNER_PATH_RE = re.compile(r'comfyui[-_]manager', re.I)
BENIGN_URL_RE = re.compile(r'https?://(ns\.adobe\.com|purl\.org|www\.w3\.org|'
                           r'schemas\.|xmlns\.)', re.I)

# 後門 C2 位址（防火牆封鎖用）
C2_IPS = ['102.220.163.167', '78.17.213.213', '121.127.34.173', '47.76.33.231']
# 已知的惡意節點資料夾名稱（含此後門程式碼裡「清除競爭對手」清單上的名字）
KNOWN_BAD_NODES = {'comfyui-networksuite', 'ruiqunodes', 'comfyui-ruiqunodes',
                   'comfyui_jamworks_client', 'srl-nodes', 'champdev-comfyui-nodes'}
WATCH_NODES = {'comfyui-notebook'}
IOC_RE = re.compile('|'.join(re.escape(s) for s in IOC_STRONG), re.I)

# custom_nodes 程式碼的可疑樣態：(等級, 說明, 正則)
CODE_PATTERNS = [
    ('HIGH', '解碼後直接執行（混淆的 payload）',
     r'(exec|eval)\s*\(\s*(__import__\(\s*[\'"](base64|zlib|marshal|codecs)|'
     r'base64\.|zlib\.|marshal\.|codecs\.decode|bytes\.fromhex)'),
    ('MED', '隱藏視窗啟動子程序（呼叫 ffmpeg 等工具的正常節點也會這樣做）',
     r'CREATE_NO_WINDOW|DETACHED_PROCESS|SW_HIDE|'
     r'creationflags[\'"\]]*\s*[=:]\s*(0x0*8000000|0x0*00008|134217728)'),
    ('HIGH', '把程式碼從標準輸入餵給另一個 python（不落地執行）',
     r'Popen\(\s*\[\s*sys\.executable\s*,\s*[\'"]-[\'"]'),
    ('HIGH', '遠端指令通道（WebSocket 連線並執行 shell）',
     r'ws_connect[\s\S]{0,4000}create_subprocess_shell|'
     r'create_subprocess_shell[\s\S]{0,4000}ws_connect'),
    ('HIGH', '遠端上傳／下載檔案指令', r'def cmd_(push_file|pull_file|execute_shell)'),
    ('HIGH', '下載 .exe',
     r'(urlretrieve|urlopen|requests\.get|httpx\.get|download)[^\n]{0,300}\.exe'),
    ('HIGH', 'PowerShell 編碼／隱藏指令',
     r'powershell(\.exe)?[^\n]{0,120}(-enc\b|-encodedcommand|-w\s+hidden|'
     r'-windowstyle\s+hidden)'),
    ('HIGH', '修改 Defender 排除清單',
     r'Add-MpPreference|ExclusionPath|Set-MpPreference'),
    ('MED', '建立排程工作／自動啟動',
     r'schtasks|CurrentVersion\\\\Run|\\\\Start Menu\\\\Programs\\\\Startup|'
     r'Register-ScheduledTask'),
    ('MED', 'marshal 反序列化程式碼', r'marshal\.loads'),
    ('MED', '寫入或啟動 venv\\\\Scripts 下的執行檔',
     r'Scripts[\\/]+[A-Za-z0-9_\-]+\.exe'),
    ('MED', '隱藏檔案屬性', r'attrib\s+\+h|FILE_ATTRIBUTE_HIDDEN'),
    ('LOW', 'os.startfile 直接啟動檔案', r'os\.startfile'),
]
CODE_RES = [(lv, msg, re.compile(p, re.I)) for lv, msg, p in CODE_PATTERNS]

STRATUM_PORTS = {3333, 4444, 5555, 7048, 8048, 14444, 14433, 9999, 1800, 2222}

TEXT_EXT = {'.py', '.pyw', '.pth', '.bat', '.cmd', '.ps1', '.vbs', '.js',
            '.json', '.txt', '.cfg', '.ini', '.toml', '.yaml', '.yml', '.sh'}
EXEC_EXT = {'.exe', '.dll', '.scr', '.com', '.bat', '.cmd', '.ps1', '.vbs', '.pyd'}

LEVEL_ORDER = {'HIGH': 0, 'MED': 1, 'LOW': 2, 'INFO': 3}
LEVEL_MARK = {'HIGH': '✗ 高', 'MED': '⚠ 中', 'LOW': '· 低', 'INFO': '  資訊'}


# ---------------------------------------------------------------- 工具
class Report:
    def __init__(self):
        self.findings = []
        self.timeline = []      # (datetime, 說明)

    def add(self, level, category, title, detail='', path=''):
        self.findings.append({'level': level, 'category': category,
                              'title': title, 'detail': detail, 'path': path})

    def when(self, t, what):
        if t:
            self.timeline.append((t, what))


def log(msg):
    print(msg, flush=True)


def fmt_t(ts):
    try:
        return dt.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return '?'


def to_dt(ts):
    try:
        return dt.datetime.fromtimestamp(ts)
    except Exception:
        return None


def read_text(path, limit=2 * 1024 * 1024):
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except Exception:
        return None


def bytes_contain_ioc(path, limit=96 * 1024 * 1024):
    """在二進位檔裡找礦池字串（礦工常把設定寫死在執行檔裡）。"""
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, 'rb') as f:
            data = f.read()
        hits = set()
        for s in IOC_STRONG:
            b = s.encode()
            if b.lower() in data.lower():
                hits.add(s)
        return sorted(hits)
    except Exception:
        return None


def sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ''


def walk(root, skip=SKIP_DIRS):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d.lower() not in skip]
        for fn in fns:
            yield os.path.join(dp, fn)


def powershell_json(cmd, timeout=180):
    """執行 PowerShell 並解析 ConvertTo-Json 的輸出；非 Windows 或失敗回 None。"""
    if not IS_WIN:
        return None
    full = ('[Console]::OutputEncoding=[Text.Encoding]::UTF8; '
            '$ErrorActionPreference="SilentlyContinue"; ' + cmd)
    try:
        p = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive',
             '-ExecutionPolicy', 'Bypass', '-Command', full],
            creationflags=NOWIN,
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=timeout)
        out = (p.stdout or '').strip()
        if not out:
            return []
        data = json.loads(out)
        return data if isinstance(data, list) else [data]
    except Exception as e:
        return {'error': str(e)}


def under(path, root):
    try:
        return os.path.normcase(os.path.abspath(path)).startswith(
            os.path.normcase(os.path.abspath(root)))
    except Exception:
        return False


# ---------------------------------------------------------------- 檢查項目
def check_custom_nodes(rep, root, days):
    cn = os.path.join(root, 'custom_nodes')
    if not os.path.isdir(cn):
        rep.add('INFO', '節點', '找不到 custom_nodes', path=cn)
        return
    log('  檢查 custom_nodes …')
    recent = dt.datetime.now() - dt.timedelta(days=days)
    for name in sorted(os.listdir(cn)):
        d = os.path.join(cn, name)
        if not os.path.isdir(d) or name.startswith('__'):
            continue
        prestart = os.path.isfile(os.path.join(d, 'prestartup_script.py'))
        init_txt = read_text(os.path.join(d, '__init__.py')) or ''
        has_map = ('NODE_CLASS_MAPPINGS' in init_txt or
                   'comfy_entrypoint' in init_txt)
        # 有些節點把 mappings 放在別的檔案再 import，所以掃整個資料夾頂層
        if not has_map:
            for fn in os.listdir(d):
                if fn.endswith('.py'):
                    t = read_text(os.path.join(d, fn)) or ''
                    if 'NODE_CLASS_MAPPINGS' in t or 'comfy_entrypoint' in t:
                        has_map = True
                        break

        # 來源
        src = ''
        gcfg = os.path.join(d, '.git', 'config')
        if os.path.isfile(gcfg):
            m = re.search(r'url\s*=\s*(\S+)', read_text(gcfg) or '')
            src = f'git: {m.group(1)}' if m else 'git（無 remote）'
        elif os.path.isfile(os.path.join(d, '.tracking')):
            src = 'Registry（.tracking）'
        elif os.path.isfile(os.path.join(d, 'pyproject.toml')):
            src = 'pyproject（可能是 Registry 或手動）'
        else:
            src = '不明（無 git、無 .tracking、無 pyproject）'

        try:
            ctime = os.path.getctime(d)
        except Exception:
            ctime = None
        newest = 0
        for fp in walk(d):
            try:
                newest = max(newest, os.path.getmtime(fp))
            except Exception:
                pass

        info = (f'來源：{src}；資料夾建立 {fmt_t(ctime)}；'
                f'最近改動 {fmt_t(newest)}；prestartup={"有" if prestart else "無"}；'
                f'提供節點={"是" if has_map else "否"}')

        if name.lower() in KNOWN_BAD_NODES:
            rep.add('HIGH', '節點', f'{name}：已知的惡意節點名稱',
                    '此名稱出現在 comfyui-networksuite 後門事件中。' + info, d)
        if name.lower() in WATCH_NODES:
            rep.add('MED', '節點', f'{name}：出現在後門程式碼的「清除競爭對手」清單上',
                    '後門會刪除它認定的其他惡意節點。這個節點可能是另一批入侵留下的，'
                    '也可能只是被誤列；請檢查其內容與來源。' + info, d)
        dropper = prestart and not has_map
        if dropper:
            rep.add('HIGH', '節點', f'{name}：只在啟動前執行程式、卻不提供任何節點',
                    '這是植入器的典型樣態（prestartup 先跑，ComfyUI 再以 '
                    'IMPORT FAILED 略過它，使用者不易察覺）。' + info, d)
            rep.when(to_dt(ctime), f'節點資料夾建立：{name}（高度可疑）')
        elif src.startswith('不明'):
            rep.add('MED', '節點', f'{name}：來源不明', info, d)
        elif prestart:
            rep.add('INFO', '節點', f'{name}：有 prestartup 腳本', info, d)

        if not dropper and ctime and to_dt(ctime) and to_dt(ctime) > recent:
            rep.when(to_dt(ctime), f'節點資料夾建立：{name}')

        # 程式碼樣態
        for fp in walk(d):
            ext = os.path.splitext(fp)[1].lower()
            if ext in EXEC_EXT and ext not in {'.bat', '.cmd', '.ps1', '.vbs', '.pyd'}:
                rep.add('MED', '節點', f'{name}：節點資料夾內含執行檔',
                        f'{os.path.basename(fp)}；{os.path.getsize(fp):,} bytes；'
                        f'修改 {fmt_t(os.path.getmtime(fp))}；SHA256 {sha256(fp)}', fp)
            if ext not in TEXT_EXT:
                continue
            txt = read_text(fp)
            if not txt:
                continue
            layers = [('', txt)] + [('（藏在 base64 裡）', b) for b in decoded_blobs(txt)]
            for tag, body in layers:
                m = IOC_RE.search(body)
                if m and m.group(0).lower() in MINER_NAME_ONLY and SCANNER_PATH_RE.search(fp):
                    m = None   # Manager 自己的惡意軟體名單，不是感染
                if m:
                    rep.add('HIGH', '節點',
                            f'{name}：含挖礦／後門特徵字串「{m.group(0)}」{tag}',
                            _ctx(body, m.start()), fp)
                for lv, msg, rx in CODE_RES:
                    mm = rx.search(body)
                    if mm:
                        rep.add(lv, '節點', f'{name}：{msg}{tag}',
                                _ctx(body, mm.start()), fp)
                if tag:
                    ips = sorted(set(re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', body)))
                    ips = [i for i in ips if not i.startswith(('0.', '127.', '1.0.'))]
                    urls = sorted(u for u in set(re.findall(r'https?://[^\s\'"]+', body))
                                  if not BENIGN_URL_RE.match(u))
                    if ips:
                        rep.add('HIGH', '節點', f'{name}：base64 內藏 IP 位址',
                                '；'.join(ips + urls)[:500], fp)
                    elif urls:
                        rep.add('LOW', '節點', f'{name}：base64 內藏網址',
                                '；'.join(urls)[:500], fp)
            # 超長單行（常見於混淆 payload）
            if ext in {'.py', '.pyw'}:
                for i, line in enumerate(txt.splitlines(), 1):
                    if len(line) > 3000 and re.fullmatch(r'[\sA-Za-z0-9+/=\'"_().,\[\]]+',
                                                         line[:3000] + line[-200:]):
                        rep.add('MED', '節點', f'{name}：疑似混淆的超長單行（第 {i} 行，'
                                f'{len(line):,} 字元）', line[:160] + '…', fp)
                        break


_B64_RE = re.compile(r'[A-Za-z0-9+/]{200,}={0,2}')


def decoded_blobs(txt):
    """找出長 base64 字串並解碼成文字（只解碼，絕不執行）。"""
    out = []
    for m in _B64_RE.finditer(txt):
        try:
            raw = base64.b64decode(m.group(0) + '=' * (-len(m.group(0)) % 4),
                                   validate=False)
            t = raw.decode('utf-8', errors='ignore')
        except Exception:
            continue
        printable = sum(c.isprintable() or c in '\n\r\t' for c in t)
        if t and printable / max(1, len(t)) > 0.9:
            out.append(t)
    return out


def _ctx(txt, pos, span=140):
    a = max(0, pos - span // 3)
    return txt[a:a + span].replace('\r', ' ').replace('\n', ' ⏎ ')


def check_venv_scripts(rep, root, days):
    sd = os.path.join(root, 'venv', 'Scripts')
    if not os.path.isdir(sd):
        sd = os.path.join(root, 'venv', 'bin')
    if not os.path.isdir(sd):
        rep.add('INFO', 'venv', '找不到 venv\\Scripts', path=sd)
        return
    log('  檢查 venv\\Scripts …')
    recent = dt.datetime.now() - dt.timedelta(days=days)
    for fn in sorted(os.listdir(sd)):
        fp = os.path.join(sd, fn)
        if not os.path.isfile(fp) or not fn.lower().endswith('.exe'):
            continue
        low = fn.lower()
        size = os.path.getsize(fp)
        ct = os.path.getctime(fp)
        detail = (f'{size:,} bytes；建立 {fmt_t(ct)}；修改 {fmt_t(os.path.getmtime(fp))}')

        if re.fullmatch(r'python.*\.exe', low) and low not in (
                'python.exe', 'pythonw.exe', 'python3.exe', 'python3w.exe'):
            rep.add('HIGH', 'venv', f'{fn}：冒充 python 的執行檔',
                    detail + f'；SHA256 {sha256(fp)}', fp)
            rep.when(to_dt(ct), f'冒充執行檔出現：{fn}')
            continue
        hits = bytes_contain_ioc(fp)
        if hits:
            rep.add('HIGH', 'venv', f'{fn}：執行檔內含礦池字串 {hits}',
                    detail + f'；SHA256 {sha256(fp)}', fp)
            rep.when(to_dt(ct), f'含礦池字串的執行檔：{fn}')
            continue
        # pip / uv 產生的 console-script 啟動器通常 < 1MB
        if size > 5 * 1024 * 1024 and low not in ('python.exe', 'pythonw.exe'):
            rep.add('MED', 'venv', f'{fn}：體積異常（console-script 啟動器通常 < 1MB）',
                    detail + f'；SHA256 {sha256(fp)}', fp)
        if to_dt(ct) and to_dt(ct) > recent and low not in ('python.exe', 'pythonw.exe'):
            rep.add('INFO', 'venv', f'{fn}：近 {days} 天內建立', detail, fp)


def find_site_packages(root):
    cands = [os.path.join(root, 'venv', 'Lib', 'site-packages')]
    cands += [os.path.join(root, 'venv', 'lib', x, 'site-packages')
              for x in (os.listdir(os.path.join(root, 'venv', 'lib'))
                        if os.path.isdir(os.path.join(root, 'venv', 'lib')) else [])]
    return [c for c in cands if os.path.isdir(c)]


def check_site_packages(rep, root, days, deep):
    sps = find_site_packages(root)
    if not sps:
        rep.add('INFO', 'site-packages', '找不到 venv 的 site-packages')
        return
    recent = dt.datetime.now() - dt.timedelta(days=days)
    for sp in sps:
        log(f'  檢查 site-packages：{sp} …')
        # .pth：Python 每次啟動都會執行其中以 import 開頭的行
        for fn in sorted(os.listdir(sp)):
            if not fn.endswith('.pth'):
                continue
            fp = os.path.join(sp, fn)
            txt = read_text(fp) or ''
            code = [l for l in txt.splitlines() if l.strip().startswith('import')]
            if not code:
                continue
            bad = any(re.search(r'exec|eval|base64|subprocess|urllib|socket|Popen|'
                                r'b64decode|marshal|compile\(', l) for l in code)
            m = IOC_RE.search(txt)
            lv = 'HIGH' if (bad or m) else 'INFO'
            rep.add(lv, 'site-packages',
                    f'{fn}：Python 啟動時會自動執行的 .pth'
                    + ('（含可疑呼叫）' if lv == 'HIGH' else ''),
                    ' ⏎ '.join(code)[:400], fp)
        for fn in ('sitecustomize.py', 'usercustomize.py'):
            fp = os.path.join(sp, fn)
            if os.path.isfile(fp):
                rep.add('MED', 'site-packages', f'{fn} 存在（每次啟動自動執行，少有正當用途）',
                        (read_text(fp) or '')[:400], fp)
        # 近期新裝的套件（看 dist-info 建立時間）
        fresh = []
        for fn in os.listdir(sp):
            if fn.endswith(('.dist-info', '.egg-info')):
                fp = os.path.join(sp, fn)
                ct = to_dt(os.path.getctime(fp))
                if ct and ct > recent:
                    fresh.append((ct, fn))
        for ct, fn in sorted(fresh):
            rep.when(ct, f'套件安裝：{fn}')
        if fresh:
            rep.add('INFO', 'site-packages', f'近 {days} 天新裝／重裝 {len(fresh)} 個套件',
                    '、'.join(fn for _, fn in sorted(fresh)[-40:]), sp)
        # 全面掃特徵字串（較慢，可用 --quick 略過）
        if deep:
            log('    掃描 site-packages 特徵字串（約 1～3 分鐘）…')
            n = 0
            for fp in walk(sp):
                ext = os.path.splitext(fp)[1].lower()
                if ext not in {'.py', '.pth', '.bat', '.cmd', '.ps1', '.js'}:
                    continue
                n += 1
                txt = read_text(fp, limit=1024 * 1024)
                if not txt:
                    continue
                m = IOC_RE.search(txt)
                if m and m.group(0).lower() in MINER_NAME_ONLY and SCANNER_PATH_RE.search(fp):
                    m = None
                if m:
                    rep.add('HIGH', 'site-packages', f'含特徵字串「{m.group(0)}」',
                            _ctx(txt, m.start()), fp)
            log(f'    已掃 {n:,} 個檔案')


def check_exposure(rep, root):
    log('  檢查網路暴露 …')
    # 1. 最近幾份 comfyui.log 的監聽位址
    udir = os.path.join(root, 'user')
    listen_all = False
    if os.path.isdir(udir):
        for fn in sorted(os.listdir(udir)):
            if fn.startswith('comfyui') and fn.endswith('.log'):
                txt = read_text(os.path.join(udir, fn), limit=64 * 1024 * 1024) or ''
                for m in re.finditer(r'To see the GUI go to:\s*(http://\S+)', txt):
                    url = m.group(1)
                    if '0.0.0.0' in url or '[::]' in url:
                        listen_all = True
                        rep.add('HIGH', '網路', f'ComfyUI 聽在所有網卡：{url}（{fn}）',
                                '同網段（若有轉送埠則包含網際網路）的任何人都能送工作流、'
                                '呼叫 Manager。移除啟動參數 --listen，或改成 --listen 127.0.0.1。',
                                os.path.join(udir, fn))
                        break
    # 2. Manager 設定
    for cfg in (os.path.join(udir, '__manager', 'config.ini'),
                os.path.join(udir, 'default', 'ComfyUI-Manager', 'config.ini'),
                os.path.join(root, 'custom_nodes', 'ComfyUI-Manager', 'config.ini')):
        if not os.path.isfile(cfg):
            continue
        cp = configparser.ConfigParser()
        try:
            cp.read(cfg, encoding='utf-8')
        except Exception:
            continue
        for sec in cp.sections():
            nm = cp.get(sec, 'network_mode', fallback='')
            sl = cp.get(sec, 'security_level', fallback='')
            if nm or sl:
                risky = (nm.lower() in ('personal_cloud',) or
                         sl.lower() in ('weak', 'normal-'))
                lv = 'HIGH' if (risky and listen_all) else ('MED' if risky else 'INFO')
                rep.add(lv, '網路', f'ComfyUI-Manager：network_mode={nm or "?"}，'
                        f'security_level={sl or "?"}',
                        '對外開放時，這組設定會放寬遠端安裝節點的限制。'
                        '建議 network_mode=private、security_level=normal 以上。'
                        if risky else '', cfg)
    # 3. 實際監聽狀態
    data = powershell_json(
        'Get-NetTCPConnection -State Listen | '
        'Select-Object LocalAddress,LocalPort,OwningProcess | ConvertTo-Json -Compress')
    if isinstance(data, list):
        for c in data:
            if c.get('LocalPort') == COMFY_PORT:
                addr = c.get('LocalAddress', '')
                lv = 'HIGH' if addr in ('0.0.0.0', '::') else 'INFO'
                rep.add(lv, '網路', f'目前 {COMFY_PORT} 埠監聽於 {addr}'
                        f'（PID {c.get("OwningProcess")}）')
    # 4. UPnP / 路由器轉送無法從本機確認，提醒
    if listen_all:
        rep.add('INFO', '網路', '請到路由器確認沒有把 8188 轉送到外網，並關閉 UPnP')


# ---------------------------------------------------------------- 開機自動執行
# 判斷「啟動指令」可疑程度用的樣態
_SCRIPT_HOST = re.compile(r'\b(powershell|pwsh|wscript|cscript|mshta|rundll32|regsvr32|'
                          r'python[w]?|pythonm|cmd(\.exe)?\s+/c|bitsadmin|certutil)\b', re.I)
_PS_HIDDEN = re.compile(r'(-enc\b|-encodedcommand|-e\s+[A-Za-z0-9+/=]{20,}|'
                        r'-w(indowstyle)?\s+hidden|frombase64string|iex\b|invoke-expression|'
                        r'downloadstring|downloadfile|invoke-webrequest|\biwr\b)', re.I)
_USER_WRITABLE = re.compile(r'\\(users|appdata|temp|tmp|programdata|public|downloads|'
                            r'documents|desktop)\\|\\comfy', re.I)
_FAKE_COMFY = re.compile(r'python[w]?(\.exe)?"?\s+-\s+-s\s+\S*main\.py', re.I)
_SYS_DIRS = re.compile(r'^"?[a-z]:\\(windows|program files|program files \(x86\))\\', re.I)
# 已知的正常系統元件路徑：Defender 本來就裝在 ProgramData、驅動服務在 DriverStore、
# OneDrive 本來就裝在使用者的 AppData（且會自動更新，所以檔案時間常是最近）
_TRUSTED = re.compile(r'(^"?[a-z]:\\programdata\\microsoft\\windows defender\\platform\\[\d.\-]+\\'
                      r'(mpcmdrun|msmpeng|mpdefendercoreservice|nissrv)\.exe'
                      r'|^"?[a-z]:\\windows\\system32\\driverstore\\filerepository\\'
                      r'|\\appdata\\local\\microsoft\\onedrive\\([\d.]+\\)?'
                      r'(onedrive|onedrivelauncher|onedrivestandaloneupdater|onedrive\.sync\.service|filecoauth)\.exe)',
                      re.I)


def _exe_from_cmd(cmd):
    """從啟動指令抽出執行檔路徑（帶環境變數展開）。"""
    c = os.path.expandvars((cmd or '').strip())
    if not c:
        return ''
    if c.startswith('"'):
        e = c.find('"', 1)
        return c[1:e] if e > 0 else c[1:]
    m = re.match(r'(.+?\.(exe|bat|cmd|ps1|vbs|js|py|pyw|com|scr|dll))\b', c, re.I)
    return m.group(1) if m else c.split(' ')[0]


def _judge(cmd, days, recent_note=True):
    """回傳 (等級, 理由列表, 執行檔路徑)。"""
    why, lv = [], 'INFO'
    c = cmd or ''
    if IOC_RE.search(c):
        return 'HIGH', [f'含挖礦／後門特徵「{IOC_RE.search(c).group(0)}」'], _exe_from_cmd(c)
    if _FAKE_COMFY.search(c):
        return 'HIGH', ['偽裝成 ComfyUI 的後門啟動方式（python - -s main.py，程式碼從標準輸入讀入）'], \
            _exe_from_cmd(c)
    if _PS_HIDDEN.search(c):
        lv, why = 'HIGH', why + ['隱藏或編碼的腳本指令']
    elif _SCRIPT_HOST.search(c):
        lv = 'MED'
        why.append(f'透過腳本宿主啟動（{_SCRIPT_HOST.search(c).group(0)}）')
    exe = _exe_from_cmd(c)
    if exe and _TRUSTED.search(exe) and lv == 'INFO':
        return 'INFO', ['已知的系統／微軟元件路徑'], exe
    if exe and _USER_WRITABLE.search(exe) and not _SYS_DIRS.search(exe):
        why.append('執行檔位在使用者可寫入的資料夾')
        if lv == 'INFO':
            lv = 'LOW'
    if exe and os.path.isfile(exe):
        ct = to_dt(os.path.getctime(exe))
        if ct and ct > dt.datetime.now() - dt.timedelta(days=days):
            why.append(f'執行檔近 {days} 天內建立（{ct:%Y-%m-%d %H:%M}）')
            if lv in ('INFO', 'LOW'):
                lv = 'MED'
            # 依附在可疑檔案上的檔案內容也掃一次
            txt = read_text(exe) if exe.lower().endswith(
                ('.bat', '.cmd', '.ps1', '.vbs', '.js', '.py', '.pyw')) else None
            if txt and IOC_RE.search(txt):
                lv, why = 'HIGH', why + ['指向的腳本內含挖礦／後門特徵']
    elif exe and re.match(r'^[a-z]:\\', exe, re.I) and not os.path.exists(exe):
        why.append('指向的檔案不存在')
    return lv, why, exe


def _reg_values(hive, path):
    import winreg
    try:
        k = winreg.OpenKey(hive, path)
    except OSError:
        return []
    out, i = [], 0
    while True:
        try:
            name, val, _ = winreg.EnumValue(k, i)
        except OSError:
            break
        out.append((name, val))
        i += 1
    return out


def _reg_subkeys(hive, path):
    import winreg
    try:
        k = winreg.OpenKey(hive, path)
    except OSError:
        return []
    out, i = [], 0
    while True:
        try:
            out.append(winreg.EnumKey(k, i))
        except OSError:
            break
        i += 1
    return out


def check_persistence(rep, root, days=14):
    """開機／登入自動執行的所有常見位置。"""
    if not IS_WIN:
        rep.add('INFO', '開機自動執行', '非 Windows，略過開機自動執行檢查')
        return
    import winreg
    HKCU, HKLM = winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE
    hn = {HKCU: 'HKCU', HKLM: 'HKLM'}
    cat = '開機自動執行'

    def emit(where, cmd, extra='', path=''):
        lv, why, exe = _judge(cmd, days)
        rep.add(lv, cat, where, (('；'.join(why) + '｜') if why else '') + str(cmd)
                + (f'｜{extra}' if extra else ''), path or exe)
        if lv in ('HIGH', 'MED') and exe and os.path.isfile(exe):
            rep.when(to_dt(os.path.getctime(exe)), f'自動執行項目的檔案建立：{exe}')

    # 1. Run / RunOnce / Policies Run（登入時執行）
    log('  檢查開機自動執行：登錄檔 Run 機碼 …')
    run_keys = []
    for hive in (HKCU, HKLM):
        for sub in (r'Software\Microsoft\Windows\CurrentVersion\Run',
                    r'Software\Microsoft\Windows\CurrentVersion\RunOnce',
                    r'Software\Microsoft\Windows\CurrentVersion\RunServices',
                    r'Software\Microsoft\Windows\CurrentVersion\RunServicesOnce',
                    r'Software\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run',
                    r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run',
                    r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\RunOnce'):
            run_keys.append((hive, sub))
    for hive, sub in run_keys:
        for name, val in _reg_values(hive, sub):
            emit(f'{hn[hive]}\\…\\{sub.split(chr(92))[-1]}：{name}', val,
                 path=f'{hn[hive]}\\{sub}')

    # 2. Winlogon：Shell／Userinit 被改是經典持久化手法
    log('  檢查開機自動執行：Winlogon、BootExecute、IFEO、AppInit …')
    wl = dict(_reg_values(HKLM, r'Software\Microsoft\Windows NT\CurrentVersion\Winlogon'))
    shell = str(wl.get('Shell', 'explorer.exe')).strip().lower()
    if shell not in ('explorer.exe', 'explorer.exe,'):
        rep.add('HIGH', cat, f'Winlogon Shell 被修改：{wl.get("Shell")}',
                '正常值是 explorer.exe', r'HKLM\…\Winlogon')
    uinit = str(wl.get('Userinit', '')).strip().lower().rstrip(',')
    if uinit and not re.fullmatch(r'[a-z]:\\windows\\system32\\userinit\.exe', uinit):
        rep.add('HIGH', cat, f'Winlogon Userinit 被修改：{wl.get("Userinit")}',
                r'正常值是 C:\Windows\system32\userinit.exe,', r'HKLM\…\Winlogon')
    hcu_wl = dict(_reg_values(HKCU, r'Software\Microsoft\Windows NT\CurrentVersion\Winlogon'))
    if hcu_wl.get('Shell'):
        rep.add('HIGH', cat, f'使用者層級的 Winlogon Shell：{hcu_wl.get("Shell")}',
                '正常情況下這裡不會有值', r'HKCU\…\Winlogon')

    # 3. BootExecute（開機最早期執行）
    sm = dict(_reg_values(HKLM, r'SYSTEM\CurrentControlSet\Control\Session Manager'))
    be = sm.get('BootExecute')
    if be is not None:
        items = be if isinstance(be, list) else [be]
        odd = [x for x in items if x and x.strip().lower() not in ('autocheck autochk *',)]
        if odd:
            rep.add('MED', cat, f'BootExecute 有額外項目：{odd}',
                    '正常值只有 autocheck autochk *', r'HKLM\…\Session Manager')

    # 4. Image File Execution Options：Debugger 劫持
    ifeo = r'Software\Microsoft\Windows NT\CurrentVersion\Image File Execution Options'
    for exe_name in _reg_subkeys(HKLM, ifeo):
        vals = dict(_reg_values(HKLM, f'{ifeo}\\{exe_name}'))
        if vals.get('Debugger'):
            lv, why, _ = _judge(vals['Debugger'], days)
            if lv in ('INFO', 'LOW'):
                lv = 'MED'      # IFEO Debugger 本身就少見，至少列為中風險
            rep.add(lv, cat, f'IFEO 劫持：執行 {exe_name} 時改跑 {vals["Debugger"]}',
                    (('；'.join(why) + '｜') if why else '') +
                    '除非你裝了除錯工具，否則這裡不該有值',
                    f'HKLM\\…\\Image File Execution Options\\{exe_name}')

    # 5. AppInit_DLLs
    for sub in (r'Software\Microsoft\Windows NT\CurrentVersion\Windows',
                r'Software\WOW6432Node\Microsoft\Windows NT\CurrentVersion\Windows'):
        v = dict(_reg_values(HKLM, sub))
        if str(v.get('AppInit_DLLs', '')).strip():
            rep.add('HIGH', cat, f'AppInit_DLLs：{v.get("AppInit_DLLs")}',
                    '每個載入 user32.dll 的程式都會注入這些 DLL', f'HKLM\\{sub}')

    # 6. 登入腳本
    env = dict(_reg_values(HKCU, r'Environment'))
    if env.get('UserInitMprLogonScript'):
        lv, why, _ = _judge(env['UserInitMprLogonScript'], days)
        rep.add('HIGH' if lv == 'HIGH' else 'MED', cat,
                f'使用者登入腳本：{env["UserInitMprLogonScript"]}',
                (('；'.join(why) + '｜') if why else '') +
                '一般個人電腦不會設定這一項', r'HKCU\Environment')

    # 7. 啟動資料夾
    log('  檢查開機自動執行：啟動資料夾 …')
    for sd in (os.path.expandvars(r'%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup'),
               os.path.expandvars(r'%PROGRAMDATA%\Microsoft\Windows\Start Menu\Programs\StartUp')):
        if not os.path.isdir(sd):
            continue
        for fn in os.listdir(sd):
            if fn.lower() == 'desktop.ini':
                continue
            fp = os.path.join(sd, fn)
            txt = read_text(fp) or ''
            target = ''
            if fn.lower().endswith('.lnk'):
                # .lnk 是二進位，抓出裡面的路徑字串（UTF-16 與 ASCII）
                try:
                    raw = open(fp, 'rb').read()
                    cand = re.findall(rb'[A-Za-z]:\\[^\x00"<>|]{3,200}', raw)
                    cand += [x.decode('utf-16le', 'ignore').encode() for x in
                             re.findall(rb'(?:[A-Za-z]\x00:\x00\\\x00(?:[^\x00]\x00){3,200})', raw)]
                    cand = [x.decode('utf-8', 'ignore').strip() for x in cand]
                    # .lnk 裡常把路徑拆成「C:\\Users\\」+ 後綴，優先取帶執行檔副檔名、最長的那個
                    exe_c = [x for x in cand if re.search(r'\.(exe|bat|cmd|ps1|vbs|js|py|pyw|com|scr)\b',
                                                           x, re.I)]
                    target = max(exe_c or cand, key=len) if cand else ''
                except Exception:
                    target = ''
            cmd = target or txt[:300] or fn
            lv, why, _ = _judge(cmd, days)
            ct = to_dt(os.path.getctime(fp))
            if ct and ct > dt.datetime.now() - dt.timedelta(days=days) and lv in ('INFO', 'LOW'):
                lv, why = 'MED', why + [f'捷徑近 {days} 天內建立']
            rep.add(lv, cat, f'啟動資料夾：{fn}',
                    (('；'.join(why) + '｜') if why else '') +
                    f'目標：{target or "（腳本內容）"}；建立 {fmt_t(os.path.getctime(fp))}', fp)

    # 8. 排程工作：連 \Microsoft\ 底下也查（攻擊者常把工作藏在那裡）
    log('  檢查開機自動執行：排程工作 …')
    tasks = powershell_json(
        'Get-ScheduledTask | ForEach-Object { [pscustomobject]@{'
        'Name=$_.TaskName; Path=$_.TaskPath; State=[string]$_.State; '
        'Author=$_.Author; Date=$_.Date; '
        'Triggers=(($_.Triggers | ForEach-Object { $_.CimClass.CimClassName -replace "MSFT_Task","" }) -join ","); '
        'Exec=(($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join " | ")'
        '} } | ConvertTo-Json -Compress', timeout=240)
    if isinstance(tasks, list):
        recent = dt.datetime.now() - dt.timedelta(days=days)
        for t in tasks:
            ex = (t.get('Exec') or '').strip()
            if not ex:
                continue
            tp = (t.get('Path') or '') + (t.get('Name') or '')
            lv, why, exe = _judge(ex, days)
            ms = (t.get('Path') or '').startswith('\\Microsoft\\')
            # 微軟內建工作只在指向使用者資料夾或有強特徵時才報
            if ms and lv in ('INFO', 'LOW', 'MED') and (not (exe and _USER_WRITABLE.search(exe))
                                                         or _TRUSTED.search(exe)):
                continue
            if ms and exe and _USER_WRITABLE.search(exe) and not _TRUSTED.search(exe) \
                    and lv in ('INFO', 'LOW'):
                lv, why = 'MED', why + ['藏在 \\Microsoft\\ 底下卻指向使用者資料夾']
            reg = _parse_ts(str(t.get('Date') or ''))
            if reg and reg > recent and lv in ('INFO', 'LOW'):
                lv, why = 'MED', why + [f'近 {days} 天內註冊']
            if reg:
                rep.when(reg, f'排程工作註冊：{tp}') if lv in ('HIGH', 'MED') else None
            if lv == 'INFO' and not t.get('Triggers', '').lower().count('boot') \
                    and not t.get('Triggers', '').lower().count('logon'):
                continue
            rep.add(lv, cat, f'排程工作：{tp}',
                    (('；'.join(why) + '｜') if why else '') +
                    f'[{t.get("State")}] 觸發：{t.get("Triggers") or "?"}；'
                    f'作者：{t.get("Author") or "?"}；註冊：{t.get("Date") or "?"}｜{ex}')
    elif isinstance(tasks, dict):
        rep.add('INFO', cat, '排程工作讀取失敗', tasks.get('error', ''))

    # 9. 服務：自動啟動、而且不在 Windows／Program Files 底下
    log('  檢查開機自動執行：服務 …')
    svcs = powershell_json(
        'Get-CimInstance Win32_Service | Where-Object { $_.StartMode -in "Auto","Boot","System" } | '
        'Select-Object Name,DisplayName,StartMode,State,PathName,StartName | '
        'ConvertTo-Json -Compress', timeout=120)
    if isinstance(svcs, list):
        for v in svcs:
            pn = (v.get('PathName') or '').strip()
            if not pn:
                continue
            exe = _exe_from_cmd(pn)
            if _SYS_DIRS.search(pn) or _SYS_DIRS.search(exe or '') or _TRUSTED.search(exe or ''):
                if not IOC_RE.search(pn):
                    continue
            lv, why, _ = _judge(pn, days)
            if lv == 'INFO':
                lv = 'LOW'
                why.append('自動啟動的服務不在系統或 Program Files 目錄')
            rep.add(lv, cat, f'服務：{v.get("Name")}（{v.get("DisplayName")}）',
                    (('；'.join(why) + '｜') if why else '') +
                    f'[{v.get("StartMode")}/{v.get("State")}] 帳號 {v.get("StartName")}｜{pn}')

    # 10. WMI 事件訂閱（無檔案持久化）
    log('  檢查開機自動執行：WMI 事件訂閱 …')
    wmi = powershell_json(
        'Get-CimInstance -Namespace root\\subscription -ClassName __EventConsumer | '
        'Select-Object @{n="Class";e={$_.CimClass.CimClassName}},Name,CommandLineTemplate,'
        'ExecutablePath,ScriptText | ConvertTo-Json -Compress')
    if isinstance(wmi, list):
        for w in wmi:
            klass = w.get('Class') or ''
            if klass in ('CommandLineEventConsumer', 'ActiveScriptEventConsumer'):
                body = (w.get('CommandLineTemplate') or w.get('ExecutablePath') or
                        w.get('ScriptText') or '')
                lv = 'HIGH' if (IOC_RE.search(body) or _PS_HIDDEN.search(body)) else 'MED'
                rep.add(lv, cat, f'WMI 事件訂閱會執行程式：{w.get("Name")}（{klass}）',
                        '這是不留檔案的持久化手法，一般使用者電腦上很少見｜' + body[:400])
    elif isinstance(wmi, dict):
        rep.add('INFO', cat, 'WMI 訂閱讀取失敗（可能需要系統管理員權限）', wmi.get('error', ''))

    # 11. PowerShell 設定檔（每次開 PowerShell 都會執行）
    docs = os.path.expanduser(r'~\Documents')
    for fp in (os.path.join(docs, 'WindowsPowerShell', 'profile.ps1'),
               os.path.join(docs, 'WindowsPowerShell', 'Microsoft.PowerShell_profile.ps1'),
               os.path.join(docs, 'PowerShell', 'profile.ps1'),
               os.path.join(docs, 'PowerShell', 'Microsoft.PowerShell_profile.ps1'),
               os.path.expandvars(r'%WINDIR%\System32\WindowsPowerShell\v1.0\profile.ps1'),
               os.path.expandvars(
                   r'%WINDIR%\System32\WindowsPowerShell\v1.0\Microsoft.PowerShell_profile.ps1')):
        if os.path.isfile(fp):
            txt = read_text(fp) or ''
            lv = 'HIGH' if (IOC_RE.search(txt) or _PS_HIDDEN.search(txt)) else 'INFO'
            rep.add(lv, cat, f'PowerShell 設定檔：{os.path.basename(fp)}',
                    f'建立 {fmt_t(os.path.getctime(fp))}｜' + txt[:300], fp)

    # 12. 系統 Python／使用者 site-packages 的啟動掛勾（你的批次工具也會跑到）
    log('  檢查開機自動執行：系統 Python 的 .pth／sitecustomize …')
    py_sites = []
    base = os.path.expandvars(r'%LOCALAPPDATA%\Programs\Python')
    if os.path.isdir(base):
        for d in os.listdir(base):
            py_sites.append(os.path.join(base, d, 'Lib', 'site-packages'))
    ubase = os.path.expandvars(r'%APPDATA%\Python')
    if os.path.isdir(ubase):
        for d in os.listdir(ubase):
            py_sites.append(os.path.join(ubase, d, 'site-packages'))
    for sp in py_sites:
        if os.path.isdir(sp):
            scan_python_hooks(rep, sp, '系統 Python')


def scan_python_hooks(rep, sp, label):
    """site-packages 裡 Python 每次啟動都會自動執行的東西。"""
    for fn in sorted(os.listdir(sp)):
        fp = os.path.join(sp, fn)
        if fn.endswith('.pth'):
            txt = read_text(fp) or ''
            code = [l for l in txt.splitlines() if l.strip().startswith('import')]
            if not code:
                continue
            bad = any(re.search(r'exec|eval|base64|subprocess|urllib|socket|Popen|'
                                r'b64decode|marshal|compile\(', l) for l in code)
            stem = re.escape(fn[:-4])
            # 常見的正常寫法：只 exec 一段「__import__("自己套件名")」的程式（例如 coloredlogs.pth）
            benign = bad and all(
                not re.search(r'base64|subprocess|urllib|socket|Popen|b64decode|marshal|compile\(', l)
                and re.search(r'__import__\([\'"]_?' + stem + r'[\'"]\)', l, re.I)
                for l in code if re.search(r'exec|eval', l))
            lv = 'HIGH' if ((bad and not benign) or IOC_RE.search(txt)) else 'INFO'
            rep.add(lv, '開機自動執行' if label else 'site-packages',
                    f'{label}：{fn}（每次啟動 Python 自動執行）'
                    + ('（含可疑呼叫）' if lv == 'HIGH' else ''),
                    f'建立 {fmt_t(os.path.getctime(fp))}｜' + ' ⏎ '.join(code)[:400], fp)
        elif fn in ('sitecustomize.py', 'usercustomize.py'):
            txt = read_text(fp) or ''
            lv = 'HIGH' if IOC_RE.search(txt) else 'MED'
            rep.add(lv, '開機自動執行', f'{label}：{fn} 存在（每次啟動 Python 自動執行）',
                    f'建立 {fmt_t(os.path.getctime(fp))}｜' + txt[:300], fp)


def check_processes(rep, root):
    if not IS_WIN:
        return
    log('  檢查執行中程序與對外連線 …')
    procs = powershell_json(
        'Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,'
        'Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress', timeout=120)
    pid_name = {}
    if isinstance(procs, list):
        for p in procs:
            pid_name[p.get('ProcessId')] = p.get('Name')
            exe = p.get('ExecutablePath') or ''
            cmd = p.get('CommandLine') or ''
            nm = (p.get('Name') or '').lower()
            if re.search(r'python[w]?(\.exe)?"?\s+-\s+-s\s+\S*main\.py', cmd, re.I):
                rep.add('HIGH', '程序', f'偽裝成 ComfyUI 的後門程序（PID {p.get("ProcessId")}）',
                        '命令列的「-」代表真正的程式碼從標準輸入讀入，後面的 main.py 只是幌子。'
                        + cmd, exe)
            elif IOC_RE.search(cmd) or nm == 'pythonm.exe':
                rep.add('HIGH', '程序', f'執行中的挖礦程序：{p.get("Name")} '
                        f'（PID {p.get("ProcessId")}，父 {p.get("ParentProcessId")}）', cmd, exe)
            elif exe and under(exe, root) and nm not in ('python.exe', 'pythonw.exe'):
                rep.add('MED', '程序', f'ComfyUI 資料夾下執行的非 python 程序：{p.get("Name")}',
                        cmd, exe)
    conns = powershell_json(
        'Get-NetTCPConnection -State Established | Select-Object RemoteAddress,'
        'RemotePort,OwningProcess | ConvertTo-Json -Compress')
    if isinstance(conns, list):
        for c in conns:
            rp = c.get('RemotePort')
            if c.get('RemoteAddress') in C2_IPS:
                rep.add('HIGH', '程序', f'正連線到後門 C2 伺服器 {c.get("RemoteAddress")}:{rp}',
                        f'PID {c.get("OwningProcess")} '
                        f'{pid_name.get(c.get("OwningProcess"), "")}')
            elif rp in STRATUM_PORTS:
                rep.add('MED', '程序', f'連線到礦池常用埠 {c.get("RemoteAddress")}:{rp}',
                        f'PID {c.get("OwningProcess")} '
                        f'{pid_name.get(c.get("OwningProcess"), "")}')


def check_miner_logs(rep, root):
    log('  搜尋挖礦紀錄檔（gpu.log 等）…')
    places = [root]
    if IS_WIN:
        places += [os.path.expandvars(x) for x in
                   (r'%TEMP%', r'%APPDATA%', r'%LOCALAPPDATA%', r'%PROGRAMDATA%')]
    names = {'gpu.log', 'xmrig.log', 'miner.log', 'lolminer.log', 'srbminer.log'}
    seen = set()
    for base in places:
        if not os.path.isdir(base):
            continue
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d.lower() not in SKIP_DIRS
                      and d.lower() not in ('packages', 'microsoft', 'google', 'mozilla')]
            if dp.count(os.sep) - base.count(os.sep) > 6:
                dns[:] = []
            for fn in fns:
                if fn.lower() not in names:
                    continue
                fp = os.path.join(dp, fn)
                if fp in seen:
                    continue
                seen.add(fp)
                txt = read_text(fp, limit=256 * 1024 * 1024) or ''
                lines = [l for l in txt.splitlines() if l.strip()]
                first = lines[0][:200] if lines else ''
                last = lines[-1][:200] if lines else ''
                is_miner = bool(IOC_RE.search(txt[:200000])) or 'hashrate' in txt.lower() \
                    or 'accepted' in txt.lower()
                lv = 'HIGH' if is_miner else 'INFO'
                rep.add(lv, '紀錄檔', f'{fn}' + ('（挖礦紀錄）' if is_miner else ''),
                        f'建立 {fmt_t(os.path.getctime(fp))}；最後寫入 '
                        f'{fmt_t(os.path.getmtime(fp))}；第一行：{first}；最後一行：{last}', fp)
                if is_miner:
                    rep.when(to_dt(os.path.getctime(fp)), f'挖礦紀錄檔建立：{fp}')
                    rep.when(to_dt(os.path.getmtime(fp)), f'挖礦紀錄最後寫入：{fp}')
                    # 紀錄內文的時間戳比檔案時間可靠（檔案時間可被竄改）
                    t0 = _parse_ts(first)
                    ct = to_dt(os.path.getctime(fp))
                    shift = None
                    if t0 and ct:
                        diff = (ct - t0).total_seconds() / 3600
                        if abs(diff - round(diff)) < 0.02 and round(diff) != 0:
                            shift = round(diff)
                    for tag, line in (('第一筆', first), ('最後一筆', last)):
                        t = _parse_ts(line)
                        if t:
                            if shift:
                                t = t + dt.timedelta(hours=shift)
                            rep.when(t, f'挖礦紀錄{tag}{"（UTC 換算本地時間）" if shift else ""}：{fn}')


_TS_RE = re.compile(r'(\d{4})[-/](\d{2})[-/](\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?')


def _parse_ts(line):
    m = _TS_RE.search(line or '')
    if not m:
        return None
    try:
        y, mo, d, h, mi, se = m.groups()
        return dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(se or 0))
    except Exception:
        return None


def check_id_files(rep, root):
    """後門第一次執行時會寫一個 id.txt 當受害者編號，建立時間＝後門首次執行。"""
    cands = [os.path.expanduser('~/id.txt'), os.path.join(root, 'id.txt'),
             os.path.join(os.path.dirname(root), 'id.txt')]
    for fp in cands:
        if not os.path.isfile(fp):
            continue
        txt = (read_text(fp) or '').strip()
        lv = 'HIGH' if re.fullmatch(r'id_\d{4,10}', txt) else 'INFO'
        rep.add(lv, '後門', f'受害者編號檔 {os.path.basename(fp)}：{txt[:40]}',
                f'建立 {fmt_t(os.path.getctime(fp))}（＝後門首次執行時間）', fp)
        if lv == 'HIGH':
            rep.when(to_dt(os.path.getctime(fp)), f'後門首次執行（寫入 {fp}）')


def check_defender(rep):
    if not IS_WIN:
        return
    log('  讀取 Windows Defender 偵測紀錄 …')
    data = powershell_json(
        'Get-MpThreatDetection | Select-Object InitialDetectionTime,ThreatID,'
        'ActionSuccess,@{n="Resources";e={$_.Resources -join "; "}} | '
        'ConvertTo-Json -Compress')
    if isinstance(data, list):
        if not data:
            rep.add('INFO', 'Defender', '沒有偵測紀錄（礦工常被設計成躲過防毒，這不代表乾淨）')
        for d in data:
            rep.add('MED', 'Defender', f'偵測紀錄：ThreatID {d.get("ThreatID")}',
                    f'{d.get("InitialDetectionTime")}；處置成功={d.get("ActionSuccess")}；'
                    f'{d.get("Resources")}')
    ex = powershell_json(
        'Get-MpPreference | Select-Object ExclusionPath,ExclusionProcess | '
        'ConvertTo-Json -Compress')
    if isinstance(ex, list) and ex:
        paths = ex[0].get('ExclusionPath') or []
        procs = ex[0].get('ExclusionProcess') or []
        if isinstance(paths, str):
            paths = [paths]
        if isinstance(procs, str):
            procs = [procs]
        items = [str(p) for p in paths + procs]
        if any('administrator' in i.lower() for i in items):
            rep.add('INFO', 'Defender', '排除清單需要系統管理員權限才能讀取',
                    '以「系統管理員身分」開 PowerShell 再執行一次本腳本，才能檢查這一項。')
            items = []
        for p in items:
            lv = 'HIGH' if re.search(r'comfy|python|venv|temp|appdata', str(p), re.I) else 'MED'
            rep.add(lv, 'Defender', f'Defender 排除項目：{p}',
                    '惡意程式常把自己的路徑加進排除清單。不是你自己加的就刪掉。')


def check_recent_files(rep, root, days):
    """ComfyUI 根目錄、custom_nodes、venv\\Scripts、user 底下近期出現的執行檔與腳本。"""
    log('  整理近期新增的執行檔與腳本 …')
    recent = dt.datetime.now() - dt.timedelta(days=days)
    areas = [os.path.join(root, 'custom_nodes'), os.path.join(root, 'venv', 'Scripts'),
             os.path.join(root, 'user')]
    for top in areas:
        if not os.path.isdir(top):
            continue
        for fp in walk(top):
            ext = os.path.splitext(fp)[1].lower()
            if ext not in EXEC_EXT or ext == '.pyd':
                continue
            try:
                ct = to_dt(os.path.getctime(fp))
            except Exception:
                continue
            if ct and ct > recent:
                rep.when(ct, f'新檔案：{os.path.relpath(fp, root)}')
    # 根目錄頂層
    for fn in os.listdir(root):
        fp = os.path.join(root, fn)
        if os.path.isfile(fp) and os.path.splitext(fn)[1].lower() in EXEC_EXT:
            ct = to_dt(os.path.getctime(fp))
            if ct and ct > recent:
                rep.when(ct, f'新檔案（根目錄）：{fn}')


# ---------------------------------------------------------------- 輸出
def _dedupe(findings):
    seen, out = {}, []
    for f in findings:
        k = (f['level'], f['category'], f['title'])
        if k in seen:
            seen[k]['_n'] += 1
            continue
        g = dict(f, _n=1)
        seen[k] = g
        out.append(g)
    for g in out:
        if g['_n'] > 1:
            g['title'] += f'（共 {g["_n"]} 處）'
    return out


def render(rep, root, args):
    rep.findings = _dedupe(rep.findings)
    out = io.StringIO()
    w = lambda s='': out.write(s + '\n')
    now = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    w('=' * 78)
    w(f'  ComfyUI 入侵檢查報告　{now}')
    w(f'  檢查目錄：{root}　時間範圍：近 {args.days} 天')
    w('=' * 78)
    cnt = {k: 0 for k in LEVEL_ORDER}
    for f in rep.findings:
        cnt[f['level']] += 1
    w(f'  高風險 {cnt["HIGH"]}　中風險 {cnt["MED"]}　低 {cnt["LOW"]}　資訊 {cnt["INFO"]}')
    w()
    if cnt['HIGH']:
        w('【需要立即處理】')
        for f in rep.findings:
            if f['level'] == 'HIGH':
                w(f'  ✗ [{f["category"]}] {f["title"]}')
        w()
    cats = []
    for f in rep.findings:
        if f['category'] not in cats:
            cats.append(f['category'])
    for cat in cats:
        items = sorted((f for f in rep.findings if f['category'] == cat),
                       key=lambda f: LEVEL_ORDER[f['level']])
        if not args.verbose:
            items = [f for f in items if f['level'] != 'INFO'] or items[:3]
        w('-' * 78)
        w(f'■ {cat}')
        for f in items:
            w(f'  {LEVEL_MARK[f["level"]]}　{f["title"]}')
            if f['detail']:
                for line in _wrap(f['detail'], 70):
                    w(f'          {line}')
            if f['path']:
                w(f'          路徑：{f["path"]}')
    w('-' * 78)
    w('■ 時間軸（由舊到新）')
    tl = sorted({(t, s) for t, s in rep.timeline if t}, key=lambda x: x[0])
    if not tl:
        w('  （無）')
    for t, s in tl[-120:]:
        w(f'  {t.strftime("%Y-%m-%d %H:%M:%S")}  {s}')
    w('=' * 78)
    w('說明：本腳本唯讀，沒有刪除或修改任何檔案。')
    w('      「高風險」項目請先備份證據（路徑、時間、SHA256）再處理。')
    return out.getvalue()


def _wrap(s, n):
    s = str(s)
    return [s[i:i + n] for i in range(0, len(s), n)] or ['']


def main():
    ap = argparse.ArgumentParser(description='ComfyUI 入侵檢查（唯讀）')
    ap.add_argument('--root', default=DEFAULT_ROOT, help='ComfyUI 資料夾')
    ap.add_argument('--days', type=int, default=14, help='時間軸回溯天數（預設 14）')
    ap.add_argument('--quick', action='store_true', help='略過 site-packages 全面掃描')
    ap.add_argument('--verbose', action='store_true', help='報告列出所有資訊級項目')
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        log(f'找不到 ComfyUI 資料夾：{root}（用 --root 指定）')
        sys.exit(1)

    log(f'開始檢查：{root}')
    rep = Report()
    steps = [
        ('custom_nodes', lambda: check_custom_nodes(rep, root, args.days)),
        ('venv', lambda: check_venv_scripts(rep, root, args.days)),
        ('site-packages', lambda: check_site_packages(rep, root, args.days, not args.quick)),
        ('網路', lambda: check_exposure(rep, root)),
        ('開機自動執行', lambda: check_persistence(rep, root, args.days)),
        ('程序', lambda: check_processes(rep, root)),
        ('紀錄檔', lambda: check_miner_logs(rep, root)),
        ('後門', lambda: check_id_files(rep, root)),
        ('Defender', lambda: check_defender(rep)),
        ('時間軸', lambda: check_recent_files(rep, root, args.days)),
    ]
    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            rep.add('INFO', name, f'此項檢查發生錯誤：{e}')

    text = render(rep, root, args)
    print()
    print(text)

    here = os.path.dirname(os.path.abspath(__file__))
    stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    txt_p = os.path.join(here, f'comfy_audit_{stamp}.txt')
    json_p = os.path.join(here, f'comfy_audit_{stamp}.json')
    with open(txt_p, 'w', encoding='utf-8-sig') as f:
        f.write(text)
    with open(json_p, 'w', encoding='utf-8') as f:
        json.dump({'root': root, 'generated': stamp, 'findings': rep.findings,
                   'timeline': [(t.isoformat(), s) for t, s in
                                sorted(rep.timeline, key=lambda x: x[0]) if t]},
                  f, ensure_ascii=False, indent=2)
    print(f'報告已存：{txt_p}')
    print(f'　　　　　{json_p}')


if __name__ == '__main__':
    main()
