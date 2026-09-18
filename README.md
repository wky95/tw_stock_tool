# Island Quant（島嶼量化）

個人使用、以資金安全與可重現性為優先的台股量化研究與交易系統。專案目前完成 Phase 0 與 Phase 1 的 point-in-time data、labels、factors 及 leakage-safe ML baseline。可信成本後回測、paper broker 與 live broker 尚未實作。

> 這是工程與研究工具，不保證獲利，也不構成投資建議。Live trading 預設且目前實際不可用。

## 現況

新架構位於 `src/island_quant/`，採模組化單體與事件驅動核心。舊版 `twbacktest/`、`app.py`、`static/` 及其測試暫時保留作參考與回歸，不是新架構的 live trading 能力。

Phase 0 已提供：

- 可安裝 Python 3.12+ package 與 CLI。
- YAML + Pydantic 強型別設定、環境變數 override、安全的 live gate。
- Instrument、Bar、Feature、Signal、Forecast、TargetPosition、OrderIntent、Order、Fill、Position、PortfolioSnapshot、RiskDecision。
- 可追溯、可重播的 domain event envelope。
- JSON structured logging、pytest、Ruff、mypy、GitHub Actions 與 Docker 骨架。
- 架構決策、現況／缺口、Phase 1 檔案級計畫與驗收標準。

Phase 1 Slice 1 已提供：

- FinMind production adapter 與完全離線 fixture adapter。
- Immutable raw response、versioned normalized Parquet、manifest/checksum、DuckDB catalog。
- Instrument master、listing/delisting、交易日曆與 timezone-aware OHLCV。
- Incremental/idempotent ingestion、checkpoint/resume、品質報告。
- Versioned `UniversePolicy`、每日 membership 與 exclusion reasons。
- Universe completeness metadata 與預設 fail-closed research gate。

Phase 1 Slice 2 已提供：

- Corporate-action revision domain 與 versioned availability policy。
- Canonical unadjusted、split-adjusted、total-return 與 adjustment-factor views。
- Candidate/promotion snapshot 與 pinned historical version reader。
- Canonical market sessions 與 missing-observation classification。
- 五類 gross forward-return labels，以及 listing/delisting、停牌與 no-lookahead guards。

Phase 1 Slice 3 已提供：

- Versioned feature contract/registry 與 14 個傳統 OHLCV factors。
- Strict PIT rolling engine、preprocessing 與 reproducible materialization。
- Daily IC、quantile returns、turnover、IC decay 與 breakdown evaluator。
- Exploratory completeness gate，以及要求 pinned versions 的研究 CLI。

Phase 1 Slice 4 已提供：

- Exact-time supervised dataset 與 deterministic lineage manifest。
- Expanding／rolling walk-forward、label-interval purging、trading-session embargo。
- Fold-local preprocessing、Dummy／Linear／Ridge／Elastic Net baseline。
- Immutable OOS prediction ledger、block bootstrap、Candidate model artifact 與 model card。

## 本機 Research Dashboard

Dashboard 是可操作的唯讀 UI 原型，只讀取固定的 synthetic fixture，不會讀取個人 production artifact、不連接券商，也沒有下單、模型 promotion 或 live-control endpoint。

安裝後啟動：

```bash
source .venv/bin/activate
island-quant dashboard --demo
```

預設只監聽 `127.0.0.1:8765`。瀏覽：

- Dashboard：http://127.0.0.1:8765
- OpenAPI：http://127.0.0.1:8765/docs
- Health：http://127.0.0.1:8765/health

按 `Ctrl+C` 停止服務。需要開發時自動重載可使用：

```bash
island-quant dashboard --demo --reload
```

Docker 啟動時，容器內需顯式監聽 `0.0.0.0`，但 host port 仍限定在 localhost：

```bash
docker build -t island-quant:dashboard .
docker run --rm -p 127.0.0.1:8765:8765 \
  island-quant:dashboard dashboard --demo --host 0.0.0.0
```

Demo fixture 固定 seed `20240918`，包含 15 檔虛構 instrument、90 sessions、14 個 baseline factors、3 個 ML experiments 和數筆刻意建立的品質問題。所有頁面固定顯示 `DEMO / EXPLORATORY — NOT FOR LIVE TRADING`；完美 IC 及信賴區間僅是工程 fixture，沒有統計或獲利意義。

安全限制：服務預設 localhost、所有 dashboard API 都是 GET、live trading 永遠為 false、broker 永遠未設定，且 UI 不顯示 secrets、credentials、home directory 或敏感絕對路徑。

已知測試技術債：目前 FastAPI／Starlette 的 `TestClient` 會由上游套件發出一則 httpx 相容介面與一則 AnyIO alias deprecation warning。測試功能正常；為避免只為消除 warning 而進行大型 dependency upgrade，暫時保留並等待上游相容版本後再處理。

## 安裝與驗證

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

island-quant validate-config
island-quant show-config
island-quant doctor
island-quant ingest-data --provider fixture \
  --fixture-dir tests/fixtures/phase1 \
  --start 2024-01-02 --end 2024-01-10 \
  --symbols 1111,2222,3333
island-quant validate-data

ruff check .
mypy
pytest
```

環境變數使用雙底線表達巢狀設定：

```bash
ISLAND_QUANT__TRADING__INITIAL_CASH=2500000 island-quant show-config
```

任何未知設定 key 都會直接失敗。即使設定 `trading.mode=live`，缺少 `environment=live` 或 `trading.live_enabled=true` 仍不能載入；真正 live 啟動還需要 Phase 4 的更多安全檢查。

## 架構與文件

- [Phase 0 現況、假設、事件流與問題](docs/PHASE_0.md)
- [完整架構與模組責任](docs/ARCHITECTURE.md)
- [Phase 1 檔案級計畫](docs/PHASE_1_PLAN.md)
- [資料來源、授權與品質風險](docs/DATA_SOURCES.md)
- [ADR 0001：模組化單體](docs/adr/0001-modular-monolith.md)
- [ADR 0002：Live fail-closed](docs/adr/0002-live-fail-closed.md)
- [ADR 0003：Point-in-time 與儲存](docs/adr/0003-point-in-time-and-storage.md)
- [ADR 0004：Point-in-time Universe](docs/adr/0004-point-in-time-universe.md)
- [ADR 0005：公司行動、價格視圖、snapshot 與 labels](docs/adr/0005-corporate-actions-price-views-and-labels.md)
- [ADR 0006：Point-in-time features 與 factor research](docs/adr/0006-point-in-time-features-and-factor-research.md)
- [ADR 0007：Leakage-safe ML baselines](docs/adr/0007-leakage-safe-ml-baselines.md)

## 設定安全原則

- Secret 只由環境或 secrets manager 注入，不能提交到 repository 或寫入 log。
- dev/test/paper/live 的 credentials 和持久化狀態必須分離。
- 策略只輸出 target position；risk 與 OMS 可獨立拒絕訂單。
- Broker 或本地狀態不確定時 fail closed，不重複送單、不增加新風險。
- 模型 promotion 需要預先門檻、paper 驗證與人工批准。
