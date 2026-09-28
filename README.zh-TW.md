# cchub

[English](README.md) · **繁體中文**

用手機操作 Linux 電腦上的 Claude Code：開新專案資料夾、打開任一專案、在裡面開 session。全部在官方 Claude App 裡完成，不用再遠端桌面回電腦。

cchub 是包在 Claude Code 官方 **Remote Control** 伺服器模式（`claude remote-control`）外面的一層薄殼。它不做自己的協定、不做 App、也不做聊天介面，手機上看到的全是官方 Claude App。它只補上 Remote Control 自己做不到的兩件事：

1. **在指定資料夾把伺服器起起來，並持續顧好它**：用 systemd 管理，crash 或斷網後自動恢復，Claude Code 更新後自動換版。
2. **從手機建立新的專案資料夾**：寫入含你需求的 `CLAUDE.md`、`git init`、寫入工作區信任。你只要在手機上確認一次。

> **非官方專案。** cchub 與 Anthropic 沒有任何關係，也未經其認可。它依賴撰寫當時（2026 年 9 月）Claude Code 2.1.28x 的行為，日後的 Claude Code 更新可能讓它失效。CLI 訊息與內附的 skill 都是繁體中文。

## 為什麼需要它

Remote Control 可以讓你用手機接續電腦上的 session，但有這些限制：

- **伺服器得先在電腦上起起來。** 資料夾裡沒有伺服器在跑，手機就沒辦法在那台電腦上開 session。
- **手機的裝置選單裡，一個伺服器就是一筆。** 它不能往下選子資料夾，所以每個專案都需要自己的伺服器。
- **新資料夾要先取得工作區信任才能起伺服器。** git repo 自成一個信任邊界，上層資料夾的信任涵蓋不到它。
- **伺服器離線約 10 分鐘就會自己結束。** 需要有東西把它拉回來。

cchub 補的就是這些缺口。

## 運作方式

```
手機：Claude App → Code → 你的電腦
 ├─「projects」＝入口伺服器（你的專案根目錄；常駐、開機自動起）
 │    └─ 你：「開新專案 ledger，做一個記帳小工具」
 │         └─ Claude（透過 cchub skill）執行 `cchub new ledger --brief-stdin`
 │              ├─ 手機跳一次確認
 │              ├─ 建 <專案根目錄>/ledger（CLAUDE.md 寫入需求、git init）
 │              ├─ 只替這個剛建立的資料夾寫信任
 │              └─ systemctl --user start cchub-rc@<ledger>
 └─「ledger」＝專案伺服器
      └─ Directory 選 ledger → 按「+ New session」→「照 CLAUDE.md 的需求做」

cchub-reconcile.timer（每 5 分鐘）：換上新版 Claude Code、記錄永久性錯誤、確保入口在跑
```

## 需求

