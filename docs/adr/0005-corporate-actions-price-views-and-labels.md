# ADR 0005：公司行動、價格視圖、snapshot 與 forward labels

## 狀態

Phase 1 Slice 2，待審核。

## 資料集與 revision

Raw unadjusted OHLCV、canonical unadjusted OHLCV、split-adjusted view、total-return view、adjustment factors、corporate actions 與 labels 是互相分離的資料集。Raw payload immutable；derived manifest 保存 source checksums、schema、transformation code 與 configuration version。

新 snapshot 先寫為 `candidate`。Manifest 保存 Git commit、dirty flag、完整 source-tree hash、code/schema version、canonical config hash、source checksums 與 transformation algorithm version；dataset identity 包含這些 provenance，因此相同 commit 下的不同未提交程式不會碰撞。Dirty candidate 可供開發，但 `validated=true` promotion 一律拒絕。

`promote_dataset(dataset, version)` 先重算 manifest identity 與 Parquet checksum，再取得同 filesystem 的 file lock；可使用 expected-current compare-and-swap。Pointer 經 file fsync、atomic replace 及支援時 parent-directory fsync 更新。舊 Parquet/manifest 不覆寫。研究與 label 必須傳明確 `dataset_version`/`price_view_version`；`latest` 僅是 Slice 1 相容入口，不是實驗識別碼。

`ratio` 定義為 `post_action_shares / pre_action_shares`。同一 `(source, source_event_id)` 的 revisions 全部保存。分析視圖取最新 revision；point-in-time 視圖只在 `available_at <= decision_time` 的 revisions 中取最新。未知 `available_at` 只能標 `provisional`，不得以 `ingested_at` 代填。

## 調整數學

令未調整 OHLC 價格為 `P_t`、成交股數為 `V_t`。對日期 `t` 之後發生且視圖允許使用的 split-like actions（split、reverse split、stock dividend、capital reduction），share ratio 為 `r_j`：

```text
S_t = product(r_j for effective_date_j > t)
split_adjusted_price_t = P_t / S_t
split_adjusted_volume_t = V_t * S_t
```

所以 2-for-1 split 的 `r=2`，舊價格除以 2、舊成交股數乘以 2；1-for-2 reverse split 或 50% capital reduction 的 `r=0.5`，舊價格乘以 2、舊成交股數乘以 0.5。

Cash dividend 不沿用 split 公式。對 ex-date `e`、每股現金 `D_e` 與前一有效交易日未調整收盤 `C_(e-1)`：

```text
q_e = (C_(e-1) - D_e) / C_(e-1)
Q_t = product(q_e for e > t)
total_return_adjusted_price_t = P_t / S_t * Q_t
```

Cash dividend 不改 volume。Factor 必須為有限正值，調整後 OHLC 必須合法，否則 fail closed。Capital reduction 強制區分 `loss_offset`、`cash` 與 `unknown`；目前只有 loss-offset reduction 可套用 share-ratio 公式，cash/unknown 皆標為 unsupported 並 fail closed。Rights issue 亦 fail closed，不會靜默略過。

事件先按 instrument、effective date、action type、source、source event ID 與 revision canonical sorting，不依賴 provider 輸入順序。同一證券同一日有多個會影響調整的事件時，目前回報 `UNSUPPORTED_SAME_DAY_CORPORATE_ACTIONS` 並停止，不產生看似精確的 factor；錯誤碼可寫入 quality flags/quality artifact。

未帶 `decision_time` 的 backward-adjusted view 明確標記 `view_semantics=analytical_backward_adjusted`、`uses_future_actions=true`。帶 decision time 時只用當時可得事件並標記 `point_in_time`。

## Availability policy

`tw-daily-v1` 將日價 event time 設為 Asia/Taipei 13:30、publication time 為 17:30，再加 30 分鐘 finalization buffer，所以預設 decision time 為 18:00。`decision_snapshot` 只在完整性／品質檢查通過後接受 `available_at <= decision_time` 的資料。

公司行動分別保存 announcement、effective/ex、record、payment、available 與 ingested timestamps。決策可用性只看真實 `available_at`，不是 effective date 或 ingestion time。

## Label 數學

決策日為 `t`，下一有效 session 為 `s1`；所有 return 均為 gross return，不扣成本，與 execution simulation 分離。

```text
next-session O2C = close(s1) / open(s1) - 1
next-session O2O = open(s2) / open(s1) - 1
N-session O2O    = open(s(N+1)) / open(s1) - 1
benchmark-relative = asset gross return - benchmark gross return（相同 entry/exit）
cross-sectional rank = 當日 eligible universe 內 gross return 的 [0,1] percentile rank
```

最早執行為 `s1` 09:00 Asia/Taipei。同日 close 不作成交價。缺 entry/exit、停牌、零成交量、不可交易／漲跌停鎖死或跨 listing/delisting 邊界時，label 無效並保存原因。Rank 只用 decision date 當下 eligible membership。

## 已知限制

- 尚無具適當授權且完整的歷史公司行動 feed，不能宣稱 production completeness。
- FinMind 推導 listing date 仍為 provisional，全市場研究繼續 fail closed。
- Rights issue、cash/unknown capital reduction、同日多調整事件、稅務與 withholding 尚未模型化；目前全部 fail closed。
- 漲跌停需上游提供 `is_tradable`/`limit_locked`；只有 OHLCV 無法判定排隊成交。
- Unexpected closure 可被 schema 表示，但仍需可信的歷史來源。
