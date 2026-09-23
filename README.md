# Island Quant（島嶼量化）

個人使用、以資金安全與可重現性為優先的台股量化研究與交易系統。專案目前完成 Phase 0、Phase 1、事件驅動成本後回測、exact-version real exploratory pipeline 與 deterministic paper OMS。Live broker 尚未實作。

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

Phase 2 Slice 3 已提供 exact-version real exploratory pipeline：

- Typed、checksum-verified、path-safe filesystem adapters，支援 bounded batch reads。
- Strict selected-OOS prediction adapter 與完整 dataset/universe/model lineage。
- Checkpoint/resume、idempotent atomic stage publishing 與資源上限。
- Existing local cache → features／labels／expanding baseline／targets → event-driven backtest。
- Machine-readable PIT coverage/promotion blockers 與 read-only Pipeline Dashboard mode。

Phase 3 paper OMS 提供 SQLite ACID state/event journal、transactional outbox、optimistic
concurrency、restart recovery、deterministic pinned-market paper broker 與 fail-closed
reconciliation。所有命令必須顯式 `--paper`，不接受 broker credentials，也不存在 live flag：

```bash
island-quant paper-init --paper
island-quant paper-status --paper
island-quant paper-orders --paper
island-quant paper-reconcile --paper
```

`paper-cancel-all` 與 `paper-recover` 預設只顯示 dry-run；實際變更必須加入 `--confirm`。
Paper fill 是工程模擬，不代表真實券商成交或投資績效。

Phase 4A 已完成 broker/production readiness audit，加入 provider-neutral broker session、
capability negotiation、typed command/event/error/reconciliation contracts，以及完全離線的
failure-injection conformance fixture。這些 contracts 尚未接上 PAPER runtime，也沒有 Shioaji
adapter、SDK dependency、credential resolver 或 network transport；live trading 仍不可用。官方
能力與未知事項見 [Shioaji capability matrix](docs/SHIOAJI_CAPABILITY_MATRIX.md)，所有未解 blocker
見 [machine-readable readiness report](docs/readiness/phase4a_broker_readiness.json)。

Phase 4B 已加入 provider-neutral production-data contracts 與完全離線 conformance harness，
涵蓋 PIT identity/revision、tradability/calendar、benchmark、市值、stream freshness/sequence、
entitlement/retention、coverage 與 provider reconciliation。另保存 Shioaji 官方語意證據、資料
授權／provider 評估模板及 secret/control-plane/deployment policy schemas。這些全是 design 與
offline fixtures：沒有選擇或連接資料商、沒有 secret backend、broker adapter、credentials、
部署、控制 endpoint 或 live toggle。見
[Phase 4B readiness](docs/readiness/phase4b_production_readiness.json)。

Phase 4C 新增嚴格、確定性且唯讀的 production admission/no-go evaluator。它只接受一個明確
absolute path 的 versioned decision/evidence pack，拒絕 unknown fields、過期／checksum 錯誤的
證據、未知 critical 決策、低於 100% PIT coverage、provider disagreement、stream faults、silent
fallback、不安全 deployment 決策，以及沒有官方證據就消除 broker blocker 的 pack：

```bash
island-quant validate-production-readiness \
  --pack "$PWD/docs/readiness/phase4c_synthetic_readiness_pack.json"
```

內附 pack 全是 synthetic、offline、not provider behavior、not licensed production data、not
live-ready。即使離線 conformance 通過，報告仍固定為 `production_admission=no_go` 與
`live_trading_ready=false`。目前沒有 provider/operator/legal 的正式答案，系統仍不得實盤。

Paper Operations 加入 pinned-calendar scheduler、singleton lease、bounded retry/dead-letter、
startup reconciliation、heartbeat、safe mode、風控 limits、持久化 alerts 與 immutable daily
report。唯讀 operations UI：

```bash
island-quant dashboard --paper-operations
```

預設仍只監聽 `127.0.0.1:8765`。UI 沒有控制 endpoint、live toggle 或 broker 連線；完整復原
步驟見 [Paper Operations runbook](docs/PAPER_OPERATIONS_RUNBOOK.md)。

