# ADR 0002：Live 預設關閉且 fail-closed

- 狀態：Accepted
- 日期：2026-09-18

## 決策

預設僅允許 backtest。Live 同時要求 `environment=live`、`trading.mode=live` 與 `trading.live_enabled=true`；Phase 4 前不提供 live broker adapter。

## 理由

設定錯誤、狀態未知、資料過期或 broker 失聯時，增加風險比錯過交易更危險。

## 後果

後續 live 啟動還必須加入 credentials 分離、資料新鮮度、broker connectivity、reconciliation、risk self-test、人工 approval 與 audit event。這些條件未全部成立時拒絕啟動或拒絕新風險。

