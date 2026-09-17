# Phase 1 檔案級計畫與驗收

## 最小垂直切片

固定、明確版本的台股 universe，從日 K 原始 payload 一路產生 immutable snapshot、point-in-time feature/label dataset、walk-forward baseline model 與 OOS 因子報告；單一 CLI 可重現。

## 檔案級計畫

| 檔案／目錄 | 真實職責 |
|---|---|
| `src/island_quant/data/ports.py` | historical source、snapshot catalog protocol |
| `src/island_quant/data/schema.py` | point-in-time bar、corporate action、universe membership schema |
| `src/island_quant/data/ingestion.py` | incremental/idempotent ingestion、checkpoint、content hash |
| `src/island_quant/data/validation.py` | schema、duplicate、missing、OHLC、timestamp、coverage checks |
| `src/island_quant/data/adapters/<source>.py` | 唯一首發資料源的 payload 轉換；不解析 UI HTML |
| `src/island_quant/storage/datasets.py` | immutable Parquet snapshot 與 DuckDB catalog |
| `src/island_quant/features/registry.py` | feature definition metadata、版本、依賴與計算契約 |
| `src/island_quant/features/baselines.py` | momentum、reversal、volatility、volume/liquidity baseline |
| `src/island_quant/labels/forward_return.py` | 依 available/decision/execution time 建立多 horizon label |
| `src/island_quant/research/splits.py` | expanding/rolling walk-forward 與 purge/embargo |
| `src/island_quant/research/factor_analysis.py` | Pearson/Spearman IC、ICIR、decay、quantile、turnover、coverage |
| `src/island_quant/research/experiments.py` | 成功與失敗 run、config/data/code hash、metrics/artifact manifest |
| `src/island_quant/models/baseline.py` | sklearn Pipeline + Ridge，所有 transformer 僅 fit train |
| `src/island_quant/reports/factor_report.py` | OOS JSON/HTML report，不挑選隱藏失敗 run |
| `src/island_quant/cli.py` | `ingest-data`、`validate-data`、`build-features`、`train-model`、`evaluate-model` |
| `tests/data/` | ingestion idempotency、時間欄位、品質錯誤 fixtures |
| `tests/research/` | no-lookahead、purge/embargo、train-only fitting、determinism tests |
| `tests/integration/test_research_pipeline.py` | 單一命令 raw-to-OOS report |

## 驗收標準

- 同一來源資料與設定重跑得到相同 snapshot id 與數值結果。
- 人工注入未來值的測試證明 feature builder 在 available time 前讀不到它。
- 重疊 label 不會跨越 purge/embargo 進入相鄰 validation fold。
- scaler/imputer/model 只在各 fold 的 train 範圍 fit。
- 報告含資料版本、設定 hash、code version（無 git 時記為 unavailable）、seed、期間、IC/Rank IC/ICIR、quantile spread、turnover、coverage 與含成本結果。
- 任何 validation failure 或失敗實驗都留下 manifest 並回傳非零 exit code。