Paper accounting runtime 會從 immutable OMS fills 重播 Decimal/TWD ledger，以 pinned instrument
reference、calendar、fee 與 settlement policy 產生持久化、content-addressed portfolio snapshot。
Pending buy cash及 sell quantity reservations 與 OMS/outbox 同 transaction 建立，fills、rejects
與 cancellations 會原子調整或釋放 reservation。

安全的單次執行模式：

```bash
island-quant paper-service-run --paper --once \
  --session 2025-01-02 --as-of 2025-01-02T09:00:00+08:00 --dry-run
```

移除 `--dry-run` 後會執行十個 versioned paper jobs、更新 portfolio telemetry 並產生 immutable
paper daily report。預設沒有 strategy/model adapter，因此明確產生零個新 target；不會偷偷下單。

若有經離線 promotion、完整驗證且已 pin 住所有上游 lineage 的
`paper_target_snapshots` artifact，可顯式指定精確 SHA-256 版本：

```bash
island-quant paper-service-run --paper --once \
  --session 2025-01-02 --as-of 2025-01-02T09:00:00+08:00 \
  --target-artifact-version <exact-sha256> --dry-run
```

先移除 `--dry-run` 才會將 target 經 long-only／cash／exposure 風控轉成 persistent OMS
intent。命令不接受 `latest`，不會把 exploratory artifact 自動升級成 paper candidate；artifact
缺漏、版本／checksum／PIT session 不一致、持倉未被 target 完整涵蓋時一律 fail closed。

Validated research target 必須先經顯式 PAPER promotion；預設 dry-run 不寫入任何 artifact：

```bash
island-quant paper-promote-target --paper \
  --source-version <exact-research-target-sha256> \
  --reviewer local-operator --reason "offline review passed" \
  --approved-at 2025-01-01T18:00:00+08:00 --execution-session 2025-01-02 \
  --approve-pit --approve-data-license --approve-risk --dry-run
```

核對輸出後將 `--dry-run` 改為 `--confirm`，才會建立 immutable approval 與 paper target
artifacts。三項 checklist 缺一、來源為 exploratory、核准晚於 execution session 或 lineage
不完整時都會拒絕。Paper Operations Dashboard 會以 reconciled marks 唯讀顯示 target／actual
weight、drift 與 exact artifact version。

多交易日的離線 soak 與 failure drill 由一份 content-addressed JSON plan 驅動。先用 dry-run
驗證 pinned calendar、instrument references、market events 與 exact target versions；只有明確
`--confirm` 才會寫入 PAPER SQLite state 與 immutable soak report：

```bash
island-quant paper-soak-run --paper --plan <paper-soak-plan.json> --dry-run
island-quant paper-soak-run --paper --plan <paper-soak-plan.json> --confirm
```

每個 session 都會重建 runtime 以演練 process restart，並重播同一 session 驗證冪等性。
失敗也會產生標記為 `incomplete` 的診斷 report。Plan 內的 broker participation cap 是可重現
failure injection，不代表真實券商成交容量。Soak 使用獨立的 `state/paper-soak/` 設定路徑，
不得與一般 PAPER operations database 重疊；所有結果只屬工程驗證，不能解讀為真實績效。

執行前可先 dry-run；超過設定安全上限必須明確加入 `--confirm-large-run`：

```bash
island-quant run-research-pipeline \
  --pipeline-config config/exploratory_pipeline.yaml --dry-run
island-quant run-research-pipeline \
  --pipeline-config config/exploratory_pipeline.yaml
island-quant inspect-pipeline-run --run-version <exact-version>
island-quant dashboard --pipeline-run-version <exact-version>
```

此命令只讀既有 `data/cache/`，正式輸出與 checkpoint 依設定寫入 ignored runtime
directories。結果固定標記 `REAL EXPLORATORY` 與
`EXPLORATORY — INCOMPLETE POINT-IN-TIME REFERENCE DATA`，不得解讀為 alpha 或投資建議。

## 本機 Research Dashboard

Dashboard 是可操作的唯讀 UI。`--demo` 只讀固定 synthetic fixture；
`--artifact-version` 則只讀一個明確版本的本機 backtest candidate。兩種模式不會互相
fallback，不連接券商，也沒有下單、模型 promotion 或 live-control endpoint。

安裝後啟動：

```bash
source .venv/bin/activate
island-quant dashboard --demo
```

建立一個完全離線的 synthetic backtest candidate，再以 artifact mode 檢視：

