# 影片下載與後製自動化工具

一支用 Python 標準函式庫 Tkinter 寫的桌面工具，把「下載影片 → 依設定做畫面後製」這段重複流程自動化。

這是我練習 **GUI 非同步處理**、**外部行程協作（subprocess）** 與 **跨平台工具鏈偵測** 的專案。功能本身不複雜，但我刻意把每個「看起來能動」的地方都實際驗證過一次，過程中踩到的坑比預期多，也記在下面。

---

## 這個專案在做什麼

| 功能 | 說明 |
| --- | --- |
| 批次下載 | 每行一個網址，支援單片與播放清單 |
| 自訂檔名 | yt-dlp 樣板，可用標題／頻道／日期／流水號等欄位 |
| 輸出格式 | MP4 / MKV / WebM 可選 |
| 網址管理 | 刪除選取行 (Ctrl+D)、一鍵清空、去除重複 |
| 最高畫質 | 自動挑選最佳影音分軌並用 FFmpeg 合併 |
| 邊緣裁切 | 指定上／下／左／右要裁掉的像素 |
| 區域遮罩 | 指定矩形區域做模糊 (boxblur) 或插補 (delogo) |
| 直式轉換 | 轉成 9:16 / 4:5 / 1:1，可選裁切兩側、模糊背景或黑邊 |
| 硬體加速 | NVENC / QSV / AMF 可選，失敗自動退回 CPU |
| 即時進度 | 目前步驟與整批加權進度雙進度條，可隨時中止 |
| 記住設定 | 選項保存於 `settings.json`，下次開啟自動還原 |

處理後的檔案預設另存 `原檔名_edited.<原容器>`，原始檔保留。後製一定會重新編碼成 H.264，而 WebM 裝不下 H.264，所以來源是 WebM 時輸出會改成 MP4。

網址是直接交給 yt-dlp 處理的，程式本身**沒有任何網域判斷**，所以 yt-dlp 支援的站台（目前版本內建 1750 個 extractor）理論上都能用。我實際測過的是 YouTube 與 Bilibili，其餘未逐一驗證。

---

## 我在這個專案裡處理的五個問題

### 1. 怎麼讓 GUI 在跑重任務時不凍結

Tkinter 是單執行緒事件迴圈，下載或編碼一旦在主執行緒跑，整個視窗會卡死到結束。

我的做法是**背景執行緒完全不碰任何 widget**——它只能往 `queue.Queue` 丟訊息，主執行緒用 `after(100, ...)` 定期取出來更新畫面：

```python
def _emit(self, kind: str, *payload) -> None:
    """由工作執行緒呼叫，只丟訊息不碰 widget。"""
    self.events.put((kind, payload))
```

一開始我以為「小心一點、只更新一個 Label 應該沒關係」，查了才知道 Tk 不是 thread-safe，跨執行緒操作是**未定義行為**——可能正常跑幾百次，然後某次隨機崩潰。這種 bug 事後很難查，用架構擋掉比用小心擋掉可靠。

中止機制用 `threading.Event`：下載端在 yt-dlp 的 progress hook 裡檢查並拋自訂例外，FFmpeg 端在讀 stdout 的迴圈裡檢查並 `terminate()` 子行程。

### 2. 怎麼避免「處理到一半失敗，檔案毀了」

最初版本我直接讓 FFmpeg 輸出覆蓋原檔，跑到一半按停止就發現**原檔沒了、新檔也不完整**。

改成先寫暫存檔，確認 return code 為 0 才置換：

```python
tmp = src.with_name(f"{src.stem}.processing{suffix}")
...
finally:
    if proc.poll() is None:
        proc.terminate()
    tmp.unlink(missing_ok=True)   # 失敗或中止都不留半成品
```

同樣的思路也用在批次處理上：單一檔案失敗只記錄錯誤並繼續下一個，不會讓一支壞影片中斷整批。這是我第一次認真思考「**失敗時系統要停在哪個狀態**」，而不只是讓它成功。

### 3. 直式裁切：把判斷交給 FFmpeg 而不是自己探測

需求是「橫式影片轉成直式，但**本來就是直式的不要動**」。

直覺做法是先跑 `ffprobe` 取解析度，再用 Python 分支決定要不要裁。但這樣每支影片要多開一次行程，而且批次處理時每個檔都得判斷一次。

後來發現 FFmpeg 的濾鏡參數本身支援運算式，可以把判斷寫進去，執行期依實際畫面決定：

```
crop=trunc(if(gt(iw\,ih*0.5625)\,ih*0.5625\,iw)/2)*2:ih:(in_w-out_w)/2:0
```

`iw`（輸入寬）、`ih`（輸入高）在濾鏡執行時才代入，所以同一條指令對任何解析度都成立：

