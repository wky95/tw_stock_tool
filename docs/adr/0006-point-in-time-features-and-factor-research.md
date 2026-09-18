# ADR 0006：Point-in-time features 與傳統因子研究

## 狀態

Phase 1 Slice 3，待審核。所有目前研究結果只能標記 `exploratory`。

## Feature contract 與時間語意

每個 feature contract 固定 name/version、數學定義、所需欄位、price view、session lookback、minimum observations、availability/missing/preprocessing/neutralization policies、markets、dtype、code provenance 與 pinned dataset version。同名同版本但 contract hash 不同時 registry 拒絕註冊。

Feature implementation 只收到 contract 宣告的輸入欄位；label columns 禁止出現在 contract。資料集版本不得使用 `latest` 或 `current`。Feature-set manifest 對排序後 contracts、dependencies 與 dataset version 做 deterministic hash。

```text
session t close finalized (18:00) -> feature(t) / cross-section(t)
                                     |
                                     v
next valid session s1 09:00 --------> label entry
next exit session ------------------> label exit / evaluation
```

對 decision day `t`，engine 只取 calendar 中截至 `t` 的 sessions，並要求所有 observation 的 `available_at <= decision_time(t)`。缺 session、零量／疑似停牌、必要欄位缺失與 lookback 不足均輸出 invalid reason，不填零、不 forward-fill。修改未來價格或未來 membership 不得改變過去 feature。

## Baseline factors

`r_t = close_t / close_(t-1) - 1`。Rolling window 均指 trading sessions。

| Feature | 精確定義 |
|---|---|
| `reversal_1` | `-r_t` |
| `momentum_5` | `close_t / close_(t-5) - 1` |
| `momentum_20` | `close_t / close_(t-20) - 1` |
| `momentum_60` | `close_t / close_(t-60) - 1` |
| `realized_volatility_20` | 最近 20 個 `r` 的 sample standard deviation (`ddof=1`)，需 21 prices |
| `downside_volatility_20` | `sqrt(mean(min(r,0)^2))`，最近 20 returns，需 21 prices |
| `average_traded_value_20` | 最近 20 sessions（含 `t`）的 `mean(traded_value)`，單位 TWD |
| `volume_activity_proxy_20` | `volume_t / mean(volume[t-19:t])`，denominator 含 `t`；不是正式 turnover |
| `amihud_illiquidity_20` | 最近 20 returns 的 `mean(abs(r_t)/traded_value_t)` |
| `overnight_return` | `open_t / close_(t-1) - 1` |
| `intraday_return` | `close_t / open_t - 1` |
| `price_to_moving_average_20` | `close_t / mean(close[t-19:t]) - 1` |
| `distance_from_rolling_high_20` | `close_t / max(high[t-19:t]) - 1`，rolling high 含 `t` |
| `volume_surprise_20` | `volume_t / mean(volume[t-20:t-1]) - 1`，denominator 不含 `t` |

沒有可靠 PIT shares outstanding，因此 `volume_activity_proxy_20` 不稱為 turnover。Sector/size neutralization 只有 fail-closed interface，不以今日分類或市值回填。

## Preprocessing

Missing indicator、每日 cross-sectional average-tie rank、每日 z-score 與每日 winsorization 不使用跨期統計。`RobustScaler` 必須明確 `fit` 後才能 `transform`，artifact parameters 保存 fit start/end、dataset version、transformation version、median 與 MAD。任何需要 fit 的後續 transformation 遵守相同 contract。

## Factor evaluation

Pearson/Spearman IC 每個 decision date 橫斷面計算；Spearman 使用 average ranks 處理 ties。少於 3 筆或 constant cross-section 標為 invalid。摘要定義：

```text
IC mean = mean(valid daily Pearson IC)
IC std  = sample_std(valid daily Pearson IC)
ICIR    = IC mean / IC std * sqrt(252)
positive ratio = count(IC > 0) / valid IC days
```

Report metadata 保存 daily IC frequency、252 annualization factor、`annualized=true`、invalid dates excluded 與有效 daily IC count。少於 20 個有效日期時 ICIR 標記 unavailable，不輸出看似可靠的年化數字。Year/market breakdown 先算 daily cross-sectional IC 再聚合，不把股票日期視為獨立 iid observations。不提供普通 pooled p-value；本 Slice顯著性標記 `not_reported`。

Quantile portfolio 預設 5 組，依 `(factor value, instrument_id)` stable sort，因此 boundary ties deterministic；每組 equal-weight、gross return。Top-minus-bottom 依明示 `long_high`/`long_low` 方向。

目前輸出明確命名 `membership_turnover = 1 - |current ∩ prior| / |current|`，不是含價格漂移的完整 portfolio turnover。新進股票增加 turnover；離開、invalid feature/label 會透過 current membership 變動反映；quantile 人數改變以當期人數作 denominator；不考慮持有期間價格漂移。不得與未來實作的 `0.5 * sum(|w_t-w_pretrade_drifted|)` 混用。報告固定標記 `gross_before_costs`。

## Completeness gate

每份報告保存 universe completeness、unknown markets、provisional listing dates、unsupported corporate actions、missing suspension status、dataset/feature-set/label/availability versions。任一缺口存在即：

- 標題為 `EXPLORATORY — INCOMPLETE POINT-IN-TIME DATA`；
- CLI 可成功產生開發報告；
- validated feature/factor promotion fail closed。

Feature materialization 沿用 candidate/current snapshot、dirty-worktree provenance 與 checksum promotion 規則，並 pin dataset、universe、label、availability 及 code version。Factor report 必須 pin feature artifact version，tested-factor inventory 保存 evaluated、invalid、failed 與 rejected attempts，而非只保留成功者。輸出依 `decision_date, feature_name, instrument_id` canonical sorting，dataset name 含 feature-set version，便於按日期與 feature set 讀取及單列 lineage debug。
