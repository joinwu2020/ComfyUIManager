ComfyUI 環境管理工具組
======================================================================

全部解壓到 ComfyUI 根目錄（和 main.py 同一層）覆蓋。


■ 解譯器的分工（這版最重要的改變）
----------------------------------------------------------------------
管理介面本身跑在「系統」Python，不 activate venv。
它只用標準庫 + tkinter，三個後端模組（comfy_config_io、
comfy_nodes_backup、comfy_sys）也都只用標準庫。

要不要用 venv 是每個動作各自決定：

    venv  啟動 ComfyUI、更新、安裝依賴、安全檢查、修復、裝 wheel
          （這些會 import torch，或要改 site-packages）
    系統  重建 venv、純 git 操作
          （不能從 venv 裡面重建 venv 自己）

為什麼要這樣：如果介面是由 venv 的 Python 啟動的，
「重建 venv」就會失敗 —— install.py 偵測到在 venv 內會直接拒絕。
所以 manager.bat 不 activate，make_shortcut.bat 也優先用系統的
pythonw.exe。host_py() 另外會檢查路徑，就算你先 activate 再開，
它仍然找得到 venv 以外的 Python。


■ 檔案
----------------------------------------------------------------------
comfy_manager.py    圖形介面（本次優化過，見下）
comfy_manager.pyw   同一個介面但不開 console（捷徑用）
                    只是 import comfy_manager 再呼叫 main()，不是複製。
                    pythonw 沒有 stderr，所以這層會把 traceback 寫進
                    update_logs\manager_crash_時間.log 並跳對話框。
comfy_manager.spec  PyInstaller 設定（選用）

comfy_env.py        環境參數 + 共用函式庫
                    單獨執行 = 唯讀體檢：python comfy_env.py
update.py           更新本體與節點
install.py          安裝 / 重建（本體缺檔案會自己 git clone）
comfy_audit.py      安全檢查

comfy_config_io.py    介面讀寫 comfy_env.py 的設定（AST）
comfy_nodes_backup.py 節點掃描 / 備份 / 還原
comfy_sys.py          程序、GPU、連接埠
repair_env.py         修復 torch / wheel
install_wheels.py     重裝 wheels/ 底下的自訂 wheel

run.bat            啟動 ComfyUI（輸出同步寫進 comfyui.log）
update.bat         更新
install.bat        安裝 / 重建（系統 Python）
scan.bat           安全檢查
manager.bat        開管理介面（保留 console，看得到錯誤）
make_shortcut.bat  建立桌面捷徑（無 console，用 comfyui.ico）
build_exe.bat      打包成 ComfyManager.exe（選用）

comfyui.ico        捷徑 / EXE 的圖示


■ 工具可以放在 ComfyUI 外面（1027 新增）
----------------------------------------------------------------------
comfy_env.py 設定區最前面多了一個 COMFY_ROOT：

    None          自動尋找（建議，可攜最穩）
    相對路徑       以腳本目錄為基準，例 ..\ComfyUI
    絕對路徑       例 C:\Users\User\Documents\comfy\ComfyUI

解析順序：

    1  環境變數 COMFY_ROOT
    2  comfy_env.py 的 COMFY_ROOT（絕對或相對）
    3  附近：腳本目錄本身 → 底下一層 → 上一層 → 上一層的其他子資料夾
    4  慣例位置：
           ~/Documents/comfy/ComfyUI
           ~/Documents/ComfyUI
           ~/OneDrive/Documents/comfy/ComfyUI   （文件被導向 OneDrive）
           ~/OneDrive/Documents/ComfyUI
           ~/comfy/ComfyUI
           ~/ComfyUI
           ~/Desktop/ComfyUI
       ~ 在 Windows 會展開成 C:\Users\你的帳號，換電腦或改帳號名都不用改設定。
    5  都找不到 → 退回腳本目錄，狀態列標示「本體 ✗」

第 3 步刻意只看附近，不往上爬更多層 —— 一路往上掃會在磁碟根目錄
那種地方撞到別人的 ComfyUI，認錯比找不到更糟。

