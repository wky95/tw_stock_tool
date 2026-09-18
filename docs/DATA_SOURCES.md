# 資料來源、授權與品質風險

## FinMind（Phase 1 primary）

使用 `TaiwanStockInfo`、`TaiwanStockDelisting`、`TaiwanStockTradingDate` 與 `TaiwanStockPrice`。官方文件：

- <https://finmind.github.io/tutor/TaiwanMarket/Technical/>
- <https://finmind.github.io/tutor/TaiwanMarket/Fundamental/#taiwanstockdelisting>
- <https://finmind.github.io/tutor/TaiwanMarket/DataList/>

Token 只從 `FINMIND_TOKEN` 環境變數讀取，不寫入設定、manifest 或 log。使用者必須自行確認當前方案的請求配額、資料授權與允許用途；FinMind 公開 API 沒有可供實盤依賴的 availability SLA。本系統不授予資料再散布或商業使用權。正式實盤前必須換成或確認具有合適授權與 SLA 的供應商。

## 已知 point-in-time 限制

`TaiwanStockInfo` 偏目前狀態；`TaiwanStockDelisting` 沒有 TWSE／TPEx 市場欄位。因此，僅靠目前 FinMind snapshot 無法可靠重建所有歷史下市股票的市場別。系統仍保存其 master 與 price raw data，但 market 無法證實時標為 `unknown`，每日 universe 以 `unknown_market` fail-closed 排除，品質報告產生 `UNKNOWN_HISTORICAL_MARKET`。

這避免把未知資料猜成某個市場，但仍可能低估歷史 eligible universe。正式大規模因子結論前，必須補入具 point-in-time market/listing history 的授權 reference dataset；不能使用今日上市清單回填。

Listing date 第一版由可取得的最早日價推導。若 provider 的價量歷史起點晚於實際上市日，minimum listing days 會偏保守，並在缺失檢查中呈現。

Instrument master 同時保存 `listing_date_source`、`listing_date_quality` 與 `observed_at`。由最早價格推導時一律標為 `first_observed_price`／`provisional`，不宣稱是官方上市日。只要研究期間存在 unknown market 或 provisional listing date，`universe_metadata.is_research_complete` 就是 false；未明確指定 research override 的 consumer 必須拒絕，override 報告則必須顯示：

`INCOMPLETE POINT-IN-TIME UNIVERSE — RESULTS NOT VALID FOR PRODUCTION CONCLUSIONS`

FinMind 文件說明：沒有公告成交價時 OHLC 可能同為零。系統將其記為 `NO_PUBLISHED_PRICE` warning 並視為停牌／無可成交價，不會用前值偷偷補成可交易資料。

## 官方抽樣交叉驗證

2026-09-18 執行 2024-01-02 至 2024-01-05 樣本：

- TWSE 2330：4 個共同交易日的 OHLC 與成交股數完全一致。
- TPEx 6488：4 個共同交易日的 OHLC 完全一致；FinMind 精確股數與 TPEx 舊日表的千股取整值相差 118–551 股，均小於 1,000 股。

Canonical volume unit 固定為 `shares`。Normalized row 同時保存 `source_volume` 與 `source_volume_unit`；單位轉換只發生在 adapter/cross-validation boundary。抽查結果保存轉換後的 absolute difference（shares）及 relative difference，不比較未標示單位的裸數字。

可重跑測試位於 `tests/test_phase1_official_crosscheck.py`，標記為 `network`，只在設定 `RUN_NETWORK_TESTS=1` 時執行。它是品質抽樣，不是第二個 production provider。