| 來源 | 目標 9:16 的結果 |
| --- | --- |
| 1920x1080 橫式 | 606x1080（裁掉左右） |
| 1080x1920 直式 | 原樣輸出 |
| 1080x2400 超長直式 | 原樣輸出 |
| 1080x1080 正方 | 606x1080（正方不算直式） |

外層的 `trunc(.../2)*2` 是另一個坑：H.264 要求寬高為偶數，我第一次跑 1080-45=1035 直接編碼失敗，才知道要向下取偶。

只裁寬度、不裁高度是刻意的決定——超長直式影片如果為了湊比例砍頭尾，會失去內容。

去水印與直式轉換是兩個獨立開關，濾鏡以鏈式組裝，可疊加：

```
[0:v]crop=...[s0];[s0]crop=...[v]
```

### 4. 「支援」與「跑得動」是兩回事

加硬體加速時我先用 `ffmpeg -encoders` 偵測有哪些可用，本機列出了 NVENC，於是預設就挑它。實際跑下去才發現：

```
Driver does not support the required nvenc API version. Required: 13.1 Found: 11.1
```

FFmpeg 把 NVENC **編譯進去了**，但這台機器的顯卡驅動版本太舊。偵測結果是「理論支援」，不是「實際可用」。

所以改成兩層：`available_encoders()` 先問 FFmpeg 有哪些，真正編碼失敗時再退回 CPU 重跑，並把該編碼器記進 `_broken_encoders`，同一批後續檔案不再浪費一次嘗試。

```python
attempts = [encoder] if encoder == "cpu" else [encoder, "cpu"]
```

這件事讓我學到：**能力偵測要以實際執行結果為準**，靜態查詢只能當作第一層篩選。如果我只在自己機器上跑一次成功就收工，這個 bug 會留給每一個驅動版本不同的使用者。

### 5. 環境問題比我想像的多

這部分沒什麼演算法，但佔掉我不少時間，也是我覺得學到最多的地方。

**winget 裝完卻找不到執行檔。** 它只改登錄檔的 PATH，既有的終端機不會生效。使用者裝完直接執行一定會中。我加了 `ensure_tools_on_path()`，啟動時把 winget 安裝位置與專案 `bin/` 補進本行程的 PATH，不必重開終端機。

**「最高畫質」不只是選對 format 字串。** yt-dlp 跑起來警告找不到 JavaScript runtime，查了才知道 YouTube 有簽章挑戰，沒有 JS 引擎就解不開、部分高畫質格式會直接缺失。裝了 Deno 之後才真的抓得到 4K。這件事如果沒實測、只看下載成功就收工，我會以為功能是好的。

**套件是綁在直譯器上的。** 我在 Python 3.11 裝好套件，換成 uv 管理的 3.14 執行就報缺套件。想直接補裝又被 PEP 668 擋下——uv 的 Python 本體受保護。正解是建專案 venv，而不是用 `--break-system-packages` 硬闖（那會弄髒所有共用該直譯器的專案）。

順手把錯誤訊息改成會印出當下的直譯器與專案 venv 路徑，讓下次遇到的人不用猜：

```
缺少必要套件：
  - yt-dlp（執行 pip install -r requirements.txt）

目前的直譯器：C:\...\uv\python\cpython-3.14.5-...\python.exe
本專案的 venv：...\videoproj\.venv\Scripts\python.exe
請改用上面這個直譯器執行。
```

---

## 驗證方式

### 單元測試

濾鏡組裝的三個函式（`build_filter` / `validate` / `build_video_args`）是純函式——輸入設定、輸出字串，不碰檔案也不開行程，所以能在沒有 FFmpeg 的環境下測：

```bat
.venv\Scripts\python.exe -m pytest tests -q
30 passed
```

測試重點放在**不變條件**而不是逐行覆蓋。例如 `process()` 固定用 `-map [v]`，所以任何設定組合產生的濾鏡圖都必須以 `[v]` 結尾——這條壞掉整支程式就失效，因此獨立成一個測試：

```python
def test_every_filter_graph_ends_with_v_label():
    for s in combos:
        assert build_filter(s).endswith("[v]"), s
```

其餘涵蓋邊界條件：全零裁切、負值、零面積區域、delogo 貼齊邊緣、未知比例的退回行為、品質值的範圍夾制。

### 手動實測

會碰到實際檔案與外部行程的部分沒辦法只靠單元測試，逐項跑過並記錄實際數字：

