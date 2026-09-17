# Phase 0 現況、假設與驗收

## Repository 盤點

既有 `twbacktest/` 是無第三方依賴的台股工具，具備日 K provider、單標的回測、盤中快照、因子評估及 20 項單元測試。它沒有 git metadata，因此無法取得 commit、分支或未提交修改；本輪保留所有既有程式與快取，另建 `src/island_quant/`。

盤點時的主要缺口：沒有正式 package/config schema、point-in-time 資料契約、可重播事件 envelope、完整交易 domain vocabulary、結構化 logging、lint/type-check/pytest CI、ADR，以及可追溯的資料與 artifact 版本制度。本 Phase 已補齊骨架層項目；資料／artifact 版本的實際儲存會在 Phase 1 完成。既有回測不是完整 OMS/paper/live 基礎，不能視為券商整合。

## 第一版假設

- 市場：TWSE／TPEx 股票與 ETF；TWD；Asia/Taipei。
- 頻率：日頻；收盤後產生訊號，最早下一交易時段成交。
- 方向與資金：只做多、初始資金 TWD 1,000,000、無槓桿、單一標的上限 10%。
- 資料：Phase 1 先使用一個可重現的官方或公開日 K adapter；原始資料 immutable。
- 執行：先 research、backtest、paper；尚未指定券商，不建立假的 live adapter。
- 部署：本機／Docker 單機；分析資料 Parquet + DuckDB，交易狀態 SQLite repository abstraction。

這些是可透過設定更改的起始值，不代表已批准 live trading。

## 核心事件流

```text
immutable raw data
  -> validated Bar(event/available/ingestion time)
  -> versioned Feature -> Label / Forecast
  -> Strategy -> TargetPosition
  -> Portfolio -> OrderIntent
  -> independent RiskDecision
  -> Execution -> OMS Order -> Broker/Paper fill
  -> Position + PortfolioSnapshot
  -> reconciliation + reports + monitoring
```

每一步寫入帶有 event id、correlation id、causation id、aggregate sequence 與 schema version 的 journal。Backtest 重播同一事件；paper/live 更換 data/broker adapter，不改策略邏輯。狀態不確定時，risk/OMS 禁止新增曝險。

## 技術棧

- Python 3.12+、Pydantic v2 / pydantic-settings、YAML。
- Phase 1：Polars、PyArrow/Parquet、DuckDB、scikit-learn。
- Phase 2/3：SQLite repository abstraction，部署需要時換 PostgreSQL；FastAPI 在有 use case 後加入。
- pytest、Ruff、mypy、GitHub Actions、Docker。

## Phase 0 驗收

- `pip install -e '.[dev]'` 可安裝。
- `island-quant validate-config` 可載入範例設定，未知 key 會失敗。
- live 設定沒有三重 gate 時會失敗。
- domain model 驗證 timezone、point-in-time ordering、OHLC 與 order invariants。
- pytest、Ruff、mypy 全部通過。

## 非阻塞但 Phase 1 前宜確認

1. 第一個研究 universe 是固定股票清單，還是要 point-in-time 上市／下市全市場？
2. 歷史資料優先使用哪個授權來源？公開來源可能無法完整處理下市、修訂與 corporate actions。
3. 訊號時間固定收盤後、下一日開盤執行是否符合預期？
4. Phase 1 是否只做多、整股（1,000 股），並以加權指數作 benchmark？
5. 實驗 artifact 要只留本機，還是需要 S3/MinIO 類 object storage？
6. 目標部署是單機 macOS/Linux，或 NAS/VPS？
7. 預計 Phase 3/4 使用哪一家台灣券商？這會影響 OMS 的委託語意，但不阻塞 Phase 1。
