# -*- coding: utf-8 -*-
"""
無 console 的進入點（給捷徑用）

用 venv\\Scripts\\pythonw.exe 執行這支，就不會有黑色 CMD 視窗。
功能和 comfy_manager.py 完全一樣——它只是 import 那支然後呼叫 main()，
不是另一份複製，所以兩邊永遠同步。

為什麼不直接把 comfy_manager.py 改名成 .pyw：
    pythonw 沒有 stdout / stderr。程式啟動階段若拋出例外（import 失敗、
    tkinter 主題壞掉、comfy_tools.py 不見），視窗根本不會出現，而且
    一個字都不會留下。這一層把 traceback 寫進 update_logs\\，
    再跳一個對話框告訴你檔案在哪。

    （之前那個 NameError: name 'T' is not defined 就是這類問題。
      有 console 時看得到，沒 console 時會變成「按了沒反應」。）
"""
import datetime
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _crash(exc_type, exc, tb):
    """把例外寫進紀錄檔，然後盡量跳個對話框"""
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(HERE, "update_logs", f"manager_crash_{stamp}.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
            f.write(f"python  {sys.version}\n")
            f.write(f"exe     {sys.executable}\n")
            f.write(f"cwd     {os.getcwd()}\n")
            f.write(f"here    {HERE}\n\n")
            f.write(text)
    except OSError:
        path = "（連紀錄檔都寫不出來）"
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(
            "Comfy Manager 啟動失敗",
            f"{exc_type.__name__}: {exc}\n\n詳細內容：\n{path}")
        r.destroy()
    except Exception:
        pass


def main():
    sys.excepthook = _crash          # 接住 mainloop 之後才發生的例外
    try:
        import comfy_manager
    except Exception:
        _crash(*sys.exc_info())
        return 1
    try:
        comfy_manager.main()
    except SystemExit:
        raise
    except Exception:
        _crash(*sys.exc_info())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
