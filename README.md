# Serenity feed：股價（yfinance）＋ 最新貼文（X API，可選）

呢個 repo 每小時喺 GitHub Actions 自動運行，產生 `feed/feed.json`，畀你嘅 Serenity Stock Tracker dashboard 讀取：

- **股價**：用 Yahoo Finance（`yfinance`）下載每日收市價，免費。美股收市後（約 21:00 UTC 後）每日更新一次。
- **最新貼文（可選）**：如果你設定咗 X API 金鑰，每小時抓取 @aleabitoreddit 嘅新貼文，令 dashboard 比 GitHub 存檔（每日約更新兩次）更即時。
- 歷史貼文由 yan-labs 存檔提供，呢度唔會重複下載，所以 X API 只需為新貼文付費。

## 設定步驟

1. **開一個新 repo**：GitHub 右上角「+」→ New repository，名例如 `serenity-feed`，揀 **Public**（public repo 嘅 Actions 免費，而 dashboard 要直接讀 raw 檔案）。
2. **上載檔案**：解壓 zip，喺新 repo 撳「uploading an existing file」，將全部檔案及資料夾拖入去，然後 Commit。
   - ⚠️ `.github` 係隱藏資料夾，Mac 嘅 Finder 預設睇唔到（按 `Cmd + Shift + .` 顯示）。如果上載後 repo 入面冇 `.github/workflows/update-feed.yml`，就用「Add file → Create new file」，檔名打 `.github/workflows/update-feed.yml`，再將 zip 入面同名檔案嘅內容貼入去。
3. **第一次運行**：去 repo 嘅「Actions」分頁 →（如有提示，撳啟用）→ 左邊揀「Update Serenity feed」→「Run workflow」→ 剔「Re-download all prices now」→ Run。大約 3–5 分鐘完成，完成後 repo 會多咗 `feed/feed.json`。
4. **接駁 dashboard**：打開 dashboard →「設定」→「數據源網址」貼上：
   `https://raw.githubusercontent.com/<你的GitHub帳戶>/serenity-feed/main/feed/feed.json`
   儲存後，各股票嘅股價圖及升幅就會出現。

之後每小時自動運行，唔使再理。

## （可選）加入 X API 取得最新貼文

1. 去 X Developer Console 開一個 App，購買 credits，**設定消費上限**（建議每月 US$10）。
2. 複製 App 嘅 **Bearer Token**。
3. 返去你嘅 repo：Settings → Secrets and variables → Actions → New repository secret
   - Name：`X_BEARER_TOKEN`
   - Secret：貼上 Bearer Token
4. 完成。下一次每小時運行就會開始抓新貼文。

**費用控制**：腳本用 `since_id` 只抓上次之後嘅新貼文，第一次只抓最近 2 日（歷史由存檔提供），亦唔會讀取用戶資料（每次 US$0.010），所以費用只取決於 Serenity 發咗幾多新貼文，同運行頻率無關。

## 代號對照（ticker_map.json）

Serenity 用嘅 $代號唔一定等於 Yahoo 代號，特別係海外上市公司（例如 SIVE → `SIVE.ST`、SOI → `SOI.PA`）。
- 每次運行後，`feed/state.json` 入面嘅 `missing` 會列出搵唔到股價嘅代號。
- 喺 Yahoo Finance 搜尋正確代號，加入 `ticker_map.json`；唔係上市公司（例如私人公司）就設為 `null`。
- 改完後喺 Actions 用「Re-download all prices now」再跑一次。

## 常見問題

- **Actions 停咗？** GitHub 會喺 repo 長時間冇活動時停用排程；呢個 workflow 每日都會 commit 股價，一般唔會出現。如果停咗，去 Actions 分頁重新啟用即可。
- **股價有一日冇更新？** `yfinance` 唔係 Yahoo 官方 API，偶然會失靈；腳本會保留上次嘅數據，下次運行再試。
- **只做個人研究用途**，Yahoo 數據請勿轉售或公開再發佈。