| 項目 | 結果 |
| --- | --- |
| YouTube 最高畫質挑選 | 4K 來源挑到 AV1 3840x2160 + AAC 音軌 |
| YouTube 下載合併 | 分軌下載 → FFmpeg 合併 → 檔名沿用標題 |
| Bilibili 下載合併 | 1080x1920 AV1 + AAC，中文檔名正常 |
| 邊緣裁切 | 1920x1080 裁頂 80 → 1920x1000；四邊裁切 → 1900x954（自動取偶） |
| 區域遮罩 | 尺寸不變，抽格確認指定區域確實模糊，音軌保留 |
| 直式 9:16 | 橫式→606x1080；直式與超長直式原樣通過；正方→606x1080 |
| 直式 1:1 | 1920x1080 → 1080x1080 |
| 濾鏡疊加 | 裁頂 80 + 直式 → 562x1000；遮罩 + 直式 → 606x1080 |
| 取景位置 | 靠左／置中／靠右 產出三種不同 hash、相同尺寸的畫面 |
| 去標誌 (delogo) | 尺寸與音軌不變，指定區域由周圍像素插補 |
| 硬體加速退回 | 本機 NVENC 因驅動過舊失敗 → 自動改用 CPU 完成 |
| 中止 | 後製階段拋出例外、清除暫存、原始檔完整 |
| 跨版本 | Python 3.11.9 與 3.14.5 皆通過 |

「取景位置」那項我一開始只確認輸出尺寸相同就算過，後來想到**尺寸相同不代表取景真的有變**，才改成比對畫格的 hash——三個位置產出三個不同 hash，這樣才算真的驗過。

---

## 已知問題與待改進

### 已知 Bug

- **中止在下載階段不會立即生效。** 我實測驗證過：progress hook 拋的是自訂的 `Cancelled`，但 `ignoreerrors=True` 讓 yt-dlp 內部的 `except Exception` 把它吃掉，函式正常返回空清單。停止仍會生效，但要等到 `_run_job` 迴圈頂端才真正停下，中間會噴出多餘的錯誤日誌。正解是改拋 `yt_dlp.utils.DownloadCancelled`——那是唯一會被 yt-dlp 重新拋出、繞過 `ignoreerrors` 的例外。後製階段的中止則正常。

### 待改進

- **`_run_ffmpeg` 的 stderr 讀取時機有死鎖風險。** 目前在迴圈讀 stdout、等 `wait()` 之後才讀 stderr，若 stderr 寫滿 pipe buffer 就會互相等待。因為 `-loglevel error` 讓輸出量很小所以還沒發生，但正確作法是 `communicate()` 或另開執行緒。
- **中止依賴 FFmpeg 持續輸出進度。** `for line in proc.stdout` 是阻塞的，FFmpeg 若卡住不吐資料，取消檢查就不會執行，目前也沒有 timeout。
- **遮罩區域沒有對照實際解析度驗證**，只檢查大於 0。填了超出畫面的座標要等 FFmpeg 執行才失敗。
- **`gui.py` 有 676 行，`App` 什麼都做**——建 UI、管執行緒、驗證、序列化設定、跑批次任務。導致 `_run_job` 無法單獨測試。下一步想把它抽成獨立的 job runner。
- **測試只覆蓋 `processor` 的純函式**，`downloader` 與 `gui` 尚無測試，也還沒導入 CI。
- **Chrome / Edge 新版的 App-Bound Encryption** 會讓 cookie 讀取失敗，目前只能建議改用 Firefox。
- **沒有打包成執行檔**，使用者需要自己準備 Python 環境。

---

## 執行方式

需要 Windows 端的 Python（WSL 內沒有 tkinter，且瀏覽器 cookie 讀不到）。

```bat
uv venv --python 3.14
uv pip install -r requirements.txt
.venv\Scripts\python.exe main.py
```

跑測試：

```bat
uv pip install pytest
.venv\Scripts\python.exe -m pytest tests -q
```

外部工具：

```bat
winget install --id Gyan.FFmpeg -e     # 影音合併與濾鏡
winget install --id DenoLand.Deno -e   # yt-dlp 解 YouTube 簽章挑戰用
```

也可以直接把 `ffmpeg.exe` / `ffprobe.exe` 放進專案的 `bin\`，程式會自動找到。

開發環境：Python 3.14.5、yt-dlp 2026.7.4、FFmpeg 8.1.2、Deno 2.9.4。

---

## 專案結構

```
main.py               進入點：相依檢查與 PATH 修正
gui.py                Tkinter 介面、執行緒調度、事件佇列
downloader.py         yt-dlp 封裝：格式挑選、合併、cookie、進度 hook
processor.py          FFmpeg 濾鏡鏈組裝、編碼器選擇與進度解析
config.py             設定資料結構、工具路徑搜尋、設定保存
tests/                單元測試
```

`downloader.py`、`processor.py`、`config.py` 完全不 import tkinter，介面相關的東西只存在於 `gui.py`（`main.py` 只在啟動檢查時確認 tkinter 裝了沒）。處理邏輯與介面分離，之後想改成 CLI 或換 GUI 框架時不必動到核心。

---

## 使用聲明

本工具供個人備份與自有內容處理使用。下載或再利用他人作品前，請確認符合平台服務條款與著作權相關規範。
