# ADR 0003：三時間語意與分層儲存

- 狀態：Accepted
- 日期：2026-09-18

## 決策

市場與研究資料明確保存 event time、available time、ingestion time。原始與衍生分析資料採 immutable、content-addressed Parquet snapshot，DuckDB 作查詢層；OMS 與交易狀態先透過 repository abstraction 使用 SQLite，部署後可換 PostgreSQL。

## 理由

三時間語意是防止 look-ahead bias 與重現研究的最低要求。分析資料與交易狀態的交易一致性需求不同，不應強迫使用同一儲存模型。

## 後果

Feature 只能讀取決策時間前已 available 的資料。每次實驗都記錄 snapshot id、設定 hash、程式版本與隨機種子。原始資料不原地覆寫。

