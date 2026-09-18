# Phase 1 計畫與已確認決策

## 不再開放的架構選項

- Universe：每日 point-in-time TWSE／TPEx 普通股；保留下市股票，排除 ETF、ETN、權證、DR、特別股。
- Primary provider：FinMind。Domain 與 normalized schema 不接受 FinMind-specific type。
- 時間：Asia/Taipei；保存 `event_time`、`available_at`、`ingested_at`；決策只讀 `available_at <= decision_time`。
- 訊號／label：收盤後決策，label 從下一交易日可成交價格開始；不假設精確開盤完全成交。
- Portfolio：long-only、cash account、無槓桿／融資／融券／當沖、整數股、允許零股。
- Benchmark：TWSE/TAIEX、TPEx/TPEx index、point-in-time eligible-universe 市值加權基準分開報告。
- Storage：本機 content-addressed raw、versioned Parquet、DuckDB catalog、manifest/checksum；以 `ArtifactStore` 保留未來 S3/MinIO 接縫。
- Deployment：macOS Apple Silicon、Python 3.12+ 與 Docker；Phase 1 不部署 VPS/NAS/object storage。
- Broker：Phase 1 不串接；Phase 3 才重新驗證 Shioaji API、帳戶資格與 macOS arm64 相容性。

所有路徑由 `config/default.yaml` 管理，不使用 home directory 或硬編碼絕對路徑。

## Slice 1：Point-in-time price data foundation（本輪）

- `data/ports.py`：provider-neutral request/payload/provider protocol。
- `data/adapters/finmind.py`：單一 production provider、timeout、retry。
- `data/adapters/fixture.py`：完全離線且固定時間的測試 provider。
- `data/schema.py`：instrument、listing/delisting、calendar、daily OHLCV normalized schemas。
- `storage/ports.py` / `storage/local.py`：`ArtifactStore`、immutable raw、Parquet snapshot、manifest、DuckDB views、checkpoint。
- `data/validation.py`：primary key、價格／成交量、OHLC、缺交易日、unknown historical market 品質報告。
- `data/universe.py`：具版本的 `UniversePolicy`、每日 membership、完整 exclusion reasons。
- `universe_metadata`：reference version、coverage、未知市場／暫定上市日計數、排除原因統計與 `is_research_complete` gate。
- `data/ingestion.py`：incremental merge、idempotency、checkpoint/resume 與 dataset lineage。
- `cli.py`：`ingest-data`、`validate-data`、fixture、dry-run 與明確 exit code。

驗收 fixture 包含未上市、下市、ETF、DR、特別股、缺交易日及跨 TWSE/TPEx 資料。網路測試全部標記 `network` 且預設跳過。

Canonical volume unit 為股數（`shares`）；provider raw value/unit 保留。最早價格推導的 listing date 一律標為 provisional，未經顯式 override 不可當成 production-quality universe。

## Slice 2：Features、labels 與 leakage guards（建議下一輪）

- Versioned feature registry 與 momentum/reversal/volatility/liquidity baseline。
- 下一交易日可成交價開始的 forward-return labels。
- available-at filter、財報公告時間 contract、no-lookahead tests。
- Cross-sectional transform 僅在當日 eligible universe 內計算。

## Slice 3：Walk-forward research 與 baseline model

- Expanding/rolling split、purge/embargo、untouched OOS test。
- Pearson/Spearman IC、ICIR、decay、quantile、turnover、coverage、成本後效果。
- sklearn Ridge baseline；imputer/scaler/model 每 fold 僅 fit train。

## Slice 4：Experiment tracking 與 OOS report

- 成功與失敗 run manifest、data/config/code hash、seed、期間與 artifact lineage。
- 分開的 TAIEX、TPEx index、eligible-universe benchmark。
- 單一 CLI 從 raw snapshot 重現 OOS report。