可攜碟要注意：絕對路徑在磁碟代號改變時（D: → E:）就失效。
留空或用相對路徑都不會。介面的「瀏覽…」按鈕選完資料夾後，
只要離腳本目錄不遠就會自動存成相對路徑。

填了之後，管理腳本就能放在 ComfyUI 外面，本體目錄不會被塞一堆 .py
和 .bat，重裝或 git reset 也不會波及工具。
環境變數 COMFY_ROOT 的優先權高於這個設定。

東西分在哪：

    ComfyUI 本體      main.py、custom_nodes、models、user、venv
    腳本目錄          所有 .py / .bat、update_logs、env_state、
                      node_backups、wheels

wheels/ 先找腳本目錄，沒有才找本體目錄，舊的放法照樣能用。

介面的「環境參數」分頁第一個欄位就是它。改完要重開介面才生效
（路徑在啟動時決定）。

.bat 會用系統 python 問 comfy_env.py 要本體位置再去找 venv，
所以分開放也不用改 bat。


■ 設定存在 comfy_root.txt，更新包不會洗掉（1027）
----------------------------------------------------------------------
本體路徑寫進腳本目錄的 comfy_root.txt，不是寫進 comfy_env.py。
那支檔案不在更新包裡，所以換新版腳本不會把你的設定蓋掉
（之前每次更新都要重設一次，就是這個原因）。

    comfy_root.txt      一行路徑，絕對或相對於腳本目錄
    介面「環境參數」第一個欄位存的就是它

解析順序：環境變數 → comfy_root.txt → comfy_env.py 的 COMFY_ROOT
          → 自動尋找 → 腳本目錄

自動尋找現在分兩輪：先找「真的有 main.py 和 comfy/」的，找不到再找
「有 custom_nodes 又有 models / user / venv」的 —— 本體被砍掉準備
重裝時仍然認得出來。


■ 重建 venv 和補齊環境徹底分開（1027）
----------------------------------------------------------------------
以前兩顆按鈕共用同一個對話框，而 --recreate 會順便把整套套件裝一遍
—— 那不是「重建」。現在：

    重建 venv    install.py --recreate --venv-only
                 只把 venv 砍掉重建（舊的改名成 venv.bak_時間保留）。
                 不裝任何套件、不碰本體。跑完是一個空環境。

    補齊環境     install.py
                 本體（缺就 clone）→ venv（沒有就建）→ pip → torch →
                 requirements → 額外套件 → 節點依賴 → wheel。
                 --lock / --skip-nodes / --overwrite-core 都在這裡。

重建完要接著按「補齊環境」才會有套件。兩件事的風險和前提不同，
混在一顆按鈕裡會變成「想重建卻順便跑完整套，想補齊卻要先回答重建
的問題」。


■ 安裝 / 重建不再要求先有 venv（1027）
----------------------------------------------------------------------
「補齊環境」和「重建 venv」本來就是用來把環境建起來的，要求 venv
先存在邏輯不通。這兩顆按鈕現在一律用系統 Python —— install.py 的
外層本來就跑在系統 Python 上：建立（或沿用）venv，再用那個 venv
重新執行自己。

哪個動作用哪個 Python：

    系統 Python    補齊環境、重建 venv、覆蓋核心
    venv           純更新、環境修復、重裝 wheel、安全檢查
                   （這些會動 site-packages，用系統 Python 會裝錯地方）

venv 不存在時，後面那組會問「要現在建立環境嗎」，按是就直接進安裝
流程，不再只是叫你去按別的按鈕。

全新安裝實測：工具在桌面、本體目錄只剩 models/user/custom_nodes、
沒有 venv —— 一鍵就 clone 本體 + 建 venv 到正確位置，
紀錄檔和 env_state 留在工具目錄。


■ 指定的路徑一律採用，即使本體還沒裝（1027）
----------------------------------------------------------------------
原本 COMFY_ROOT 要通過「裡面有 main.py 和 comfy/」的驗證才採用，
不通過就改去自動尋找。這是錯的：

  本體還沒裝、或你剛把本體砍掉準備重裝，正是 install.py 和
  「覆蓋核心」要處理的情況。因為「看起來不像 ComfyUI」就把設定丟掉，
  那兩個功能就永遠裝不到你指定的地方 —— 還會退回腳本目錄，
  連 venv 都去那裡找（找不到 venv 的錯誤就是這樣來的）。