| 項目 | 怎麼確認 |
|---|---|
| 有 systemd user 服務的 Linux，並開啟 linger，這樣開機後不用登入入口也會起來 | `loginctl show-user $USER -p Linger` |
| Python 3（只用標準函式庫，在 3.12 上測試）與 git | `python3 --version` |
| Claude Code CLI **2.1.281 以上**，並用 claude.ai 訂閱帳號登入。API key 與 `claude setup-token` 的 token 都不能用 Remote Control | `claude auth status` |
| 已回答 Remote Control 的一次性同意：在終端機執行一次 `claude remote-control`，回答 `y` | `cchub doctor` |
| 專案根目錄已受信任：在那裡執行一次 `claude`，接受信任對話框 | `cchub doctor` |
| `~/.local/bin` 在 `PATH` 上 | `echo $PATH` |
| claude.ai 帳號有防護（強密碼＋兩步驟驗證），手機有螢幕鎖 | 見[安全](#安全) |

## 安裝

```bash
git clone https://github.com/YeeWay0129/cchub.git
cd cchub

# 1. 先看每一步會做什麼（完全不改動系統）
bin/cchub install --dry-run --projects-root ~/code

# 2. 安裝：在你自己的終端機執行，會要你輸入 yes
bin/cchub install --projects-root ~/code
```

第一次安裝一定要帶 `--projects-root`：新專案會建在這裡，入口伺服器也在這裡跑。另外兩個選項：

- `--allowed-root <目錄>`：可重複，加入其他可以打開的資料夾範圍。
- `--entry-dir <目錄>`：把入口放在別處。

在 Claude Code session 裡、或沒有 TTY 時，真的 install／uninstall 會被拒絕；`--dry-run` 則隨時可以跑。

`install` 會做這些事，全部都能用 `cchub uninstall` 還原：

1. 把 `bin/`、`cchub/`、`templates/` 複製到 `~/.local/share/cchub/`，讓入口 session 改不到它自己的工具。
2. 建立 `~/.local/bin/cchub`，指向這份複本。
3. 寫入 `~/.config/systemd/user/cchub-rc@.service`、`cchub-reconcile.service`、`cchub-reconcile.timer`，再執行 `daemon-reload`。
4. 寫入 skill `~/.claude/skills/cchub/SKILL.md`；如果已經有同名但內容不同的檔案，先備份。
5. 在 `~/.claude/settings.json` 加 6 條 `permissions.ask` 規則（見下方）；會先備份，其他設定完全不動。
6. 寫入 `~/.config/cchub/config.json`（不存在才寫），再啟用並啟動入口伺服器與 reconcile 計時器。
7. 執行 `cchub doctor`。

## 手機上怎麼用

在 Claude App 進入 **Code** → 你的電腦，畫面上會有 **Directory** 選單、目前的 session 列表，以及 **+ New session** 按鈕。

- **開新專案**：Directory 選入口（你的專案根目錄），開一個 session 或用現有的，說「開新專案 ledger，做一個記帳小工具」。按下那一次確認。收到「✅ ledger 已上線」後，在 Directory 選 **ledger** → 按 **+ New session** → 說「照 CLAUDE.md 的需求做」。
- **打開既有專案**：在入口說「打開 my-app」，然後在 Directory 選它、開新 session。
- **同一個專案再開一個 session**：在 Directory 選它 → 按 **+ New session**。
- **看成果**：請 Claude 把成果發成 Artifact，點連結就能看。
- **查看或停止伺服器**：說「現在開著哪些？」（執行 `cchub ls`）；說「關掉 ledger」會先跳確認。

`new`／`open` 的回覆最後會附上伺服器的**環境網址**（`https://claude.ai/code?environment=…`），當作備用入口。cchub 不記錄 session 網址，也不記錄任何可能含有對話內容的東西。

## 指令

| 指令 | 作用 | 需要確認 |
|---|---|---|
| `cchub ls [--json]` | 列出受管伺服器：資料夾、狀態、上線時間、警告 | – |
| `cchub open <名稱或路徑> [--mode M]` | 替已受信任的既有資料夾起伺服器 | – |
| `cchub logs <名稱> [-n N]` | 看過濾後的狀態紀錄，不含任何對話內容 | – |
| `cchub doctor` | 檢查前置條件與防護狀態，列出未受信任的專案 | – |
| `cchub new <名稱> [--title T] [--brief-stdin] [--mode M] [--no-git]` | 建新專案（CLAUDE.md、git、信任）並起伺服器 | **要** |
| `cchub stop <名稱>`／`cchub restart <名稱>` | 停止或重啟伺服器；入口不能停 | **要** |
| `cchub install`／`cchub uninstall` | 安裝或移除；兩者都有 `--dry-run` | **要** |

幾條值得知道的規則：

- **名稱**：可以是允許範圍內的資料夾名稱，或絕對路徑。有兩個以上同名資料夾時，會請你改用完整路徑。
- **新專案**：名稱必須符合 `^[a-z0-9][a-z0-9-]{0,39}$`，而且只會建在專案根目錄底下。
- **權限模式**：只接受 `default`、`acceptEdits`、`auto`、`plan`、`dontAsk`，**`bypassPermissions` 一律拒絕**。預設是 `auto`；如果你的方案沒有 auto 模式，請修改 `~/.config/cchub/config.json` 的 `default_mode` 與 `entry_mode`。
- **上限**：專案伺服器最多同時 6 個（入口不算）。
- **需求原文**：只用加了引號的 heredoc 從 stdin 傳入，原文裡的 `$`、反引號、引號都不會被 shell 展開。

## 安全

安裝前請先讀完這一節。

- **cchub 能做的事**：
  - 替 Claude Code 已經信任的資料夾起伺服器。
  - 建一個空專案，並只信任**那個資料夾**。
  - 停止伺服器。
- **cchub 做不到的事**：它**完全沒有信任既有資料夾的能力**。只有同一次 `cchub new` 剛建立的資料夾會被寫入信任，而且寫入前會先確認裡面只有模板檔案。既有資料夾如果未受信任，請你自己用 Claude Code 的官方對話框處理：在那個資料夾執行 `claude`。
- **確認是使用體驗層，不是安全邊界。** 安裝時會加入這幾條使用者層級的 ask 規則：

  ```
  Bash(cchub new *)
  Bash(cchub stop *)
  Bash(cchub restart *)
  Bash(cchub install*)
  Bash(cchub uninstall*)
  Bash(cchub _*)
  ```

  它們讓手機跳出確認，連 auto 模式下也一樣。但規則只比對字面，改用絕對路徑或 `bash -c` 呼叫就不會觸發。所以安全性建立在 cchub **能做什麼**，而不是這些確認。
- **暴露面會擴大。** 入口伺服器 24 小時常駐，預設是 `auto` 模式。任何人只要進得了你的 claude.ai 帳號，或拿到你沒上鎖的手機，隨時都能在你的電腦上執行程式，不再限於你開著 session 的時候。請使用強密碼、兩步驟驗證與手機螢幕鎖，也可以考慮把 `entry_mode` 設得更嚴格。
- **入口 session 不是沙盒。** 它沿用你使用者層級的 Claude Code 設定與允許規則。在 auto 模式下，它可以修改專案根目錄底下任何專案的檔案，由 Claude Code 自己的分類器把關。
- **log 不保留對話內容。** cchub 執行 `claude remote-control` 時不加 `--verbose`，輸出也只保留白名單內的行：連線狀態、環境網址、少數幾種完全吻合的錯誤訊息。錯誤判定只看 stderr。session 標題與工具活動是由對話產生的，可以偽裝成任何東西，所以一律丟棄。

## 仍然需要回到電腦的情況

| 情況 | cchub 的反應 | 你要做的 |
|---|---|---|
| 既有資料夾未受信任 | `open` 拒絕 | 在那裡執行一次 `claude`，接受信任對話框 |
| 資料夾的信任繼承自上層，但裡面有自己的 hooks、MCP 伺服器或權限設定 | `open` 拒絕 | 同上，讓你親自審閱那些設定 |
| Claude Code 登入過期或已登出 | 伺服器停下，`cchub ls` 顯示原因 | 執行 `claude auth login` |
| Remote Control 的一次性同意還沒回答 | 持續重試，`cchub ls` 顯示原因 | 執行一次 `claude remote-control` 並回答 `y` |
| 電腦關機 | 無法處理 | 開機 |

斷網、睡眠，以及 Claude Code 更新刪掉執行中的版本，都會自動恢復。

## 移除

```bash
cchub uninstall --dry-run   # 先看會移除什麼
cchub uninstall             # 在你自己的終端機執行
```

uninstall 會做這些事：

- 停用並移除單元與計時器、skill、`~/.local/bin/cchub`、`~/.local/share/cchub`。安裝後被改動過的檔案會保留，並提出警告。
- 還原 cchub 加過的信任。
- 只移除 cchub 加的 ask 規則，移除前會先備份 `settings.json`。

**專案資料夾一律不會被刪除。**

## 開發

```bash
python3 -m unittest discover -s tests
```

測試使用暫存目錄與假的 systemd／proc 函式，不會碰到你真正的 `~/.claude.json` 或 systemd。

## 授權

[MIT](LICENSE)
