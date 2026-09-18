# ADR 0004：Point-in-time 全市場 Universe

- 狀態：Accepted
- 日期：2026-09-18

## 決策

Production research 每個交易日以具版本的 `UniversePolicy` 建立 TWSE／TPEx 普通股 membership。每一檔都保存 eligible flag 與全部 exclusion reasons；ETF、ETN、權證、DR、特別股、尚未上市、已下市、停牌／無價、lookback 不足及流動性不足均不得悄悄消失。

## 後果

下市股票的歷史價量與 membership 必須保留。市場別或 listing history 無法證實時 fail closed 並出具品質 warning，而不是使用今日清單回推。Policy version 是研究 artifact lineage 的一部分。