```bash
island-quant run-backtest --demo
island-quant inspect-backtest --artifact-namespace demo --artifact-version <exact-version>
island-quant dashboard --artifact-namespace demo --artifact-version <exact-version>
```

另提供 `compare-backtests --left-version ... --right-version ...` 與
`generate-backtest-report --artifact-version ... --output ...`。非 demo 的
`run-backtest` 必須明確 pin prediction、model、dataset、universe、calendar、corporate
action、benchmark 與 target-policy versions；本 Slice 尚未配置真實 selected-ledger
filesystem adapter，因此會 fail closed，不會偷偷改用 demo。

Demo backtest artifacts 固定寫入設定之 `artifact_root/demo/` namespace；candidate namespace
不接受 demo fallback。Artifact version 僅接受 64 字元 lowercase SHA-256 digest。Markdown
report 預設拒絕覆寫，必須明確加上 `--force`，且不可輸出到 immutable artifact root 內。

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

Demo Dashboard fixture 固定 seed `20240918`，包含 15 檔虛構 instrument、90 sessions、14 個 baseline factors、3 個 ML experiments 和數筆刻意建立的品質問題。Synthetic backtest 另使用固定的三標的、三交易日 fixture 與完整敏感度 grid。所有結果固定標記 synthetic／exploratory；完美 IC、信賴區間、報酬、Sharpe 或 drawdown 僅是工程 fixture，沒有統計、投資或獲利意義。

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
- [ADR 0010：Exact-version real exploratory pipeline](docs/adr/0010-exact-version-exploratory-pipeline.md)
- [ADR 0011：Persistent paper OMS](docs/adr/0011-persistent-paper-oms.md)
- [ADR 0012：Recoverable paper operations](docs/adr/0012-paper-operations.md)
- [ADR 0013：Replayable paper accounting runtime](docs/adr/0013-paper-accounting-runtime.md)
- [ADR 0014：Exact-version paper target execution](docs/adr/0014-exact-paper-target-execution.md)
- [ADR 0015：Audited PAPER promotion and portfolio risk](docs/adr/0015-paper-promotion-and-portfolio-risk.md)
- [ADR 0016：Multi-session PAPER soak and failure drills](docs/adr/0016-paper-soak-and-failure-drills.md)
- [ADR 0017：Provider-neutral broker readiness](docs/adr/0017-broker-production-readiness.md)
- [Broker integration readiness runbook](docs/BROKER_INTEGRATION_RUNBOOK.md)
- [Shioaji official capability matrix](docs/SHIOAJI_CAPABILITY_MATRIX.md)
- [Production data readiness](docs/PRODUCTION_DATA_READINESS.md)
- [ADR 0018：Production data and semantic closure](docs/adr/0018-production-data-semantic-closure.md)
- [Production data integration runbook](docs/PRODUCTION_DATA_INTEGRATION_RUNBOOK.md)
- [Provider evaluation and license questionnaire](docs/PROVIDER_EVALUATION_AND_LICENSE_QUESTIONNAIRE.md)
- [Security and deployment design](docs/SECURITY_DEPLOYMENT_DESIGN.md)
- [ADR 0019：Production admission/no-go evaluator](docs/adr/0019-production-admission-no-go-evaluator.md)
- [Production readiness validation runbook](docs/PRODUCTION_READINESS_VALIDATION_RUNBOOK.md)
- [Decision/evidence pack schema](docs/READINESS_DECISION_PACK_SCHEMA.md)
- [Provider admission and reconciliation runbook](docs/PROVIDER_ADMISSION_RECONCILIATION_RUNBOOK.md)
- [Security/deployment decision checklist](docs/SECURITY_DEPLOYMENT_DECISION_CHECKLIST.md)

## 設定安全原則

- Secret 只由環境或 secrets manager 注入，不能提交到 repository 或寫入 log。
- dev/test/paper/live 的 credentials 和持久化狀態必須分離。
- 策略只輸出 target position；risk 與 OMS 可獨立拒絕訂單。
- Broker 或本地狀態不確定時 fail closed，不重複送單、不增加新風險。
- Broker credentials 只能由 composition root 透過 opaque reference 取得；domain/config/artifact
  不保存 secret value。
- 模型 promotion 需要預先門檻、paper 驗證與人工批准。