現在：明確指定的路徑無條件採用。目錄不存在也採用，install.py 會
建立它。驗證只保留給「自動尋找」，那裡是用猜的，猜錯比找不到更糟。

install.py 和介面的「覆蓋核心」現在共用同一支
comfy_env.ensure_core_tree()，所以行為一定一致：

    沒有 .git        建立目錄並 clone（目錄非空也沒關係）
    有 .git 沒檔案   fetch + reset
    有 .git 有檔案   同步 origin 後 fetch + reset

四種情況都實測過：空目錄、完全不存在的目錄、砍掉本體只留
models/user/custom_nodes、以及重複執行。使用者資料全程沒被動到。


■ 路徑分離後修掉的三個 bug（1027）
----------------------------------------------------------------------
1. 路徑存不進去
   comfy_config_io 寫字串優先用雙引號，於是 Windows 路徑變成
       COMFY_ROOT = "C:\Users\..."
   而 \U 是 Python 的跳脫序列 → 語法錯誤 → 安全檢查擋下整個存檔。
   欄位看起來就像「打了也沒用」。含反斜線的字串現在交給 repr()。

2. 覆蓋核心失效
   介面自己拼 git 指令，用的是「目前的工作目錄」。本體搬走之後那裡
   沒有 .git，直接 fatal: not a git repository。
   現在改呼叫 comfy_env.overwrite_core()，它自己解析 ROOT、偵測分支
   （不假設是 master）、順便同步 origin。

3. overwrite_core 本身也是壞的
   原本先 checkout 再 reset。只要本體有任何本地修改，checkout 就會
   拒絕（error: Your local changes would be overwritten by checkout）
   —— 而「覆蓋核心」的本意就是要丟掉那些修改，拒絕等於功能失效。
   改成先 reset --hard 清乾淨，再 checkout -f -B。

   驗證：本體同時有「未提交修改」和「本地 commit」的情況下執行覆蓋，
   兩者都被清掉、上游新檔進來，而 models / user / custom_nodes
   完全沒動。

另外 update.py 補上 sys.path.insert(自己的目錄)，隔離模式或被別的
啟動器叫起來時也找得到 comfy_env。


■ 卡死的修正（1027）
----------------------------------------------------------------------
症狀：重啟或更新 ComfyUI 之後，介面開始週期性卡住。

原因是 own_pid()。狀態輪詢每 1.5 秒呼叫它兩次，而平常 self.proc
存在、poll() 很快；但「重啟」或「更新後接管」之後 self.proc 是 None
（接管的是別人啟動的程序），於是每次都掉進 SYS.find_comfy() 的
WMI / PowerShell 查詢 —— 一次 300-800ms，一秒半兩次。

改法：own_pid() 只讀背景探測寫下的旗標，主執行緒零 I/O。

同時處理掉另外兩處會凍住視窗的：

    終止 / 重啟      kill_tree 每個最多等 20 秒 → 丟背景，畫面顯示
                     「停止中…」，關好才回呼
    更新前的關閉     ensure_stopped 的等待迴圈 → 改成非同步，呼叫端
                     統一走 run_when_stopped(名稱, 要做的事)

現在全檔主執行緒上的阻塞呼叫是 0 處（用 AST 掃描驗證）。


■ ComfyUI 的 console 改用 HTTP 讀
----------------------------------------------------------------------
以前接不到 console，是因為用的是管線或檔案：

  管線  只有「建立它的程序」讀得到。ComfyUI 如果是 run.bat 或其他方式
        開的，管理介面拿不到任何輸出。
  檔案  要嘛靠 run.bat 用 Tee 轉寫（只有走 run.bat 才有），要嘛讀
        user 目錄下的 comfyui.log（那是 logging 的 FileHandler 寫的，
        已經格式化成 [時間][等級]，而且沒有顏色）。

現在改成直接跟 ComfyUI 要：

    GET http://127.0.0.1:8188/internal/logs/raw

