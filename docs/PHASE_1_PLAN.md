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

## Slice 1：Point-in-time price data foundation（已提交）

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

## Slice 2：Canonical prices、corporate actions 與 labels（已提交）

- Provider-neutral corporate-action events，保留 revision、announcement/effective/available time 與 provisional quality。
- Raw/canonical/split-adjusted/total-return/factor 分離；adjusted lineage 與 analytical/PIT 語意明示。
- Content-addressed candidate snapshot、checksum validation、atomic current promotion 與 pinned version reader。
- TWSE/TPEx-capable calendar、holiday/unexpected closure 與個股 observation classification。
- Versioned availability policy；日價 publication/finalization buffer 與 quality-gated decision snapshot。
- Next-session O2O/O2C、N-session O2O、benchmark-relative、eligible-universe rank labels。
- Adjustment、交易日、listing/delisting、停牌、no-same-day、promotion failure 與 leakage tests。

精確公式與限制見 ADR 0005。本 Slice 不含 feature 搜尋或 ML。

## Slice 3：Features 與 factor research（已提交）

- Versioned feature registry、14 個 OHLCV baseline factors 與 deterministic feature-set manifest。
- PIT rolling engine、composable preprocessing、candidate materialization 與 row lineage inspect。
- Daily cross-sectional IC、quantile portfolio、turnover、IC decay、year/market breakdown。
- Exploratory/validated completeness gate 與完全 pinned CLI。
- 尚未訓練任何 ML model。

## Slice 4：Leakage-safe ML baseline（已實作，待審核）

- Exact-time supervised dataset，固定 feature order/dtype，完整上游版本與 deterministic checksum。
- Expanding／rolling walk-forward；以實際 label interval purge，以交易 session embargo，final holdout 不參與選模。
- 每 fold 僅以 train fit median imputation、winsorization、standardization。
- Dummy mean／zero、weighted linear、Ridge 小型 grid、Elastic Net 小型 grid；不含 tree boosting。
- 預設每個 decision date 總權重相同；validation daily Spearman IC 選模，test 永不選模。
- Immutable OOS ledger、fold/aggregate metrics、decision-date block bootstrap、完整 candidate inventory。
- JSON-only Candidate model artifact、Git/source-tree provenance、model card 與 promotion fail-closed gate。
- `build-ml-dataset`、`run-ml-experiment`、`inspect-experiment`、`generate-model-card`，皆要求 pinned version 並支援 dry-run。

精確契約與限制見 ADR 0007。本 Slice 僅報 gross-before-costs，不宣稱可交易 alpha。

## Slice 5：Research benchmark 與 consolidated OOS report（建議）

- 補齊 point-in-time 市值權重 benchmark 與 TAIEX／TPEx index 分開歸因。
- 將 factor 與 ML OOS 結果整合為單一、完全 pinned 的可重現研究報告。
- 加入 multiple-testing correction 所需統計與正式 holdout access audit workflow。
