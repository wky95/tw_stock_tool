# 系統架構

## 原則

1. 研究、回測、paper、live 共用 domain、feature、strategy、portfolio 與 risk 邏輯，只替換 clock、data feed、execution/broker adapter。
2. 策略輸出 `TargetPosition`，不能直接呼叫 broker。
3. 原始資料 immutable；衍生資料、設定、程式與模型都有版本和 lineage。
4. 所有決策以 `available_time` 判定可見資訊，不能只靠資料列日期。
5. OMS、risk、reconciliation 是獨立安全邊界；狀態不確定時 fail closed。
6. 先完成可驗收垂直切片，未實作能力不建立假 adapter 或空成功路徑。

## 模組與依賴

```text
services / cli / api / workers                 composition roots
                 |
    research  strategies  portfolio  risk      use cases / policy
       |          |          |        |
 data/features/labels/models  backtest/execution/oms
                 |                    |
              domain <---------- ports/interfaces
                 ^                    |
       storage / data adapters / broker adapters
```

`domain` 不依賴 framework、資料庫、網路或 vendor SDK。Adapter 可以依賴 domain/port，domain 不反向依賴 adapter。跨模組狀態只經過明確 repository 或 immutable event。

## Domain model

- `Instrument`：market、symbol、asset type、currency、lot/tick rules。
- `Bar`：OHLCV 與 event/available/ingestion time、source、data version。
- `Feature`：名稱與版本、as-of/available time、值與 lineage metadata。
- `Signal` / `Forecast`：策略或模型的觀察，不含下單副作用。
- `TargetPosition`：策略期望權重；portfolio 層才能整合多策略資金。
- `OrderIntent`：經 sizing 後的下單意圖，帶 idempotency key。
- `RiskDecision`：approve/reject、規則版本、原因與實際輸入。
- `Order` / `Fill`：本地 OMS 狀態與 broker 事實分離。
- `Position` / `PortfolioSnapshot`：可持久化、可對帳的會計狀態。

## 事件與控制流

```text
BarAvailable -> Feature/Forecast -> TargetPosition
 -> Portfolio sizing -> OrderIntent -> Pre-trade Risk
 -> OMS submit -> Broker ack/fill -> Accounting
 -> Snapshot -> Reconciliation -> Report/Alert
```

事件有 event/correlation/causation id、aggregate sequence 與 schema version。回測使用 deterministic clock 和 broker simulator 重播；paper/live 接真實 clock 與 adapter。網路逾時後 OMS 先依 client order id / idempotency key 查詢，不直接重送。

## 儲存邊界

- Raw：不可變 vendor payload，包含 fetch/ingestion metadata 與 checksum。
- Curated：schema validated Parquet snapshot，包含 point-in-time 欄位。
- Feature/label：有 definition version 和 source snapshot id 的 Parquet。
- Artifact：experiment manifest、model、model card、圖表與 report。
- Transactional state：order/fill/position/journal/checkpoint，先 SQLite repository abstraction，部署可換 PostgreSQL。

## 環境與安全

Dev、test、paper、live 使用不同設定、state path 與 credentials。Live 需三重設定 gate，後續再加 broker credential class、freshness、connectivity、reconciliation、risk self-test 與 deployment approval。任何 critical uncertainty 都禁止新增風險；kill switch 不依賴 strategy process。

## 分階段界線

- Phase 0（完成）：package、config、logging、domain/event vocabulary、CI、ADR。
- Phase 1：可重現的 point-in-time data-to-OOS research pipeline。
- Phase 2：event-driven backtest、accounting、cost、risk、replay。
- Phase 3：paper broker、persistent OMS、recovery、reconciliation、monitoring。
- Phase 4（總體目標）：指定券商 adapter、小額 live safety gates 與 runbook。
- Phase 4A（完成）：broker readiness audit、provider-neutral contracts、離線 conformance harness；
  不含 broker adapter、credentials、network transport 或 live enablement。
- Phase 4B（完成，offline readiness only）：production-data contracts/conformance、broker 官方
  語意證據、provider/license decision pack、secret/control-plane/deployment policy schemas；不含
  provider/broker adapter、credentials、network、部署或 live enablement。
- Phase 4C+（gated）：licensed production-data adapter、secret backend、credentialed simulation
  與指定券商 adapter；只有所有 go/no-go gate 通過後才能另提 staged-capital 實作。
- Phase 5：多市場、多策略、optimizer、advanced execution、drift。