回傳的是 LogInterceptor 原樣攔下的 stdout/stderr，ANSI 色碼原封不動
—— 跟網頁版底部那個 Logs 面板讀的是同一份資料。所以：

  - 不管 ComfyUI 是誰啟動的，都看得到
  - 有顏色（[INFO] 綠、[WARNING] 黃、[ERROR] 紅，節點自己的色碼也認）
  - 存活判斷改問 /system_stats，不再掃程序表

掃程序表本來就會誤判成「已經關閉」：節點的 worker 子程序、剛結束還沒
被回收的殼、WMI 偶爾查不到。連接埠回得了 200 才是真的活著。

限制：那個緩衝區是 deque，預設只留最後 300 筆。輪詢間隔（0.9 秒）內
如果吐超過 300 筆，中間會漏掉 —— 這時畫面上會標示「緩衝區已滾動」，
不會假裝連續。


■ 關掉管理介面不會動到 ComfyUI
----------------------------------------------------------------------
ComfyUI 的 stdout 接到 DEVNULL（輸出走 HTTP，不需要管線）。
Windows 不會因為父程序結束就連帶殺掉子程序 —— 以前會被拖死是因為
管線，管線一斷子程序寫 stdout 就卡住。現在沒有管線，所以管理介面
關掉、甚至被工作管理員結束，ComfyUI 都照常跑。

啟動旗標是 CREATE_NO_WINDOW + CREATE_NEW_PROCESS_GROUP。
不要加 DETACHED_PROCESS：它和 CREATE_NO_WINDOW 互斥，意思是
「不繼承父程序的 console」，而 ComfyUI 是 console 程式，拿不到
console 就會自己配一個新的 —— 畫面上會冒出一個黑窗。

下次開啟會自動接回來：判斷依據是「連接埠有沒有回應」，不是 PID。
PID 會被系統回收給別的程式，而外部啟動的 ComfyUI 本來就不在紀錄裡。
env_state/manager_state.json 記著上次的 PID 和連接埠，接回來之後
「終止」仍然有效。

要停 ComfyUI 請按側邊欄的「終止」—— 關視窗不會停它。


■ 這一版的介面優化
----------------------------------------------------------------------
卡頓的來源全是「在 Tk 主執行緒上開子程序」。Tkinter 的重繪和回呼
排在同一個事件迴圈，所以一個 subprocess.run 就會凍住整個視窗，
包括切分頁。

  開窗卡好幾秒   狀態列的 import torch（2-5 秒）+ git log + 逐一 git
                 掃節點，原本在主執行緒跑 → 改丟背景佇列
  每 3 秒頓一下  掃外部程序要走 WMI/PowerShell，一次 300-800ms
                 → 拆成便宜（主執行緒 1.5 秒）和昂貴（背景，最多
                   10 秒一次；自己在跑 ComfyUI 時完全不掃）
  切節點分頁慢   逐一 git 掃四十個節點 → 背景 + 20 秒快取
  跑任務時變慢   輸出一行一次寫入，每行都 see() 強迫重排
                 → 整批寫入；Text 行數上限 6000
  ComfyUI 吐 log 時卡  每寫一行就 index("end-1c")（O(行數)）
                       → 自己記行數

分頁改成第一次切過去才建內容。


■ 打包 EXE（選用）
----------------------------------------------------------------------
    build_exe.bat                  單檔 + 保留 console
    set COMFY_ONEDIR=1             資料夾版（啟動快，誤判率低）
    set COMFY_NOCONSOLE=1          隱藏 console
    set COMFY_ICON=x.ico           指定圖示

不打包也完全沒問題：manager.bat 直接跑 .py，零誤判、零啟動延遲。


■ 不在這個 zip 裡，你機器上要保留
----------------------------------------------------------------------
build_comfy_kitchen.py   編譯 comfy_kitchen（Sol-Attn 需要）
                         重建 venv 之後要再跑一次，否則會退回
                         wheels/ 裡的舊版


■ 會自動產生的資料夾
----------------------------------------------------------------------
update_logs/   所有任務的紀錄檔
env_state/     節點快照、pip freeze、約束檔、manager 狀態
node_backups/  節點版本備份
comfyui.log    ComfyUI 的輸出
