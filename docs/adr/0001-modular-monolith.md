# ADR 0001：先採模組化單體與事件驅動核心

- 狀態：Accepted
- 日期：2026-09-18

## 決策

第一版使用單一 Python package 的模組化單體。資料、研究、策略、portfolio、risk、execution、OMS 與 broker 以 domain model 和 port 分隔；回測、paper、live 共用事件與策略介面。

## 理由

個人系統目前不需要分散式服務的部署成本。事件 journal 與清楚邊界已能提供 deterministic replay、故障復原及未來拆服務的接縫。

## 後果

模組不得直接存取其他模組的資料庫表；跨邊界以 domain object、event 或明確 port 溝通。只有 composition root 可以組裝 adapter。

