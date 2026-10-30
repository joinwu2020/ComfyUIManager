# -*- coding: utf-8 -*-
"""
設定檔讀寫：用 AST 定位 comfy_env.py / update.py 裡的頂層賦值，原地換掉。

為什麼用 AST 而不是正規表示式：設定值裡有巢狀大括號、中文註解、多行 dict，
用 regex 很容易吃掉旁邊的註解或切錯邊界。AST 給的是精確的行範圍。

只會動白名單內的變數，其他內容（包含註解、空行、f-string 如 TORCH_INDEX）
一個位元組都不碰。
"""
import ast
import datetime
import os
import shutil


class ConfigError(Exception):
    pass


def _tree(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    return src, src.splitlines(keepends=True), ast.parse(src)


def read_assignments(path, names):
    """回傳 {變數名: 已求值的 Python 物件}。抓不到的名稱不會出現在結果裡。"""
    out = {}
    try:
        src, lines, tree = _tree(path)
    except (OSError, SyntaxError) as ex:
        raise ConfigError(f"無法解析 {os.path.basename(path)}：{ex}")
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in names:
                try:
                    out[t.id] = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    pass        # 例如 f-string、運算式：不可編輯，略過
    return out


def _spans(path, names):
    """{變數名: (起始行索引, 結束行索引)}　0-based、結束為不含"""
    src, lines, tree = _tree(path)
    spans = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in names:
                spans[t.id] = (node.lineno - 1, node.end_lineno)
    return src, lines, spans


def _q(v):
    """把值寫成原始碼。

    字串優先用雙引號，跟 comfy_env.py 原本的風格一致 —— 但「只有在
    安全的時候」。Windows 路徑 C:\\Users\\... 直接包雙引號會變成
    COMFY_ROOT = "C:\\Users\\..."，而 \\U 在 Python 字串裡是跳脫序列，
    寫出去就是語法錯誤，整個存檔會被安全檢查擋下。
    含反斜線或引號的字串一律交給 repr()，它會正確處理跳脫。
    """
    if isinstance(v, str) and '"' not in v and "\\" not in v and "'" not in v:
        return '"' + v + '"'
    return repr(v)


def render_value(name, value, comment=""):
    """把 Python 物件寫成好讀的原始碼。dict / list 一行一項。
    comment：原本掛在賦值第一行的行尾註解，照原樣保留。"""
    c = f"  {comment}" if comment else ""
    if isinstance(value, dict):
        if not value:
            return f"{name} = {{}}{c}"
        w = max(len(_q(k)) for k in value)
        body = "".join(f"    {_q(k):<{w}}: {_q(v)},\n" for k, v in value.items())
        return f"{name} = {{{c}\n{body}}}"
    if isinstance(value, tuple):
        if not value:
            return f"{name} = (){c}"
        return f"{name} = ({', '.join(_q(v) for v in value)}){c}"
    if isinstance(value, list):
        if not value:
            return f"{name} = []{c}"
        one = f"{name} = [{', '.join(_q(v) for v in value)}]{c}"
        if len(one) <= 88:
            return one
        body = "".join(f"    {_q(v)},\n" for v in value)
        return f"{name} = [{c}\n{body}]"
    return f"{name} = {_q(value)}{c}"


def write_assignments(path, updates, backup=True):
    """把 {變數名: 新值} 寫回檔案。回傳實際變更的變數名清單。

    由後往前取代，才不會讓前面的行號位移。
    """
    if not updates:
        return []
    src, lines, spans = _spans(path, set(updates))
    missing = [n for n in updates if n not in spans]
    if missing:
        raise ConfigError(f"{os.path.basename(path)} 裡找不到這些設定："
                          + "、".join(missing))

    current = read_assignments(path, set(updates))
    changed = [n for n, v in updates.items() if current.get(n, object()) != v]
    if not changed:
        return []

    if backup:
        bak = f"{path}.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(path, bak)

    for name in sorted(changed, key=lambda n: spans[n][0], reverse=True):
        a, b = spans[name]
        # 保留原本掛在第一行的行尾註解（例：TORCH_PINS = { # 精確版本…）
        first = lines[a]
        comment = ""
        if "#" in first:
            tail = first[first.index("#"):].rstrip("\r\n")
            if tail.count('"') % 2 == 0 and tail.count("'") % 2 == 0:
                comment = tail
        text = render_value(name, updates[name], comment)
        if not text.endswith("\n"):
            text += "\n"
        lines[a:b] = [text]

    new_src = "".join(lines)
    try:                                 # 寫回去之前先確認還是合法 Python
        ast.parse(new_src)
    except SyntaxError as ex:
        raise ConfigError(f"產生的內容語法錯誤，已放棄寫入：{ex}")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_src)
    return changed
